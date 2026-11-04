"""Curate expandable lead-label batches using pinned, local RTMDet screening."""
from collections import Counter, defaultdict
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time

from .bootstrap import atomic_json

RECIPE = 'lead-candidates-rtmdet-tiny-v1'
CHECKPOINT_SHA = '79b8a0b1e22b100e36a5591b24a1a97eae2fc3f29ad73cb4809d8c9c6cd19a52'


def hash_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for b in iter(lambda: stream.read(2**20), b''):
            h.update(b)
    return h.hexdigest()


def prepare_pool(data_root):
    """Read authoritative splits; never reassign existing evaluation frames."""
    data_root = Path(data_root).resolve()
    root = data_root / 'experiments/lead_cars'
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect((data_root / 'metadata.sqlite').as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    protected_sessions = {(r['source'], r['session_id']) for r in db.execute("SELECT source,session_id FROM images WHERE split IN ('val','test')")}
    protected_hashes = {r[0] for r in db.execute("SELECT sha256 FROM images WHERE split IN ('val','test')")}
    protected_phash = [int(r[0], 16) for r in db.execute("SELECT phash FROM images WHERE split IN ('val','test') AND phash IS NOT NULL AND phash!=''")]
    selected, seen_sessions = [], set()
    rows = list(db.execute("SELECT * FROM images WHERE source='comma10k' AND split='train' AND status='curated' AND review_status!='rejected' AND pool='commercial' AND synthetic=0"))
    rows.sort(key=lambda r: hashlib.sha256(('lead-curation-v1:' + r['image_id']).encode()).hexdigest())
    for row in rows:
        session = (row['source'], row['session_id'])
        if session in protected_sessions or session in seen_sessions or row['sha256'] in protected_hashes:
            continue
        if row['phash'] and any((int(row['phash'], 16) ^ h).bit_count() <= 4 for h in protected_phash):
            continue
        image = Path(row['path']).resolve()
        if not image.is_relative_to(data_root) or not image.is_file():
            continue
        tags = json.loads(row['tags'] or '{}')
        lighting = tags.get('lighting', {})
        lighting = lighting.get('value', 'unknown') if isinstance(lighting, dict) else lighting
        selected.append({'image_id': row['image_id'], 'source': row['source'], 'session_id': row['session_id'],
            'source_ref': row['source_ref'], 'source_split': 'train', 'pool': row['pool'],
            'image_source': image.relative_to(data_root).as_posix(), 'source_sha256': row['sha256'],
            'license_id': row['license_id'], 'lighting': lighting, 'phash': row['phash']})
        seen_sessions.add(session)
    db.close()
    atomic_json(root / 'screening-pool.json', {'recipe': RECIPE, 'records': selected,
        'protected_evaluation_excluded': True, 'one_frame_per_recording': True})
    return {'candidate_pool': len(selected), 'source': 'comma10k', 'original_split': 'train'}


def bucket(record, detections, w, h):
    if record['lighting'] in ('night', 'dark', 'twilight', 'dusk'):
        return 'low_light'
    central = [d for d in detections if d['forward_candidate']]
    nearest = max(central, key=lambda d: d['box'][3])
    x1, y1, x2, y2 = nearest['box']
    cx = (x1+x2)/(2*w)
    if len(detections) >= 4:
        return 'multiple_vehicles'
    if cx < .4:
        return 'left_offset'
    if cx > .6:
        return 'right_offset'
    if (x2-x1)*(y2-y1)/(w*h) < .008:
        return 'distant_vehicle'
    return 'central_vehicle'


def screen(data_root, weights, target=500):
    """Run in the existing detector environment; no training or network calls."""
    import cv2
    import numpy as np
    import torch
    from torchvision.ops import batched_nms
    from libreyolo import LibreYOLO
    data_root, weights = Path(data_root).resolve(), Path(weights).resolve()
    if hash_file(weights) != CHECKPOINT_SHA:
        raise ValueError('RTMDet checkpoint differs from the pinned app model')
    root = data_root / 'experiments/lead_cars'
    pool = json.loads((root / 'screening-pool.json').read_text())['records']
    cache = sqlite3.connect(root / 'screening.sqlite')
    cache.execute('CREATE TABLE IF NOT EXISTS screening(image_id TEXT PRIMARY KEY,recipe TEXT,source_sha TEXT,result TEXT)')
    torch.set_num_threads(4)
    cv2.setNumThreads(1)
    model = LibreYOLO(str(weights), device='cpu').model.eval()
    model.head.export = True
    mean = torch.tensor([103.53, 116.28, 123.675]).reshape(1, 3, 1, 1)
    std = torch.tensor([57.375, 57.12, 58.395]).reshape(1, 3, 1, 1)
    names = {2: 'car', 3: 'motorcycle', 5: 'bus', 7: 'truck'}
    accepted = []
    status = {'recipe': RECIPE, 'phase': 'screening', 'target': target, 'screened': 0, 'candidates': 0,
              'inference_device': 'cpu', 'checkpoint_sha256': CHECKPOINT_SHA,
              'human_labels_created': 0, 'training_started': False}
    prior = data_root / 'experiments/lead_cars/review.sqlite'
    existing = set()
    if prior.exists():
        db = sqlite3.connect(prior)
        existing = {r[0] for r in db.execute('SELECT image_id FROM images')}
        db.close()
    needed = max(0, target-len(existing))
    goal = max(needed, int(needed*1.5))
    atomic_json(root / 'curation_status.json', status)
    for record in pool:
        if record['image_id'] in existing:
            continue
        previous = cache.execute('SELECT recipe,source_sha,result FROM screening WHERE image_id=?', (record['image_id'],)).fetchone()
        if previous:
            if previous[0] != RECIPE or previous[1] != record['source_sha256']:
                raise ValueError('Cached screening recipe/source changed')
            result = json.loads(previous[2])
        else:
            path = data_root / record['image_source']
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != record['source_sha256']:
                raise ValueError('Original curation photo changed')
            image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError('Unreadable source photo')
            h, w = image.shape[:2]
            scale = min(640/w, 640/h)
            rw, rh = round(w*scale), round(h*scale)
            ox, oy = (640-rw)//2, (640-rh)//2
            canvas = np.full((640, 640, 3), 114, np.uint8)
            canvas[oy:oy+rh, ox:ox+rw] = cv2.resize(image, (rw, rh))
            tensor = torch.from_numpy(canvas).permute(2, 0, 1)[None].float()
            with torch.inference_mode():
                prediction = model((tensor-mean)/std)[0]
            scores, labels = prediction[:, 4:].max(dim=1)
            mask = (scores >= .45) & torch.isin(labels, torch.tensor(list(names)))
            boxes, scores, labels = prediction[mask, :4], scores[mask], labels[mask]
            keep = batched_nms(boxes, scores, labels, .5)
            detections = []
            for index in keep:
                x1, y1, x2, y2 = boxes[index].tolist()
                box = [max(0., min(w, (x1-ox)/scale)), max(0., min(h, (y1-oy)/scale)),
                       max(0., min(w, (x2-ox)/scale)), max(0., min(h, (y2-oy)/scale))]
                cx = (box[0]+box[2])/(2*w)
                forward = (.18 <= cx <= .82 and .38 <= box[3]/h <= .97
                           and box[2]-box[0] >= 12 and box[3]-box[1] >= 12
                           and (box[2]-box[0])*(box[3]-box[1])/(w*h) >= .0007)
                detections.append({'label': names[int(labels[index])], 'confidence': round(float(scores[index]), 5),
                                   'box': [round(v, 3) for v in box], 'forward_candidate': forward})
            result = {'width': w, 'height': h, 'detections': detections,
                      'has_forward_vehicle_candidate': any(d['forward_candidate'] for d in detections)}
            if result['has_forward_vehicle_candidate']:
                result['curation_bucket'] = bucket(record, detections, w, h)
            cache.execute('INSERT INTO screening VALUES(?,?,?,?)',
                          (record['image_id'], RECIPE, record['source_sha256'], json.dumps(result)))
            cache.commit()
        status['screened'] += 1
        if result['has_forward_vehicle_candidate']:
            accepted.append({**record, **result})
        status['candidates'] = len(accepted)
        if status['screened'] % 10 == 0:
            atomic_json(root / 'curation_status.json', status)
        if len(accepted) >= goal:
            break
    cache.close()
    if len(accepted) < needed:
        status.update(phase='insufficient_candidates', needed=needed)
        atomic_json(root / 'curation_status.json', status)
        raise ValueError('Not enough detected candidates; preserve partial cache and broaden the pool')
    # Round-robin strata avoids a batch filled only with easy central traffic.
    strata = defaultdict(list)
    for record in accepted:
        strata[record['curation_bucket']].append(record)
    selected = []
    used_phashes = []
    used_hashes = set()
    if prior.exists():
        db = sqlite3.connect(prior)
        old_records = [json.loads(r[0]) for r in db.execute('SELECT record_json FROM images')]
        used_phashes = [int(r['phash'],16) for r in old_records]
        used_hashes = {r['source_sha256'] for r in old_records}
        db.close()
    while len(selected) < needed:
        if not any(strata.values()):
            raise ValueError('Not enough distinct candidate photos; expand screening before publication')
        for name in sorted(strata):
            if strata[name] and len(selected) < needed:
                record = strata[name].pop(0)
                perceptual = int(record['phash'],16)
                if record['source_sha256'] in used_hashes or any((perceptual^h).bit_count()<=4 for h in used_phashes):
                    continue
                selected.append(record)
                used_phashes.append(perceptual)
                used_hashes.add(record['source_sha256'])
    atomic_json(root / ('selected-' + str(target) + '.json'), {'recipe': RECIPE,
        'checkpoint_sha256': CHECKPOINT_SHA, 'records': selected, 'target_total': target,
        'existing_images_preserved': len(existing), 'selection_is_not_ground_truth': True})
    status.update(phase='screened', selected=len(selected), target_total=target,
                  bucket_counts=dict(Counter(r['curation_bucket'] for r in selected)))
    atomic_json(root / 'curation_status.json', status)
    return status


def publish(data_root, target=500):
    """Append verified candidates; never overwrite a label or old batch."""
    from .lead_review import LeadReview, digest, encoded
    from .quality import phash, read_rgb
    data_root = Path(data_root).resolve()
    store = LeadReview(data_root)
    selection = json.loads((store.root / ('selected-' + str(target) + '.json')).read_text())
    if selection['recipe'] != RECIPE or selection['checkpoint_sha256'] != CHECKPOINT_SHA:
        raise ValueError('Unrecognized curation recipe')
    registry = sqlite3.connect((data_root / 'metadata.sqlite').as_uri() + '?mode=ro', uri=True)
    registry.row_factory = sqlite3.Row
    protected = list(registry.execute("SELECT source,session_id,sha256,phash FROM images WHERE split IN ('val','test')"))
    protected_sessions = {(r['source'],r['session_id']) for r in protected}
    protected_sha = {r['sha256'] for r in protected}
    protected_phash = [int(r['phash'],16) for r in protected if r['phash']]
    db = store.db()
    try:
        batch_id = 'initial-' + str(target) + '-' + digest(selection)[:16]
        previous = db.execute('SELECT receipt_json FROM batches WHERE batch_id=?', (batch_id,)).fetchone()
        if previous:
            return json.loads(previous[0])
        existing_rows = list(db.execute('SELECT * FROM images'))
        existing = {r['image_id']: json.loads(r['record_json']) for r in existing_rows}
        sessions = {(r['source'],r['session_id']):r['split'] for r in existing.values()}
        known_hashes = {r['source_sha256'] for r in existing.values()}
        existing_phash = [int(r['phash'],16) for r in existing.values()]
        proposed = []
        for item in selection['records']:
            if item['image_id'] in existing:
                continue
            authoritative = registry.execute('SELECT * FROM images WHERE image_id=?', (item['image_id'],)).fetchone()
            if (not authoritative or authoritative['split'] != 'train'
                    or authoritative['source'] != item['source'] or authoritative['session_id'] != item['session_id']
                    or authoritative['sha256'] != item['source_sha256'] or authoritative['pool'] != 'commercial'
                    or authoritative['status'] != 'curated' or authoritative['review_status']=='rejected'
                    or authoritative['synthetic'] or authoritative['license_id'] != item['license_id']):
                raise ValueError('Candidate no longer matches the authoritative source registry')
            session = (item['source'], item['session_id'])
            if session in protected_sessions or session in sessions or item['source_sha256'] in protected_sha | known_hashes:
                raise ValueError('Candidate overlaps existing evaluation, source bytes or recording')
            path = (data_root / item['image_source']).resolve()
            if not path.is_relative_to(data_root) or hash_file(path) != item['source_sha256']:
                raise ValueError('Source curation bytes changed')
            rgb = read_rgb(path)
            h, w = rgb.shape[:2]
            perceptual = phash(rgb)
            if any((perceptual ^ v).bit_count() <= 4 for v in protected_phash + existing_phash):
                raise ValueError('Candidate is a near duplicate; rescreen with stronger deduplication')
            if w != item['width'] or h != item['height'] or not item['has_forward_vehicle_candidate']:
                raise ValueError('Candidate dimensions or vehicle evidence changed')
            # Stable session assignment permits growth without moving old labels.
            split = 'val' if int(hashlib.sha256(('lead-task-v1:' + item['session_id']).encode()).hexdigest()[:8],16)%5 == 0 else 'train'
            image = 'images/' + item['image_id'] + path.suffix.lower()
            dest = store.root / image
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copy2(path, dest)
            if hash_file(dest) != item['source_sha256']:
                raise ValueError('Frozen lead image differs from original')
            record = {k:v for k,v in item.items() if k not in ('image_source','has_forward_vehicle_candidate')}
            record.update(image=image, phash=f'{perceptual:016x}', split=split, batch_id=batch_id,
                          policy='lead-vehicle-visible-box-v1', curation_recipe=RECIPE,
                          detector={'model':'LibreRTMDett','checkpoint_sha256':CHECKPOINT_SHA,
                                    'license':'Apache-2.0','independent_inference':True},
                          label_status='unreviewed', candidates_are_labels=False)
            proposed.append(record)
            sessions[session] = split
            known_hashes.add(item['source_sha256'])
            existing_phash.append(perceptual)
        if len(existing)+len(proposed) != target:
            raise ValueError('Batch must contain exactly the requested cumulative total')
        # Carry actual source licence text with this separate task.
        (store.root / 'licenses').mkdir(exist_ok=True)
        shutil.copy2(data_root / 'licenses/comma10k-LICENSE.txt', store.root / 'licenses/comma10k-LICENSE.txt')
        atomic_json(store.root / ('batch-' + batch_id + '.json'), proposed)
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT COUNT(*) FROM images').fetchone()[0] != len(existing):
            raise ValueError('Lead collection grew while publishing; retry from the new size')
        db.executemany('INSERT INTO images(image_id,record_json,fingerprint) VALUES(?,?,?)',
                       [(r['image_id'],encoded(r),digest(r)) for r in proposed])
        result = {'batch_id':batch_id,'total':target,'added':len(proposed),'sources':dict(Counter(r['source'] for r in proposed)),
                  'splits':dict(Counter(r['split'] for r in proposed)),
                  'curation_buckets':dict(Counter(r['curation_bucket'] for r in proposed)),
                  'distinct_recording_sessions':len(sessions),'human_labels_created':0,'training_started':False,
                  'selection':'detected vehicle candidates; lead identity requires human boxes'}
        db.execute('INSERT INTO batches VALUES(?,?)',(batch_id,encoded(result)))
        db.commit()
        atomic_json(store.root/'curation_receipt.json',result)
        return result
    finally:
        db.close()
        registry.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',type=Path,required=True)
    parser.add_argument('--stage',choices=('pool','screen','publish','count'),required=True)
    parser.add_argument('--target',type=int,default=500)
    parser.add_argument('--weights',type=Path)
    args = parser.parse_args()
    if args.target<1:
        parser.error('Target must be positive')
    if args.stage=='count':
        from .lead_review import LeadReview
        result=LeadReview(args.data_root).status()['counts']['total']
    elif args.stage=='screen':
        if not args.weights:
            parser.error('--weights is required for screening')
        result=screen(args.data_root,args.weights,args.target)
    elif args.stage=='pool':
        result=prepare_pool(args.data_root)
    else:
        result=publish(args.data_root,args.target)
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
