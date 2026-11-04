"""Replace width-biased warning holds with native-run evidence and fresh audits.

Geometry and original QC warnings are retained. Every source run must already
be represented by a curve inside its native extent. Failed audit cohorts,
human decisions, deferred photos and reviewer claims cannot be bypassed.
"""
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
import argparse
import json
import os
from pathlib import Path
import sqlite3
import time

import cv2

from .bootstrap import atomic_json
from .local import local_settings
from .recheck_flagged_conversions import digest, load_paint, protected, snapshot
from .review_store import ReviewStore
from .source_run_evidence import SOFT, audit_candidate
from .source_validation import select_samples
from .teachers.base import file_sha256

POLICY = 'native-run-supported-conversion-audit-v1'


def code_hashes():
    package = Path(__file__).resolve().parent
    names = ('audit_supported_conversions.py', 'source_run_evidence.py', 'recheck_flagged_conversions.py',
             'source_validation.py', 'qc.py', 'geometry.py', 'sources/comma10k.py', 'quality.py')
    return {str(package / name): file_sha256(package / name) for name in names}


def examine(record):
    cv2.setNumThreads(1)
    result = {'image_id': record['image']['image_id'], 'input': record}
    try:
        for key in ('geometry', 'source'):
            if file_sha256(record[key]['path']) != record['check'][key + '_sha256']:
                raise ValueError('Bound source or geometry changed')
        geometry = json.loads(Path(record['geometry']['path']).read_text())
        reasons = json.loads(record['check']['diagnostics']).get('reasons', [])
        paint, _, _ = load_paint(record['source'])
        if paint.shape != (record['image']['height'], record['image']['width']):
            raise ValueError('Source dimensions changed')
        evidence = audit_candidate(geometry, paint, reasons)
        result.update(eligible=evidence is not None, evidence=evidence)
        if evidence and record['human']:
            from tools.evaluate_a2d2_repair_candidate import source_supported_raster_score
            human = json.loads(Path(record['human']['path']).read_text())
            result['human_sha256'] = file_sha256(record['human']['path'])
            result['source_supported_human_agreement'] = source_supported_raster_score(geometry, human, paint)
        return result
    except Exception as error:
        return {**result, 'eligible': False, 'error': type(error).__name__ + ': ' + str(error)}


def prepare(settings, directory, workers=6):
    directory.mkdir(parents=True, exist_ok=False)
    con = sqlite3.connect(settings.db_path, timeout=60)
    con.row_factory = sqlite3.Row
    backlog, controls = [], []
    deps = code_hashes()
    try:
        for check in con.execute('SELECT c.* FROM source_conversion_checks c JOIN images i USING(image_id) WHERE i.status=\'curated\' ORDER BY c.image_id').fetchall():
            reasons = json.loads(check['diagnostics']).get('reasons', [])
            if not reasons or not set(reasons) <= SOFT:
                continue
            row = con.execute('SELECT * FROM images WHERE image_id=?', (check['image_id'],)).fetchone()
            record = snapshot(con, check, settings)
            geometry = json.loads(Path(check['geometry_path']).read_text())
            if set(geometry.get('source_converter', {}).get('warnings', [])) - {'unassigned_source_fragment_requires_review'}:
                continue
            if row['locked'] and row['review_status'] in ('approved', 'corrected') and record['human']:
                controls.append(record)
            elif check['state'] == 'flagged' and not protected(con, row['image_id']) and record['group'].get('state') != 'blocked':
                backlog.append(record)
    finally:
        con.close()
    selection = {'backlog': backlog, 'controls': controls, 'code_sha256': deps, 'config_sha256': digest(settings.cfg)}
    atomic_json(directory / 'selection.json', selection)
    status = {'phase': 'checking_native_source_runs', 'pid': os.getpid(), 'total': len(backlog),
              'control_total': len(controls), 'processed': 0, 'controls_processed': 0,
              'eligible': 0, 'errors': 0, 'started_at': time.time()}
    results, control_results = [], []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for label, inputs, destination in [('controls', controls, control_results), ('backlog', backlog, results)]:
            status['phase'] = label
            with (directory / (label + '.jsonl')).open('w', encoding='utf-8') as output:
                for result in pool.map(examine, inputs, chunksize=8):
                    destination.append(result)
                    output.write(json.dumps(result) + '\n'); output.flush()
                    status.update(processed=len(results), controls_processed=len(control_results),
                                  eligible=sum(row['eligible'] for row in results),
                                  errors=sum('error' in row for row in results + control_results), updated_at=time.time())
                    if (len(results) + len(control_results)) % 100 == 0:
                        atomic_json(directory / 'progress.json', status)
                        print(json.dumps(status), flush=True)
    if deps != code_hashes():
        raise ValueError('Native-run evidence code changed during evaluation')
    comparisons = [row for row in control_results if row.get('eligible')]
    disagreement = [row['image_id'] for row in comparisons
                    if row['source_supported_human_agreement']['fp'] or row['source_supported_human_agreement']['fn']]
    report = {**status, 'phase': 'prepared', 'policy': POLICY, 'code_sha256': deps,
              'config_sha256': selection['config_sha256'],
              'selection_sha256': file_sha256(directory / 'selection.json'),
              'backlog_sha256': file_sha256(directory / 'backlog.jsonl'),
              'controls_sha256': file_sha256(directory / 'controls.jsonl'),
              'eligible_past_human_controls': len(comparisons), 'past_lane_disagreements': disagreement,
              'approved_past_controls': sum(row['input']['image']['review_status'] == 'approved' for row in comparisons),
              'geometry_changes': 0, 'human_decisions_created': 0}
    atomic_json(directory / 'report.json', report)
    atomic_json(directory / 'progress.json', report)
    return report


