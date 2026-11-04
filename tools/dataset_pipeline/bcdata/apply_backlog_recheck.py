"""Install independently checked backlog drafts behind new three-sample audits."""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import time

from .bootstrap import atomic_json
from .local import local_settings
from .recheck_flagged_conversions import (POLICY, dependencies, digest, protected,
                                         snapshot, unchanged_spans)
from .review_store import ReviewStore
from .source_validation import select_samples
from .teachers.base import file_sha256


def load_verified(settings, directory):
    directory = Path(directory).resolve()
    report = json.loads((directory / 'report.json').read_text())
    selection = json.loads((directory / 'selection.json').read_text())
    if (report.get('policy') != POLICY or report['code_sha256'] != dependencies()
            or report['config_sha256'] != digest(settings.cfg)):
        raise ValueError('Recheck code or configuration changed after evaluation')
    for filename, key in (('selection.json', 'selection_sha256'), ('backlog.jsonl', 'backlog_sha256'),
                          ('past_review_controls.jsonl', 'controls_sha256')):
        if file_sha256(directory / filename) != report[key]:
            raise ValueError('Sealed recheck artifact changed: ' + filename)
    records = [json.loads(line) for line in (directory / 'backlog.jsonl').read_text().splitlines()]
    controls = [json.loads(line) for line in (directory / 'past_review_controls.jsonl').read_text().splitlines()]
    if len(records) != report['rechecked'] or len({row['image_id'] for row in records}) != len(records):
        raise ValueError('Recheck selection is incomplete or duplicated')
    if {row['image_id'] for row in records} != {row['image']['image_id'] for row in selection['backlog']}:
        raise ValueError('Recheck selection no longer matches its frozen inputs')
    expected_controls = {row['image']['image_id']: row for row in selection['past_review_controls']}
    if len(controls) != len(expected_controls) or {row['image_id'] for row in controls} != set(expected_controls):
        raise ValueError('Human control evaluation is incomplete')
    approved = [row for row in controls if row['input']['image']['review_status'] == 'approved']
    if len(approved) < 100 or report['approved_regressions'] or any(row.get('regression') or row.get('error') for row in approved):
        raise ValueError('At least 100 actual approved controls and zero regressions are required')
    if any(row.get('error') for row in controls):
        raise ValueError('Human control evaluation has errors')
    con = sqlite3.connect(settings.db_path, timeout=60)
    con.row_factory = sqlite3.Row
    try:
        for row in controls:
            previous = expected_controls[row['image_id']]
            if row['input'] != previous:
                raise ValueError('Control input differs from the frozen selection')
            image = con.execute('SELECT * FROM images WHERE image_id=?', (row['image_id'],)).fetchone()
            latest_human = con.execute("SELECT * FROM annotations WHERE image_id=? AND kind='human' ORDER BY created_at DESC,id DESC LIMIT 1", (row['image_id'],)).fetchone()
            if (not image or not image['locked'] or image['review_status'] != previous['image']['review_status']
                    or not latest_human or dict(latest_human) != previous['human']
                    or not con.execute('SELECT 1 FROM review_events WHERE image_id=?', (row['image_id'],)).fetchone()):
                raise ValueError('Actual human control changed: ' + row['image_id'])
            if any(not Path(path).is_file() or file_sha256(path) != expected for path, expected in row['fingerprints'].items()):
                raise ValueError('Human evaluation input changed: ' + row['image_id'])
    finally:
        con.close()
    originals = {row['image']['image_id']: row for row in selection['backlog']}
    for row in records:
        if row['input'] != originals[row['image_id']]:
            raise ValueError('Backlog input differs from the frozen selection')
        if row.get('candidate_path'):
            path = Path(row['candidate_path']).resolve()
            if not path.is_relative_to(directory / 'geometry') or file_sha256(path) != row['candidate_sha256']:
                raise ValueError('Immutable candidate changed')
            original = json.loads(Path(row['input']['geometry']['path']).read_text())
            candidate = json.loads(path.read_text())
            if not unchanged_spans(original, candidate):
                raise ValueError('Candidate lost an original boundary span')
            from tools.evaluate_source_v4 import crossing_checks
            old_crossings = {tuple(item['slots']): item['crossing_rows'] for item in crossing_checks(original)}
            if any(item['crossing_rows'] > old_crossings.get(tuple(item['slots']), 0)
                   for item in crossing_checks(candidate)):
                row['installation_skip_reason'] = 'extension_increased_crossing_rows'
    return report, records


