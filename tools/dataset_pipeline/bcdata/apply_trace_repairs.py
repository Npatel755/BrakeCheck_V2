"""Install sealed, regression-checked native trace repairs behind fresh audits."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sqlite3
import time

from .bootstrap import atomic_json
from .local import local_settings
from .repair_trace_backlog import POLICY, dependencies
from .recheck_flagged_conversions import digest, protected, snapshot, load_paint
from .review_store import ReviewStore
from .source_trace_refinement import retains_slot_traces, retains_physical_traces
from .source_validation import select_samples
from .teachers.base import file_sha256


def route_key(row):
    tip = row.get('tip_evidence') or {}
    order = row.get('shared_order_evidence') or {}
    if order.get('eligible') is True and order.get('version') == 'native-shared-paint-order-v1':
        if tip.get('eligible') is True and tip.get('version') == 'native-distal-tip-evidence-v1':
            return 'native-distal-tip-evidence-v1+native-shared-paint-order-v1'
        return 'native-shared-paint-order-v1'
    if tip.get('eligible') is True and tip.get('version') == 'native-distal-tip-evidence-v1':
        return 'native-distal-tip-evidence-v1'
    mechanism = (row.get('refinement') or {}).get('version', 'native-trace')
    # The existing reconstruction/refinement path sometimes has a wrapper
    # and sometimes returns its trace evidence directly. It is one algorithm.
    return mechanism if mechanism == 'native-foreground-order-v1' else 'native-trace'


def supported_sources(controls, minimum=20):
    by_source = defaultdict(list)
    seen = set()
    for row in controls:
        variants = [row] + [{**row, **item} for item in row.get('alternative_controls', [])]
        for variant in variants:
            key = (row['input']['image']['source'], row['image_id'], route_key(variant))
            if variant.get('eligible') and key not in seen:
                by_source[row['input']['image']['source']].append(variant)
                seen.add(key)
    evidence, supported = {}, set()
    for source, rows in by_source.items():
        agreement = sum(row['human_comparison']['candidate']['fp'] == 0
                        and row['human_comparison']['candidate']['fn'] == 0 for row in rows)
        regressions = [row['image_id'] for row in rows
                       if row['input']['image']['review_status'] == 'approved' and row.get('regression')]
        approved = sum(row['input']['image']['review_status'] == 'approved' for row in rows)
        # Evidence from an established route cannot certify a new ordering
        # fallback. Teacher disagreement is a separate human-audit stratum,
        # not a different source-only reconstruction algorithm.
        routes = defaultdict(list)
        for row in rows:
            routes[route_key(row)].append(row)
        route_evidence = {}
        for key, members in routes.items():
            matches = sum(row['human_comparison']['candidate']['fp'] == 0
                          and row['human_comparison']['candidate']['fn'] == 0 for row in members)
            approved_count = sum(row['input']['image']['review_status'] == 'approved' for row in members)
            failures = [row['image_id'] for row in members
                        if row['input']['image']['review_status'] == 'approved' and row.get('regression')]
            route_evidence[key] = {
                'qualifying_controls': len(members), 'approved_controls': approved_count,
                'source_supported_human_matches': matches,
                'source_supported_match_fraction': matches / len(members),
                'approved_regressions': failures,
                'eligible_for_fresh_audits': len(members) >= minimum and approved_count >= 10
                    and matches / len(members) >= .95 and not failures}
        passed = any(item['eligible_for_fresh_audits'] for item in route_evidence.values())
        evidence[source] = {'qualifying_controls': len(rows), 'approved_controls': approved,
                            'distinct_human_controls': len({row['image_id'] for row in rows}),
                            'source_supported_human_matches': agreement,
                            'source_supported_match_fraction': agreement / len(rows),
                            'approved_regressions': regressions, 'eligible_for_fresh_audits': passed,
                            'route_evidence': route_evidence}
        if passed:
            supported.add(source)
    return supported, evidence


def verify(settings, directory):
    report = json.loads((directory / 'report.json').read_text())
    if (report.get('policy') != POLICY or report['code_sha256'] != dependencies()
            or report['config_sha256'] != digest(settings.cfg) or report['errors']):
        raise ValueError('Incomplete, changed or unsuccessful trace repair evaluation')
    for filename, key in [('selection.json', 'selection_sha256'), ('backlog.jsonl', 'backlog_sha256'),
                          ('controls.jsonl', 'controls_sha256')]:
        if file_sha256(directory / filename) != report[key]:
            raise ValueError('Sealed evaluation bytes changed: ' + filename)
    selection = json.loads((directory / 'selection.json').read_text())
    records = [json.loads(line) for line in (directory / 'backlog.jsonl').read_text().splitlines()]
    controls = [json.loads(line) for line in (directory / 'controls.jsonl').read_text().splitlines()]
    for rows, key in ((records, 'backlog'), (controls, 'controls')):
        expected = {row['image']['image_id']: row for row in selection[key]}
        if len(rows) != len(expected) or {row['image_id'] for row in rows} != set(expected):
            raise ValueError('Incomplete or duplicate evaluation selection')
        if any(row['input'] != expected[row['image_id']] or row.get('error') for row in rows):
            raise ValueError('Evaluation differs from frozen inputs')
    if sum(row['input']['image']['review_status'] == 'approved' for row in controls) < 100:
        raise ValueError('At least 100 real approved controls must be evaluated')
    supported, source_evidence = supported_sources(controls)
    return report, records, controls, supported, source_evidence


def apply(settings, directory):
    directory = Path(directory).resolve()
    if (directory / 'applied.json').exists():
        raise ValueError('Already applied')
    report, records, controls, supported, source_evidence = verify(settings, directory)
    candidates = [row for row in records if row['eligible'] and row['input']['image']['source'] in supported
                  and (not source_evidence.get(row['input']['image']['source'], {}).get('route_evidence')
                       or source_evidence[row['input']['image']['source']]['route_evidence']
                           .get(route_key(row), {}).get('eligible_for_fresh_audits'))]
    backup = ReviewStore(settings).backup()
    con = sqlite3.connect(settings.db_path, timeout=60)
    con.row_factory = sqlite3.Row
    ready, skipped = [], []
    batch = POLICY + ':' + file_sha256(directory / 'report.json')[:16]
    try:
        con.execute('BEGIN IMMEDIATE')
        for control in controls:
            if (not any(item.get('eligible') for item in [control] + control.get('alternative_controls', []))
                    or control['input']['image']['source'] not in supported):
                continue
            previous = control['input']
            image = con.execute('SELECT * FROM images WHERE image_id=?', (control['image_id'],)).fetchone()
            human = con.execute("SELECT * FROM annotations WHERE image_id=? AND kind='human' ORDER BY created_at DESC,id DESC LIMIT 1", (control['image_id'],)).fetchone()
            if (not image or not image['locked'] or image['review_status'] != previous['image']['review_status']
                    or not human or dict(human) != previous['human']
                    or not con.execute('SELECT 1 FROM reviews WHERE image_id=?', (control['image_id'],)).fetchone()
                    or any(file_sha256(path) != expected for path, expected in control['fingerprints'].items())):
                raise ValueError('Actual human regression control changed')
        for row in candidates:
            image_id = row['image_id']
            current = con.execute('SELECT * FROM source_conversion_checks WHERE image_id=?', (image_id,)).fetchone()
            if (not current or current['state'] != 'flagged' or protected(con, image_id)
                    or snapshot(con, current, settings) != row['input']
                    or row['input']['group'].get('state') == 'blocked'):
                skipped.append({'image_id': image_id, 'reason': 'review_or_binding_changed'}); continue
            if any(file_sha256(path) != expected for path, expected in row['fingerprints'].items()):
                raise ValueError('Source, geometry, palette or comparison bytes changed')
            path = Path(row['candidate_path']).resolve()
            if not path.is_relative_to(directory / 'geometry') or file_sha256(path) != row['candidate_sha256']:
                raise ValueError('Immutable trace draft changed')
            candidate = json.loads(path.read_text())
            original = json.loads(Path(row['input']['geometry']['path']).read_text())
            retain = (retains_physical_traces(original, candidate, load_paint(row['input']['source'])[0])
                      if row['input']['image']['source'] == 'a2d2' else retains_slot_traces(original, candidate))
            if (not retain
                    or not row['evidence']['eligible_for_fresh_audit']
                    or candidate['image_id'] != image_id or candidate['version'] != row['candidate_version']):
                raise ValueError('Trace draft lost an original boundary identity or span')
            ready.append(row)
        groups = defaultdict(list)
        sessions = {}
        for row in ready:
            image = row['input']['image']
            candidate = json.loads(Path(row['candidate_path']).read_text())
            count = sum(bool(line.get('slot')) for line in candidate['lanes'])
            lighting = json.loads(image['tags'] or '{}').get('lighting', {}).get('value', 'unknown')
            before = json.loads(row['input']['check']['diagnostics']).get('reasons', [])
            cause = 'identity-warning' if any('slot' in code or 'cross' in code for code in before) else 'coverage-warning'
            mechanism = (route_key(row) if row.get('tip_evidence') or row.get('shared_order_evidence')
                         else (row.get('refinement') or {}).get('version', 'native-trace'))
            risk = 'teacher-disagreement' if row.get('audit_risks') else 'teacher-agrees'
            key = f"{image['source']}:{image['split']}:{lighting}:lanes-{count}:{cause}:{mechanism}:{risk}"
            groups[key].append(row['image_id']); sessions[row['image_id']] = image['session_id']
        cohorts = {}
        for key, ids in groups.items():
            ids.sort(key=lambda image_id: digest(POLICY + image_id))
            for offset in range(0, len(ids), 100):
                cohorts[f'{batch}:{key}:part-{offset // 100}'] = ids[offset:offset + 100]
        samples = select_samples(cohorts, minimum_per_group=3, maximum_per_group=3,
                                 sample_strata=sessions, policy=POLICY)
        sample_ids = {image_id for ids in samples.values() for image_id in ids}
        group_for = {image_id: key for key, ids in cohorts.items() for image_id in ids}
        now = time.time()
        con.execute('''CREATE TABLE IF NOT EXISTS native_trace_repair_history (
            image_id TEXT NOT NULL,batch_id TEXT NOT NULL,previous_state TEXT NOT NULL,
            replacement TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(image_id,batch_id))''')
        for key, ids in cohorts.items():
            con.execute('INSERT INTO source_validation_groups VALUES (?,?,?,?,?,?,?)',
                        (key, batch, key.removeprefix(batch + ':'), json.dumps(samples[key]), 'awaiting_audit',
                         json.dumps({'policy': POLICY, 'members': len(ids), 'maximum_group_size': 100,
                                     'report_path': str(directory / 'report.json'),
                                     'requires_fresh_human_audit': True, 'human_control_evidence': source_evidence}), now))
        counts = Counter()
        for row in ready:
            image_id, previous = row['image_id'], row['input']
            annotation = previous['geometry']
            cursor = con.execute('''INSERT INTO annotations
                (image_id,kind,producer,model_version,license_ids,path,params,created_at)
                VALUES (?,'geometry','geometry',?,?,?,?,?)''',
                (image_id, row['candidate_version'], annotation['license_ids'], row['candidate_path'],
                 json.dumps({'policy': POLICY, 'previous_geometry_id': annotation['id'],
                             'report_path': str(directory / 'report.json')}), now))
            geometry_id = cursor.lastrowid
            state = 'sample' if image_id in sample_ids else 'awaiting_audit'
            diagnostics = {**json.loads(previous['check']['diagnostics']), 'policy': POLICY, 'reasons': [],
                           'audit_risks': json.loads(previous['check']['diagnostics']).get('reasons', []),
                           'native_target_evidence': row['evidence'], 'repair_evidence': row['refinement'],
                           'tip_evidence': row.get('tip_evidence'),
                           'shared_order_evidence': row.get('shared_order_evidence'),
                           'previous_group': previous['check']['group_key'], 'requires_fresh_human_audit': True}
            con.execute('''UPDATE source_conversion_checks SET batch_id=?,group_key=?,state=?,geometry_id=?,
                geometry_version=?,geometry_path=?,geometry_sha256=?,diagnostics=?,created_at=? WHERE image_id=?''',
                (batch, group_for[image_id], state, geometry_id, row['candidate_version'], row['candidate_path'],
                 row['candidate_sha256'], json.dumps(diagnostics), now, image_id))
            quality = row['quality']
            con.execute('''UPDATE qc SET score=?,tier=?,priority=?,issues=?,qc_sample=0,
                geometry_version=?,updated_at=? WHERE image_id=?''',
                (quality['score'], quality['tier'], 1 - quality['score'], json.dumps(quality['issues']),
                 row['candidate_version'], now, image_id))
            con.execute('INSERT INTO native_trace_repair_history VALUES (?,?,?,?,?)',
                        (image_id, batch, json.dumps(previous), json.dumps({'geometry_id': geometry_id,
                         'geometry_path': row['candidate_path'], 'diagnostics': diagnostics, 'state': state}), now))
            con.execute('INSERT INTO review_versions VALUES (?,1) ON CONFLICT(image_id) DO UPDATE SET revision=revision+1', (image_id,))
            counts[state] += 1
        if report['code_sha256'] != dependencies():
            raise ValueError('Repair code changed during installation')
        for row in ready:
            if (any(file_sha256(path) != expected for path, expected in row['fingerprints'].items())
                    or file_sha256(row['candidate_path']) != row['candidate_sha256']):
                raise ValueError('Bound repair bytes changed during installation')
        if ready:
            con.execute("UPDATE local_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        con.commit()
    except BaseException:
        con.rollback(); raise
    finally:
        con.close()
    result = {'policy': POLICY, 'batch_id': batch, 'installed': len(ready), 'groups': len(cohorts),
              'counts': dict(counts), 'sample_ids': sorted(sample_ids), 'skipped': skipped,
              'human_control_evidence': source_evidence, 'backup': backup,
              'human_decisions_created': 0, 'training_held': True, 'created_at': time.time()}
    atomic_json(directory / 'applied.json', result)
    atomic_json(settings.path('reports', 'native_trace_repairs.json'), result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generation', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(apply(local_settings(), args.generation)), flush=True)
