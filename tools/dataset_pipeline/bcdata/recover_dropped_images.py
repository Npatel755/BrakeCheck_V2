"""Prepare recovered source images, then publish every fresh audit together.

Training stays held. Original photographs, source labels, human decisions and
split assignments are preserved. Recovery requires a sealed native-image
comparison; similarity hashes alone cannot certify a duplicate.
"""
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
import argparse
import json
import os
from pathlib import Path
import sqlite3
import time

import cv2

from .bootstrap import atomic_json
from .db import Registry
from .geometry import extract_lanes
from .lanes import params_for
from .licenses import LicenseRegistry
from .local import local_settings
from . import fusion, qc
from .import_audi import _human_or_claim, _load_cpu_teacher
from .paint_proposals import generate_candidate, NAME, VERSION as MODEL_VERSION
from .recheck_flagged_conversions import digest, unchanged_spans
from .recovery_source_maps import RecoverySourceTeacher, classes_and_maps
from .review_store import ReviewStore
from .source_converter import repair_geometry
from .source_converter_v3 import repair_geometry_v3, risk_diagnostics
from .source_converter_v4 import repair_geometry_v4
from .source_converter_a2d2_v2 import repair_geometry as repair_audi
from .source_fragment_continuation import repair_geometry as extend_fragment
from .source_validation import classify, a2d2_conversion_reasons, select_samples, a2d2_strata, strata
from .teachers.base import run_teacher, file_sha256

POLICY = 'verified-dropped-recovery-v1'
SETTINGS = REGISTRY = LICENSES = TEACHER = CODE = None


def initialize(data_root, code):
    global SETTINGS, REGISTRY, LICENSES, TEACHER, CODE
    os.environ['HF_HUB_OFFLINE'] = '1'
    SETTINGS = local_settings(data_root)
    SETTINGS.cfg['teachers'].update(device='cpu', amp=False)
    SETTINGS.cfg['teachers']['comma_segnet']['revision'] = 'b642c614eb0737fe7befa229f47fbd0245033038'
    cv2.setNumThreads(1)
    REGISTRY = Registry(SETTINGS.db_path)
    LICENSES = LicenseRegistry(SETTINGS.licenses_path)
    TEACHER = _load_cpu_teacher(SETTINGS)
    CODE = code


def convert_native(original, maps, source, settings):
    params = params_for(settings)
    geo = extract_lanes(maps['lane'], params, maps.get('my_car'), maps.get('movable'))
    risk = risk_diagnostics(geo)
    if risk['risky']:
        geo = repair_geometry_v3(geo, maps['lane'], maps.get('my_car'), maps.get('movable'))
        if risk_diagnostics(geo)['risky']:
            geo['source_converter']['warnings'] = ['unresolved_dash_association']
    else:
        geo = repair_geometry(geo, maps['lane'])
    geo = repair_geometry_v4(geo, maps['lane'], maps.get('my_car'), maps.get('movable'))
    if source == 'a2d2':
        geo = repair_audi(geo, maps['lane'], maps.get('my_car'), maps.get('movable'), params=params,
                          solid_mask=maps.get('solid'), dashed_mask=maps.get('dashed'))
    extended, evidence = extend_fragment(geo, maps['lane'], solid=maps.get('solid'), dashed=maps.get('dashed'))
    if evidence and unchanged_spans(geo, extended):
        from tools.evaluate_source_v4 import crossing_checks
        before = {tuple(item['slots']): item['crossing_rows'] for item in crossing_checks(geo)}
        no_new_crossings = all(item['crossing_rows'] <= before.get(tuple(item['slots']), 0) for item in crossing_checks(extended))
        old_issues = qc.check(geo, {'mode': 'source_label'}, maps['lane'], None, settings, source=source)[1]
        new_issues = qc.check(extended, {'mode': 'source_label'}, maps['lane'], None, settings, source=source)[1]
        old_coverage, old_support = qc.mask_polyline_consistency(geo, maps['lane'])
        new_coverage, new_support = qc.mask_polyline_consistency(extended, maps['lane'])
        if (no_new_crossings and not {i['code'] for i in new_issues} - {i['code'] for i in old_issues}
                and new_coverage >= old_coverage and new_support >= old_support):
            geo = extended
    return geo