def current_record(con, row, settings):
    image_id = row['image_id']
    if row.get('installation_skip_reason'):
        return row['installation_skip_reason']
    if protected(con, image_id):
        return 'reviewed_claimed_or_deferred'
    current = con.execute('SELECT * FROM source_conversion_checks WHERE image_id=?', (image_id,)).fetchone()
    if not current or current['state'] != 'flagged' or dict(current) != row['input']['check']:
        return 'conversion_binding_changed'
    if snapshot(con, current, settings) != row['input']:
        return 'image_annotation_qc_or_group_changed'
    for kind in ('geometry', 'source'):
        annotation = row['input'][kind]
        if annotation['id'] != current[kind + '_id'] or annotation['path'] != current[kind + '_path']:
            return 'stale_' + kind + '_binding'
    if any(not Path(path).is_file() or file_sha256(path) != expected for path, expected in row['fingerprints'].items()):
        return 'input_bytes_changed'
    if row.get('candidate_path') and file_sha256(row['candidate_path']) != row['candidate_sha256']:
        return 'candidate_bytes_changed'
    if row['input']['group'].get('state') == 'blocked' and not row.get('extension') and not row['reasons']:
        return 'failed_group_requires_geometry_fix'
    return None


def assign_groups(records, batch):
    groups, strata = defaultdict(list), {}
    by_id = {row['image_id']: row for row in records}
    for row in records:
        if row['reasons']:
            continue
        image = row['input']['image']
        path = row.get('candidate_path') or row['input']['geometry']['path']
        geometry = json.loads(Path(path).read_text())
        count = sum(bool(line.get('slot')) for line in geometry['lanes'])
        lighting = json.loads(image['tags'] or '{}').get('lighting', {}).get('value', 'unknown')
        cause = 'fragment-extension' if row.get('extension') else 'quality-recheck'
        risk = 'model-disagrees' if row['audit_risks'] else 'source-agrees'
        stratum = f"{image['source']}:{image['split']}:{lighting}:lanes-{count}:{cause}:{risk}"
        groups[stratum].append(row['image_id'])
        # Prefer different recording sessions among the three actual samples.
        strata[row['image_id']] = image['session_id']
    cohorts = {}
    for stratum, ids in groups.items():
        ordered = sorted(ids, key=lambda image_id: digest(POLICY + image_id))
        # A single three-image audit must not release thousands of images.
        for offset in range(0, len(ordered), 250):
            cohorts[f'{batch}:{stratum}:part-{offset // 250}'] = ordered[offset:offset + 250]
    samples = select_samples(cohorts, minimum_per_group=3, maximum_per_group=3,
                             sample_strata=strata, policy=POLICY)
    return cohorts, samples


def model_disagreement_cohorts(records, controls):
    """Learn which disagreement patterns are suitable for sampled review.

    Keep patterns with a poor correction history manual. Even a supported
    pattern gets a fresh audit, never automatic validation.
    """
    outcomes = defaultdict(Counter)

    def key(row):
        path = row.get('candidate_path') or row['input']['geometry']['path']
        geometry = json.loads(Path(path).read_text())
        count = sum(bool(line.get('slot')) for line in geometry['lanes'])
        return (row['input']['image']['source'], count,
                'empty-source' if row['source_paint_pixels'] == 0 else 'paint-present')

    for row in controls:
        if row.get('before_reasons') == ['model_source_count_disagreement'] and not row.get('error'):
            outcomes[key(row)][row['input']['image']['review_status']] += 1
    evidence = []
    supported = set()
    for pattern, counts in sorted(outcomes.items()):
        total = sum(counts.values())
        acceptance = counts['approved'] / total
        eligible = total >= 20 and acceptance >= .95
        if eligible:
            supported.add(pattern)
        evidence.append({'source': pattern[0], 'lane_count': pattern[1], 'paint': pattern[2],
                         'past_decisions': dict(counts), 'observed_unchanged_acceptance': acceptance,
                         'eligible_for_fresh_sample_audit': eligible})
    adjusted = deepcopy(records)
    for row in adjusted:
        if not row.get('error') and row.get('audit_risks') and not row['reasons'] and key(row) not in supported:
            row['reasons'] = list(row['audit_risks'])
    return adjusted, evidence


