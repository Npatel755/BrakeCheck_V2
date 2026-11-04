"""Freeze cleared ZOD labels as isolated, train-only UFLDv2 additions.

The ZOD mirror has no trusted recording IDs. Keep its entire source in one
conservative train-only block; never manufacture a frame-level validation split.
Share-alike provenance stays explicit, separate from the commercial-only run.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import shutil
import sqlite3
import uuid

from .bootstrap import atomic_json
from .college_repair_batch import observed_slot_order_ok
from .converted_merge import _has_renderable_span, _lists, _render
from .quality import phash, read_rgb
from .simulation_review import _inside
from .teachers.base import file_sha256
from .ufld_experiment import _base_state, _sample_paths, _verify_sample
from .zod_audit_groups import states
from .zod_clearance import current_approval, same_source_labels
from .zod_mirror import encoded, sha

FORMAT = 'ufld_zod_cleared_v1'
SESSION = 'zod-all-recordings-train-only'


def eligible_rows(db, groups):
    """Return actual approvals plus passed, unchanged source conversions."""
    result = {}
    for row in db.execute("SELECT * FROM frames WHERE image_ready=1 AND action='approve'"):
        if current_approval(row):
            result[row['image_id']] = (dict(row), 'human')
    for key, state in groups.items():
        if state['state'] != 'audited':
            continue
        query = '''SELECT f.* FROM zod_audit_members m INDEXED BY zod_group_members
                   CROSS JOIN frames f ON f.image_id=m.image_id
                   WHERE m.group_key=? AND m.repair_required=0 AND m.geometry_sha=f.geometry_sha
                   AND f.image_ready=1 AND f.correction_json IS NULL AND f.action IS NULL'''
        for row in db.execute(query, (key,)):
            result.setdefault(row['image_id'], (dict(row), 'source_audit'))
    return result


def protected_inventory(data_root, inventory):
    protected = [dict(row) for row in inventory if row['split'] in ('val', 'test')]
    if any(row['source'] in ('zod', 'zod_mirror') for row in protected):
        raise ValueError('ZOD recording identities are unknown; cannot mix it with any existing ZOD evaluation')
    path = Path(data_root) / 'metadata.sqlite'
    if path.exists():
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try:
            for row in protected:
                source = db.execute('SELECT sha256,phash,path FROM images WHERE image_id=?', (row['image_id'],)).fetchone()
                if source:
                    if source['sha256'] != (row.get('source_sha256') or row.get('sha256')):
                        raise ValueError('Protected image identity differs from registry')
                    row['phash'] = source['phash']
                    if not row['phash']:
                        photo = Path(source['path'])
                        if file_sha256(photo) != source['sha256']:
                            raise ValueError('Protected source photo changed')
                        row['phash'] = f'{phash(read_rgb(photo)):016x}'
                elif row.get('image'):
                    photo = _inside(Path(data_root) / 'experiments', row['source'] + '/' + row['image'])
                    if file_sha256(photo) != (row.get('source_sha256') or row.get('sha256')):
                        raise ValueError('Protected imported photo changed')
                    row['phash'] = f'{phash(read_rgb(photo)):016x}'
        finally:
            db.close()
    if any(not row.get('phash') for row in protected):
        raise ValueError('Every protected validation/test image needs its original perceptual hash')
    return protected


def verify_row(row):
    for field in ('native', 'geometry', 'correction'):
        raw = row[field + '_json']
        if raw is not None and sha(raw.encode()) != row[field + '_sha']:
            raise ValueError('ZOD ' + field + ' evidence changed')
    return json.loads(row['correction_json'] or row['geometry_json'])


def freeze_annotation_history(db, target, image_ids):
    """Retain actual human events and model adoption dependencies verbatim."""
    image_ids = set(image_ids)
    events, adoptions = [], []
    for event in db.execute('SELECT * FROM audit_events ORDER BY created_at,event_id'):
        if event['image_id'] not in image_ids:
            continue
        payload = json.loads(event['payload'])
        if payload.get('kind') == 'converter_repair':
            continue
        events.append(dict(event))
        name, requested = payload.get('candidate'), payload.get('candidate_version')
        if not name:
            continue
        proposal = db.execute('SELECT * FROM zod_model_proposals WHERE image_id=? AND model=?',
                              (event['image_id'], name)).fetchone()
        if not proposal or sha(proposal['geometry_json'].encode()) != proposal['geometry_sha']:
            raise ValueError('Adopted model provenance is unavailable')
        frame = db.execute('SELECT native_sha,image_sha FROM frames WHERE image_id=?', (event['image_id'],)).fetchone()
        if proposal['source_binding'] != sha(encoded([frame['native_sha'], frame['image_sha']]).encode()):
            raise ValueError('Adopted model belongs to different source bytes')
        recipe = lambda version: version.split(':continuous-', 1)[0].split(':physical-', 1)[0]
        if not requested or recipe(requested) != recipe(proposal['version']):
            raise ValueError('Historical model adoption cannot be bound to the cached inference recipe')
        receipt = json.loads(proposal['geometry_json']).get('model_receipt')
        if not receipt or not receipt.get('license'):
            raise ValueError('Model adoption is missing its weight/license receipt')
        adoptions.append({'event_id': event['event_id'], 'image_id': event['image_id'],
            'model': name, 'adopted_version': requested, 'cached_version': proposal['version'],
            'source_binding': proposal['source_binding'], 'model_receipt': receipt})
    atomic_json(target / 'zod-human-events.json', events)
    atomic_json(target / 'zod-model-adoptions.json', adoptions)


def build(data_root, base_run, output_run, *, progress=None):
    from .zod_mirror_review import ZodMirrorReview
    import cv2
    cv2.setNumThreads(1)
    data_root, base_run, output_run = map(lambda p: Path(p).resolve(), (data_root, base_run, output_run))
    if output_run.exists() or output_run.is_relative_to(base_run):
        raise ValueError('Use a new isolated output run')
    if progress:
        progress({'format': FORMAT, 'phase': 'verifying_protected_images_and_base',
                  'output_run': str(output_run), 'base_run': str(base_run),
                  'training_started': False, 'auto_promote': False})
    inventory = json.loads((base_run / 'dataset/split_inventory.json').read_text())
    protected = protected_inventory(data_root, inventory)
    base, base_records, marker, _, _, reserved = _base_state(base_run, inventory)
    base_hashes = json.loads((base / 'hashes.json').read_text())
    cache = json.loads((base / 'culane_anno_cache.json').read_text())
    records = list(base_records)
    source_hashes = {r.get('source_sha256') or r.get('sha256') for r in inventory}
    protected_phashes = [int(r['phash'], 16) for r in protected]
    store = ZodMirrorReview(data_root)
    db = store._db()
    db.execute('BEGIN')
    groups = states(db)
    eligible = eligible_rows(db, groups)
    stage = output_run.with_name(output_run.name + '.preparing-' + uuid.uuid4().hex)
    target = stage / 'dataset'
    target.mkdir(parents=True)
    report = {'format': FORMAT, 'phase': 'copying_base', 'output_run': str(output_run),
              'base_run': str(base_run), 'label_candidates': len(eligible), 'accepted_zod': 0,
              'rejected': [], 'test_images_used': 0, 'zod_validation_images': 0,
              'split_policy': SESSION, 'pool': 'share_alike', 'license': 'CC-BY-SA-4.0',
              'auto_promote': False, 'training_started': False, 'human_decisions_created': 0}
    def update(**changes):
        report.update(changes)
        atomic_json(stage / 'preparation.json', report)
        if progress:
            progress(report)
    update()
    try:
        freeze_annotation_history(db, target, eligible)
        verified_base_assets = {}
        for name in base_hashes:
            source, dest = _inside(base, name), _inside(target, name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Frozen sample assets are immutable. Copy metadata because the new
            # manifest/cache are rewritten; never rewrite a linked base file.
            if '/' in name and not name.startswith('list/'):
                try:
                    os.link(source, dest)
                except OSError:
                    shutil.copy2(source, dest)
                    if file_sha256(dest) != base_hashes[name]:
                        raise ValueError('Copied base asset changed: ' + name)
                verified_base_assets[name] = base_hashes[name]
            else:
                shutil.copy2(source, dest)
        atomic_json(target / 'base_manifest.json', base_records)
        atomic_json(target / 'base_hashes.json', base_hashes)
        atomic_json(target / 'reserved_test_ids.json', reserved)
        atomic_json(target / 'split_inventory.json', inventory)
        atomic_json(target / 'protected_inventory.json', protected)
        notice = store.root / 'upstream/README.md'
        if not notice.is_file():
            raise ValueError('Pinned ZOD source notice is required')
        (target / 'licenses').mkdir(exist_ok=True)
        shutil.copy2(notice, target / 'licenses/ZOD-source-README.md')
        update(phase='rendering_cleared_zod')
        group_proofs = {}
        for image_id, (row, origin) in sorted(eligible.items()):
            geometry = verify_row(row)
            rgb_path = _inside(store.root, row['image_path'])
            if file_sha256(rgb_path) != row['image_sha']:
                raise ValueError('ZOD original photo changed')
            if row['image_sha'] in source_hashes:
                report['rejected'].append({'image_id': image_id, 'reason': 'duplicate_source_bytes'})
                continue
            rgb = read_rgb(rgb_path)
            if rgb is None or rgb.shape != (720, 1280, 3):
                raise ValueError('Invalid ZOD source image dimensions')
            perceptual = phash(rgb)
            if any((perceptual ^ held).bit_count() <= 4 for held in protected_phashes):
                report['rejected'].append({'image_id': image_id, 'reason': 'possible_evaluation_near_duplicate'})
                continue
            if not observed_slot_order_ok(geometry) or not _has_renderable_span(geometry):
                report['rejected'].append({'image_id': image_id, 'reason': 'geometry_not_trainable'})
                continue
            if origin == 'human' and not geometry['lanes'] and not geometry.get('no_visible_lane_paint'):
                report['rejected'].append({'image_id': image_id, 'reason': 'unconfirmed_empty_labels'})
                continue
            group_key = db.execute('SELECT group_key FROM zod_audit_members WHERE image_id=?', (image_id,)).fetchone()[0]
            if origin == 'source_audit' and group_key not in group_proofs:
                query = '''SELECT f.* FROM zod_audit_members m JOIN frames f USING(image_id)
                           WHERE m.group_key=? AND m.is_sample=1 ORDER BY f.image_id'''
                samples = [dict(r) for r in db.execute(query, (group_key,))]
                member_count = db.execute('SELECT member_count FROM zod_audit_groups WHERE group_key=?', (group_key,)).fetchone()[0]
                proof = {'group': group_key, 'member_count': member_count, 'samples': samples}
                verify_group(proof)
                group_proofs[group_key] = proof
            image = 'images/train/' + image_id + '.jpg'
            try:
                entry, lanes, _ = _render(rgb, geometry, target, image, human_approved_empty=not geometry['lanes'])
            except ValueError as error:
                report['rejected'].append({'image_id': image_id, 'reason': 'geometry_not_trainable',
                                           'detail': str(error)})
                continue
            cache[image] = entry
            proof_name = 'zod-evidence/' + image_id + '.json'
            atomic_json(target / proof_name, {'row': row, 'clearance': origin, 'group': group_key})
            record = {'image_id': image_id, 'image': image, 'split': 'train', 'source': 'zod_mirror',
                      'session_id': SESSION, 'pool': 'share_alike', 'license': 'CC-BY-SA-4.0',
                      'synthetic': False, 'source_sha256': row['image_sha'], 'label_origin': origin,
                      'decision': 'approved' if origin == 'human' else 'source_validated',
                      'lanes': lanes, 'zod_evidence': proof_name}
            _verify_sample(target, record, entry)
            records.append(record)
            source_hashes.add(row['image_sha'])
            report['accepted_zod'] += 1
            if report['accepted_zod'] % 50 == 0:
                update()
        if not report['accepted_zod']:
            raise ValueError('No cleared, trainable ZOD additions')
        atomic_json(target / 'zod-source-audits.json', group_proofs)
        atomic_json(target / 'manifest.json', records)
        atomic_json(target / 'culane_anno_cache.json', cache)
        _lists(target, records, cache)
        report.update(phase='verified', train_images=sum(r['split'] == 'train' for r in records),
                      val_images=sum(r['split'] == 'val' for r in records))
        atomic_json(target / 'experiment.json', report)
        hashes = {}
        for p in target.rglob('*'):
            if not p.is_file() or p.name in ('hashes.json', 'export_marker.json'):
                continue
            name = p.relative_to(target).as_posix()
            hashes[name] = verified_base_assets.get(name) or file_sha256(p)
        atomic_json(target / 'hashes.json', hashes)
        atomic_json(target / 'export_marker.json', {'format': FORMAT, 'revision': marker['revision'],
                    'errors': [], 'validation': {'ok': True}, 'experimental': True})
        load(stage, _verified_base_assets=verified_base_assets)
        update(phase='prepared')
        stage.rename(output_run)
        return report
    finally:
        db.close()


def verify_group(proof):
    samples = proof['samples']
    if len(samples) != min(3, proof['member_count']) or len({r['image_id'] for r in samples}) != len(samples):
        raise ValueError('Source cohort needs all three distinct samples (all for smaller cohorts)')
    for row in samples:
        geometry = verify_row(row)
        if not current_approval(row) or not same_source_labels(json.loads(row['geometry_json']), geometry):
            raise ValueError('Source cohort sample is stale, edited or not approved')


def load(output_run, *, _verified_base_assets=None):
    """Verify frozen evidence and label files before the GPU worker may load it."""
    target = Path(output_run).resolve() / 'dataset'
    hashes = json.loads((target / 'hashes.json').read_text())
    if not {'manifest.json', 'culane_anno_cache.json', 'zod-source-audits.json',
            'base_manifest.json', 'base_hashes.json', 'protected_inventory.json',
            'reserved_test_ids.json', 'licenses/ZOD-source-README.md',
            'zod-human-events.json', 'zod-model-adoptions.json'} <= hashes.keys():
        raise ValueError('Incomplete ZOD evidence fingerprints')
    for name, digest in hashes.items():
        if _verified_base_assets and _verified_base_assets.get(name) == digest:
            # Used only during construction, after full base byte validation
            # and linking those immutable inodes (or hashing fallback copies).
            # Every later training/resume call rechecks all physical bytes.
            continue
        if file_sha256(_inside(target, name)) != digest:
            raise ValueError('Frozen ZOD training bytes changed: ' + name)
    marker = json.loads((target / 'export_marker.json').read_text())
    if marker.get('format') != FORMAT or marker.get('errors') or not marker.get('validation', {}).get('ok'):
        raise ValueError('Unvalidated ZOD snapshot')
    records = json.loads((target / 'manifest.json').read_text())
    base = json.loads((target / 'base_manifest.json').read_text())
    if records[:len(base)] != base:
        raise ValueError('ZOD preparation changed the base manifest')
    for name, digest in json.loads((target / 'base_hashes.json').read_text()).items():
        if name.startswith(('images/', 'laneseg_label_w16/')) and hashes.get(name) != digest:
            raise ValueError('ZOD preparation changed base sample bytes')
    proofs = json.loads((target / 'zod-source-audits.json').read_text())
    human_events = json.loads((target / 'zod-human-events.json').read_text())
    adopted_events = {e['event_id']: e for e in human_events if json.loads(e['payload']).get('candidate')}
    adoptions = json.loads((target / 'zod-model-adoptions.json').read_text())
    if set(adopted_events) != {a['event_id'] for a in adoptions}:
        raise ValueError('Missing historical model adoption dependencies')
    for adoption in adoptions:
        event = json.loads(adopted_events[adoption['event_id']]['payload'])
        if (adoption['model'] != event['candidate'] or adoption['adopted_version'] != event['candidate_version']
                or adoption['image_id'] != adopted_events[adoption['event_id']]['image_id']
                or not adoption['model_receipt'].get('license')):
            raise ValueError('Model adoption provenance differs from its human event')
    for proof in proofs.values():
        verify_group(proof)
    cache = json.loads((target / 'culane_anno_cache.json').read_text())
    protected = json.loads((target / 'protected_inventory.json').read_text())
    held_ids = set(json.loads((target / 'reserved_test_ids.json').read_text())) | {r['image_id'] for r in protected}
    held_hashes = {r.get('source_sha256') or r.get('sha256') for r in protected}
    ids, paths = set(), set()
    for row in records:
        if row['image_id'] in ids or row['image'] in paths or row['split'] not in ('train', 'val'):
            raise ValueError('Invalid or duplicate training identity')
        ids.add(row['image_id']); paths.add(row['image'])
    for row in records[len(base):]:
        if (row['split'] != 'train' or row['source'] != 'zod_mirror' or row['session_id'] != SESSION
                or row['pool'] != 'share_alike' or row['license'] != 'CC-BY-SA-4.0'
                or row['image_id'] in held_ids or row['source_sha256'] in held_hashes):
            raise ValueError('ZOD addition violates train-only/provenance policy')
        proof = json.loads(_inside(target, row['zod_evidence']).read_text())
        evidence = proof['row']
        geometry = verify_row(evidence)
        if row['image_id'] != evidence['image_id'] or row['source_sha256'] != evidence['image_sha']:
            raise ValueError('ZOD source identity changed')
        for adoption in adoptions:
            if (adoption['image_id'] == row['image_id']
                    and adoption['source_binding'] != sha(encoded([evidence['native_sha'], evidence['image_sha']]).encode())):
                raise ValueError('ZOD model adoption belongs to different source bytes')
        if proof['clearance'] == 'human':
            if not current_approval(evidence):
                raise ValueError('ZOD human approval is not current')
        elif proof['clearance'] == 'source_audit':
            verify_group(proofs[proof['group']])
            if evidence['correction_json'] is not None or evidence['action'] is not None:
                raise ValueError('Source clearance cannot replace an individual review')
        else:
            raise ValueError('Unknown ZOD clearance')
        if not observed_slot_order_ok(geometry):
            raise ValueError('ZOD labels have invalid boundary order')
        _verify_sample(target, row, cache[row['image']])
        for path in _sample_paths(row):
            if path.as_posix() not in hashes:
                raise ValueError('ZOD sample is missing file fingerprints')
    return target, records, marker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--base-run', type=Path, required=True)
    parser.add_argument('--output-run', type=Path, required=True)
    parser.add_argument('--status-report', type=Path)
    args = parser.parse_args()
    status = args.status_report or args.data_root / 'reports/zod_training_preparation.json'
    last_phase = None
    def progress(report):
        nonlocal last_phase
        atomic_json(status, report)
        if report['phase'] != last_phase:
            print(report['phase'], flush=True)
            last_phase = report['phase']
    from filelock import FileLock
    lock = args.data_root / 'logs/zod-training-preparation.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock), timeout=0):
        print(json.dumps(build(args.data_root, args.base_run, args.output_run, progress=progress), indent=2))


if __name__ == '__main__':
    main()