def prepare_one(proof):
    image_id = proof['image_id']
    try:
        row = REGISTRY.image(image_id)
        if dict(row) != proof['previous_image'] or row['status'] != 'dropped' or row['locked'] or _human_or_claim(REGISTRY, image_id):
            return {'image_id': image_id, 'skipped': 'image_changed_or_human_history'}
        source = REGISTRY.annotation(image_id, 'source_label')
        if not source or json.loads(source['params']).get('human') is not True:
            raise ValueError('Original human source label is missing')
        source = dict(source)
        source_sha = file_sha256(source['path'])
        classes, maps = classes_and_maps(source)
        if classes.shape != (row['height'], row['width']):
            raise ValueError('Source dimensions differ from the original photo')
        params = json.loads(source['params'])
        if params.get('palette_path') and file_sha256(params['palette_path']) != params.get('palette_sha256'):
            raise ValueError('Original palette changed')
        run_teacher(SETTINGS, REGISTRY, LICENSES, RecoverySourceTeacher(SETTINGS, REGISTRY), [row])
        generate_candidate(SETTINGS, REGISTRY, TEACHER, row)
        fusion.fuse_image(SETTINGS, REGISTRY, row, [NAME])
        fused = REGISTRY.annotation(image_id, 'fused', 'fusion')
        version = POLICY + ':' + digest({'source': source_sha, 'code': CODE, 'config': SETTINGS.cfg})[:20]
        path = SETTINGS.path('lane_geometry', 'dropped_recovery', image_id + '-' + version.rsplit(':', 1)[1] + '.json')
        existing = REGISTRY.annotation(image_id, 'geometry', 'geometry', version)
        if existing and path.is_file():
            geo = json.loads(path.read_text())
        else:
            geo = convert_native(None, maps, row['source'], SETTINGS)
            geo.update(image_id=image_id, version=version, fused_version=fused['model_version'], mode='source_label')
            atomic_json(path, geo)
            REGISTRY.add_annotation(image_id, 'geometry', 'geometry', version, str(path),
                                    license_ids=json.loads(fused['license_ids']),
                                    params={'recovery_policy': POLICY, 'source_sha256': source_sha, 'code_sha256': CODE})
        score, issues = qc.check(geo, {'mode': 'source_label'}, maps['lane'], None, SETTINGS, source=row['source'])
        REGISTRY.set_qc(image_id, score, qc.tier_for(score, issues, 'source_label', SETTINGS), 1 - score, issues, False, version)
        teacher_path = SETTINGS.path('lane_geometry', 'teachers', NAME, image_id + '.json')
        teacher = json.loads(teacher_path.read_text())
        if teacher.get('teacher_version') != MODEL_VERSION or teacher.get('source_guided') is not False:
            raise ValueError('Independent model comparison is missing or changed')
        reasons = classify(geo, [issue['code'] for issue in issues], teacher)
        if row['source'] == 'a2d2':
            reasons = sorted(set(reasons + a2d2_conversion_reasons(geo)))
        risks = []
        # This exact pattern was approved unchanged in 83/84 frozen human
        # controls. It still requires a separate fresh human sample audit.
        if row['source'] == 'a2d2' and reasons == ['model_source_count_disagreement'] and not (classes == 2).any():
            risks, reasons = reasons, []
        geometry = dict(REGISTRY.annotation(image_id, 'geometry', 'geometry', version))
        quality = dict(REGISTRY.conn.execute('SELECT * FROM qc WHERE image_id=?', (image_id,)).fetchone())
        assigned_split = row['split']
        if assigned_split is None:
            known = [item[0] for item in REGISTRY.conn.execute('SELECT DISTINCT split FROM images WHERE session_id=? AND split IS NOT NULL', (row['session_id'],))]
            if len(known) > 1:
                raise ValueError('Conflicting recording-session splits')
            from .expand_dashcam import split_for
            assigned_split = known[0] if known else split_for(row['session_id'])
        stratum_row = {**dict(row), 'split': assigned_split}
        return {'image_id': image_id, 'previous_image': proof['previous_image'], 'duplicate_comparison': proof['comparison'] if 'comparison' in proof else {},
                'source': source, 'source_sha256': source_sha, 'geometry': geometry,
                'geometry_sha256': file_sha256(path), 'quality': quality, 'reasons': reasons, 'audit_risks': risks,
                'stratum': a2d2_strata(stratum_row, geo) if row['source'] == 'a2d2' else strata(stratum_row, geo),
                'assigned_split': assigned_split,
                'lane_count': sum(bool(line.get('slot')) for line in geo['lanes']),
                'teacher_path': str(teacher_path), 'teacher_sha256': file_sha256(teacher_path)}
    except Exception as error:
        return {'image_id': image_id, 'error': type(error).__name__ + ': ' + str(error)}