def apply(settings, directory):
    directory = Path(directory).resolve()
    if (directory / 'applied.json').exists():
        raise ValueError('This recheck generation has already been applied')
    report, records = load_verified(settings, directory)
    controls = [json.loads(line) for line in (directory / 'past_review_controls.jsonl').read_text().splitlines()]
    records, learned_cohorts = model_disagreement_cohorts(records, controls)
    candidates = [row for row in records if 'error' not in row and
                  (row.get('extension') or row['reasons'] != json.loads(row['input']['check']['diagnostics']).get('reasons', []))]
    # A real SQLite/human-label backup precedes every production mutation.
    backup = ReviewStore(settings).backup()
    con = sqlite3.connect(settings.db_path, timeout=60)
    con.row_factory = sqlite3.Row
    ready, skipped = [], []
    try:
        con.execute('BEGIN IMMEDIATE')
        for row in candidates:
            reason = current_record(con, row, settings)
            if reason:
                skipped.append({'image_id': row['image_id'], 'reason': reason})
            else:
                ready.append(row)
        batch = POLICY + ':' + digest({'report': file_sha256(directory / 'report.json'),
                                       'ids': sorted(row['image_id'] for row in ready)})[:16]
        cohorts, samples = assign_groups(ready, batch)
        group_for = {image_id: key for key, ids in cohorts.items() for image_id in ids}
        sample_ids = {image_id for ids in samples.values() for image_id in ids}
        now = time.time()
        con.execute('''CREATE TABLE IF NOT EXISTS source_backlog_recheck_history (
            batch_id TEXT NOT NULL,image_id TEXT NOT NULL,previous TEXT NOT NULL,
            replacement TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(batch_id,image_id))''')
        for key, ids in cohorts.items():
            evidence = {'policy': POLICY, 'requires_fresh_human_audit': True,
                        'report_path': str(directory / 'report.json'), 'report_sha256': file_sha256(directory / 'report.json'),
                        'members': len(ids), 'human_decisions_created': 0}
            con.execute('INSERT INTO source_validation_groups VALUES (?,?,?,?,?,?,?)',
                        (key, batch, key.removeprefix(batch + ':'), json.dumps(samples[key]),
                         'awaiting_audit', json.dumps(evidence), now))
        counts = Counter()
        for row in ready:
            image_id, previous = row['image_id'], row['input']
            ann = previous['geometry']
            geometry_id, version, path, sha = ann['id'], ann['model_version'], ann['path'], previous['check']['geometry_sha256']
            if row.get('candidate_path'):
                version, path, sha = row['candidate_version'], row['candidate_path'], row['candidate_sha256']
                cursor = con.execute('''INSERT INTO annotations
                    (image_id,kind,producer,model_version,license_ids,path,params,created_at)
                    VALUES (?,'geometry','geometry',?,?,?,?,?)''',
                    (image_id, version, ann['license_ids'], path,
                     json.dumps({'policy': POLICY, 'previous_geometry_id': ann['id'],
                                 'report_path': str(directory / 'report.json')}), now))
                geometry_id = cursor.lastrowid
            state = 'flagged' if row['reasons'] else 'sample' if image_id in sample_ids else 'awaiting_audit'
            key = group_for.get(image_id, previous['check']['group_key'])
            diagnostics = {**json.loads(previous['check']['diagnostics']), 'reasons': row['reasons'],
                           'audit_risks': row['audit_risks'], 'policy': POLICY,
                           'previous_group': previous['check']['group_key'],
                           'report_path': str(directory / 'report.json'),
                           'continuation_evidence': row['extension'], 'requires_fresh_human_audit': True}
            quality = row['quality']
            con.execute('''UPDATE qc SET score=?,tier=?,priority=?,issues=?,qc_sample=0,
                geometry_version=?,updated_at=? WHERE image_id=?''',
                (quality['score'], quality['tier'], 1 - quality['score'], json.dumps(quality['issues']), version, now, image_id))
            con.execute('''UPDATE source_conversion_checks SET batch_id=?,group_key=?,state=?,
                geometry_id=?,geometry_version=?,geometry_path=?,geometry_sha256=?,diagnostics=?,created_at=? WHERE image_id=?''',
                (batch, key, state, geometry_id, version, path, sha, json.dumps(diagnostics), now, image_id))
            replacement = {'state': state, 'geometry_id': geometry_id, 'geometry_path': path,
                           'geometry_sha256': sha, 'group_key': key, 'diagnostics': diagnostics}
            con.execute('INSERT INTO source_backlog_recheck_history VALUES (?,?,?,?,?)',
                        (batch, image_id, json.dumps(previous), json.dumps(replacement), now))
            con.execute('INSERT INTO review_versions VALUES (?,1) ON CONFLICT(image_id) DO UPDATE SET revision=revision+1', (image_id,))
            counts[state] += 1
        if ready:
            con.execute("UPDATE local_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        if report['code_sha256'] != dependencies():
            raise ValueError('Recheck dependencies changed during installation')
        # Recheck immutable bytes after staging, immediately before commit.
        for row in ready:
            if any(file_sha256(path) != expected for path, expected in row['fingerprints'].items()):
                raise ValueError('Input bytes changed during installation')
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    applied = {'policy': POLICY, 'batch_id': batch, 'backup': backup,
               'installer_sha256': file_sha256(__file__),
               'applied': len(ready), 'counts': dict(counts), 'groups': len(cohorts),
               'sample_ids': sorted(sample_ids), 'skipped': skipped,
               'geometry_improvements': sum(bool(row['extension']) for row in ready),
               'past_review_disagreement_cohorts': learned_cohorts,
               'human_decisions_created': 0, 'source_files_modified': False,
               'created_at': time.time()}
    atomic_json(directory / 'applied.json', applied)
    atomic_json(settings.path('reports', 'flagged_conversion_recheck.json'), applied)
    return applied


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generation', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(apply(local_settings(), args.generation)), flush=True)