def publish(settings, directory):
    report = json.loads((directory / 'report.json').read_text())
    if (report['code_sha256'] != code_hashes() or report['config_sha256'] != digest(settings.cfg)
            or report['errors'] or report['past_lane_disagreements'] or report['eligible_past_human_controls'] < 10):
        raise ValueError('Native-run audit evidence is changed, incomplete, or disagrees with past human labels')
    for filename, key in [('selection.json', 'selection_sha256'), ('backlog.jsonl', 'backlog_sha256'), ('controls.jsonl', 'controls_sha256')]:
        if file_sha256(directory / filename) != report[key]:
            raise ValueError('Frozen evaluation bytes changed')
    if (directory / 'applied.json').exists():
        raise ValueError('Already applied')
    candidates = [json.loads(line) for line in (directory / 'backlog.jsonl').read_text().splitlines()]
    candidates = [row for row in candidates if row['eligible']]
    backup = ReviewStore(settings).backup()
    con = sqlite3.connect(settings.db_path, timeout=60)
    con.row_factory = sqlite3.Row
    ready, skipped = [], []
    batch = POLICY + ':' + file_sha256(directory / 'report.json')[:16]
    try:
        con.execute('BEGIN IMMEDIATE')
        for row in candidates:
            check = con.execute('SELECT * FROM source_conversion_checks WHERE image_id=?', (row['image_id'],)).fetchone()
            if (not check or protected(con, row['image_id']) or snapshot(con, check, settings) != row['input']
                    or row['input']['group'].get('state') == 'blocked'):
                skipped.append(row['image_id']); continue
            for key in ('geometry', 'source'):
                if file_sha256(row['input'][key]['path']) != row['input']['check'][key + '_sha256']:
                    raise ValueError('Source or geometry bytes changed after evaluation')
            ready.append(row)
        groups = defaultdict(list)
        for row in ready:
            previous = row['input']
            geometry = json.loads(Path(previous['geometry']['path']).read_text())
            count = sum(bool(line.get('slot')) for line in geometry['lanes'])
            lighting = json.loads(previous['image']['tags'] or '{}').get('lighting', {}).get('value', 'unknown')
            cause = ','.join(json.loads(previous['check']['diagnostics']).get('reasons', []))
            key = f"{previous['image']['source']}:{previous['image']['split']}:{lighting}:lanes-{count}:cause-{cause}"
            groups[key].append(row['image_id'])
        cohorts = {}
        for key, ids in groups.items():
            ids.sort(key=lambda image_id: digest(POLICY + image_id))
            for offset in range(0, len(ids), 100):
                cohorts[f'{batch}:{key}:part-{offset // 100}'] = ids[offset:offset + 100]
        samples = select_samples(cohorts, minimum_per_group=3, maximum_per_group=3,
                                 sample_strata={row['image_id']: row['input']['image']['session_id'] for row in ready}, policy=POLICY)
        sample_ids = {image_id for ids in samples.values() for image_id in ids}
        group_for = {image_id: key for key, ids in cohorts.items() for image_id in ids}
        now = time.time()
        con.execute('''CREATE TABLE IF NOT EXISTS native_run_audit_history (
            image_id TEXT NOT NULL,batch_id TEXT NOT NULL,previous_check TEXT NOT NULL,
            evidence TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(image_id,batch_id))''')
        for key, ids in cohorts.items():
            con.execute('INSERT INTO source_validation_groups VALUES (?,?,?,?,?,?,?)',
                        (key, batch, key.removeprefix(batch + ':'), json.dumps(samples[key]), 'awaiting_audit',
                         json.dumps({'policy': POLICY, 'members': len(ids), 'report_path': str(directory / 'report.json'),
                                     'geometry_unchanged': True, 'requires_fresh_human_audit': True}), now))
        counts = Counter()
        for row in ready:
            state = 'sample' if row['image_id'] in sample_ids else 'awaiting_audit'
            previous = row['input']['check']
            diagnostics = {**json.loads(previous['diagnostics']), 'reasons': [],
                           'audit_risks': json.loads(previous['diagnostics']).get('reasons', []),
                           'native_source_run_evidence': row['evidence'], 'policy': POLICY,
                           'previous_group': previous['group_key'], 'requires_fresh_human_audit': True}
            con.execute('INSERT INTO native_run_audit_history VALUES (?,?,?,?,?)',
                        (row['image_id'], batch, json.dumps(previous), json.dumps(row['evidence']), now))
            con.execute('UPDATE source_conversion_checks SET batch_id=?,group_key=?,state=?,diagnostics=?,created_at=? WHERE image_id=?',
                        (batch, group_for[row['image_id']], state, json.dumps(diagnostics), now, row['image_id']))
            con.execute('INSERT INTO review_versions VALUES (?,1) ON CONFLICT(image_id) DO UPDATE SET revision=revision+1', (row['image_id'],))
            counts[state] += 1
        if ready:
            con.execute("UPDATE local_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        con.commit()
    except BaseException:
        con.rollback(); raise
    finally:
        con.close()
    applied = {'policy': POLICY, 'batch_id': batch, 'converted_to_audit': len(ready), 'counts': dict(counts),
               'groups': len(cohorts), 'sample_ids': sorted(sample_ids), 'skipped': skipped,
               'backup': backup, 'geometry_changes': 0, 'human_decisions_created': 0}
    atomic_json(directory / 'applied.json', applied)
    return applied


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--generation', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    settings = local_settings()
    result = publish(settings, args.generation.resolve()) if args.apply else prepare(settings, args.generation.resolve(), args.workers)
    print(json.dumps(result), flush=True)