def publish(settings, directory, records):
    backup = ReviewStore(settings).backup()
    reg = Registry(settings.db_path)
    ready, skipped = [], []
    batch = POLICY + ':' + digest({'generation': str(directory), 'ids': sorted(row['image_id'] for row in records)})[:16]
    try:
        with reg.tx() as con:
            con.execute('BEGIN IMMEDIATE')
            for result in records:
                if 'error' in result or 'skipped' in result:
                    continue
                image_id = result['image_id']
                row = reg.image(image_id)
                if dict(row) != result['previous_image'] or row['locked'] or _human_or_claim(reg, image_id):
                    skipped.append({'image_id': image_id, 'reason': 'image_or_review_changed'})
                    continue
                if con.execute('SELECT 1 FROM source_conversion_checks WHERE image_id=?', (image_id,)).fetchone():
                    skipped.append({'image_id': image_id, 'reason': 'already_has_conversion_check'})
                    continue
                quality = con.execute('SELECT * FROM qc WHERE image_id=?', (image_id,)).fetchone()
                if not quality or dict(quality) != result['quality'] or quality['geometry_version'] != result['geometry']['model_version']:
                    skipped.append({'image_id': image_id, 'reason': 'prepared_quality_changed'})
                    continue
                for name in ('source', 'geometry'):
                    annotation = reg.annotation(image_id, 'source_label' if name == 'source' else 'geometry', None if name == 'source' else 'geometry')
                    if not annotation or dict(annotation) != result[name] or file_sha256(annotation['path']) != result[name + '_sha256']:
                        raise ValueError('Prepared annotation changed before publication: ' + image_id)
                if file_sha256(row['path']) != row['sha256'] or file_sha256(result['teacher_path']) != result['teacher_sha256']:
                    raise ValueError('Original photo or independent comparison changed')
                ready.append(result)
            groups = defaultdict(list)
            by_id = {row['image_id']: row for row in ready}
            for row in ready:
                if not row['reasons']:
                    image = row['previous_image']
                    risk = ':model-disagrees' if row['audit_risks'] else ''
                    groups[f"{image['source']}:{row['stratum']}{risk}"].append(row['image_id'])
            cohorts = {}
            for key, ids in groups.items():
                ordered = sorted(ids, key=lambda image_id: digest(POLICY + image_id))
                for offset in range(0, len(ordered), 250):
                    cohorts[f'{batch}:{key}:part-{offset // 250}'] = ordered[offset:offset + 250]
            samples = select_samples(cohorts, minimum_per_group=3, maximum_per_group=3,
                                     sample_strata={row['image_id']: row['previous_image']['session_id'] for row in ready}, policy=POLICY)
            sample_ids = {image_id for ids in samples.values() for image_id in ids}
            group_for = {image_id: key for key, ids in cohorts.items() for image_id in ids}
            now = time.time()
            con.execute('''CREATE TABLE IF NOT EXISTS dropped_image_recovery_history (
                image_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,previous_image TEXT NOT NULL,
                recovery_evidence TEXT NOT NULL,created_at REAL NOT NULL)''')
            for key, ids in cohorts.items():
                con.execute('INSERT INTO source_validation_groups VALUES (?,?,?,?,?,?,?)',
                            (key, batch, key.removeprefix(batch + ':'), json.dumps(samples[key]), 'awaiting_audit',
                             json.dumps({'policy': POLICY, 'members': len(ids), 'requires_fresh_human_audit': True,
                                         'duplicate_report': str(directory / 'report.json')}), now))
            counts = Counter()
            for result in ready:
                image_id = result['image_id']
                state = 'flagged' if result['reasons'] else 'sample' if image_id in sample_ids else 'awaiting_audit'
                geometry, source = result['geometry'], result['source']
                key = group_for.get(image_id, batch + ':flagged:' + result['stratum'])
                diagnostics = {'policy': POLICY, 'reasons': result['reasons'], 'audit_risks': result['audit_risks'],
                               'stratum': result['stratum'], 'duplicate_comparison': result['duplicate_comparison'],
                               'requires_fresh_human_audit': True}
                con.execute('INSERT INTO source_conversion_checks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (image_id, batch, key, state, geometry['id'], geometry['model_version'], geometry['path'], result['geometry_sha256'],
                             source['id'], source['path'], result['source_sha256'], json.dumps(diagnostics), now))
                con.execute('INSERT INTO dropped_image_recovery_history VALUES (?,?,?,?,?)',
                            (image_id, batch, json.dumps(result['previous_image']), json.dumps(diagnostics), now))
                assigned_split = result.get('assigned_split') or result['previous_image']['split']
                if assigned_split not in ('train', 'val', 'test'):
                    raise ValueError('Recovered image needs a recording-session split')
                if con.execute('SELECT 1 FROM images WHERE session_id=? AND split IS NOT NULL AND split<>? LIMIT 1',
                               (result['previous_image']['session_id'], assigned_split)).fetchone():
                    raise ValueError('Recovered image would cross recording-session splits')
                con.execute("UPDATE images SET status='curated',reject_reason=NULL,has_source_label=1,split=?,updated_at=? WHERE image_id=?", (assigned_split, now, image_id))
                con.execute('INSERT INTO review_versions VALUES (?,1) ON CONFLICT(image_id) DO UPDATE SET revision=revision+1', (image_id,))
                counts[state] += 1
            if ready:
                con.execute("UPDATE local_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        result = {'policy': POLICY, 'batch_id': batch, 'restored': len(ready), 'counts': dict(counts), 'groups': len(cohorts),
                  'sample_ids': sorted(sample_ids), 'skipped': skipped, 'backup': backup,
                  'human_decisions_created': 0, 'training_held': True, 'finished_at': time.time()}
        atomic_json(directory / ('published-' + time.strftime('%Y%m%d-%H%M%S') + '.json'), result)
        atomic_json(directory / 'published.json', result)
        atomic_json(settings.path('reports', 'dropped_image_recovery.json'), result)
        return result
    finally:
        reg.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--generation', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--source', choices=('a2d2', 'comma10k'))
    parser.add_argument('--retry-errors', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    directory = args.generation.resolve()
    report = json.loads((directory / 'report.json').read_text())
    for filename, key in (('selection.json', 'selection_sha256'), ('comparisons.jsonl', 'comparisons_sha256')):
        if file_sha256(directory / filename) != report[key]:
            raise ValueError('Sealed duplicate comparison changed')
    selection = json.loads((directory / 'selection.json').read_text())
    if any(file_sha256(path) != expected for path, expected in selection['code_sha256'].items()):
        raise ValueError('Duplicate-verification code changed after the native-image comparison')
    proofs = [json.loads(line) for line in (directory / 'comparisons.jsonl').read_text().splitlines()]
    selected = [proof for proof in proofs if proof.get('recover') and 'error' not in proof]
    if args.retry_errors:
        previous_results = [json.loads(line) for line in (directory / 'prepared.jsonl').read_text().splitlines()]
        failures = {row['image_id'] for row in previous_results if row.get('error')}
        selected = [proof for proof in selected if proof['image_id'] in failures]
    if args.source:
        selected = [proof for proof in selected if proof['previous_image']['source'] == args.source]
    if args.limit:
        selected = selected[:args.limit]
    settings = local_settings()
    code = {str(Path(__file__)): file_sha256(__file__),
            str(Path(__file__).with_name('recovery_source_maps.py')): file_sha256(Path(__file__).with_name('recovery_source_maps.py'))}
    status = {'phase': 'preparing_source_conversions', 'pid': os.getpid(), 'total': len(selected),
              'prepared': 0, 'errors': 0, 'training_held': True, 'started_at': time.time()}
    records = []
    output = directory / ('prepared-retry.jsonl' if args.retry_errors else
                          'prepared-pilot-' + (args.source or 'all') + '.jsonl' if args.limit else 'prepared.jsonl')
    with output.open('w', encoding='utf-8') as stream:
        with ProcessPoolExecutor(max_workers=args.workers, initializer=initialize, initargs=(str(settings.root), code)) as pool:
            for result in pool.map(prepare_one, selected, chunksize=1):
                records.append(result)
                stream.write(json.dumps(result) + '\n'); stream.flush()
                status.update(prepared=len(records), errors=sum('error' in row for row in records), updated_at=time.time())
                if len(records) % 20 == 0 or len(records) == len(selected):
                    atomic_json(directory / 'recovery_progress.json', status)
                    print(json.dumps(status), flush=True)
    if args.prepare_only:
        status.update(phase='prepared_only')
    else:
        status.update(phase='creating_all_audits')
        atomic_json(directory / 'recovery_progress.json', status)
        published = publish(settings, directory, records)
        status.update(phase='complete', published=published)
    atomic_json(directory / 'recovery_progress.json', status)
    print(json.dumps(status), flush=True)


if __name__ == '__main__':
    from filelock import FileLock
    with FileLock(str(local_settings().path('logs', 'dropped-recovery.lock')), timeout=0):
        main()
