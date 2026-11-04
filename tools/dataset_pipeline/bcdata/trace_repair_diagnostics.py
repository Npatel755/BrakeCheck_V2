"""Stage individual source-backed drafts for human review, without publication.

This deliberately does not use the bulk repair installer or its audit cohorts.
Every staged photo stays flagged and keeps its original conversion group and
warnings. A real human decision applies to that photo alone.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sqlite3
import time

from .apply_trace_repairs import verify
from .bootstrap import atomic_json
from .local import local_settings
from .recheck_flagged_conversions import digest, load_paint, protected, snapshot
from .repair_trace_backlog import dependencies
from .review_store import ReviewStore
from .source_target_evidence import measure as measure_native
from .source_tip_evidence import VERSION as TIP_VERSION, measure as measure_tip
from .source_shared_order_evidence import VERSION as ORDER_VERSION, measure as measure_shared_order
from .source_trace_refinement import retains_physical_traces
from .teachers.base import file_sha256

POLICY = 'individual-native-tip-diagnostics-v1'
MAXIMUM = 20
MODULE = Path(__file__).resolve()
SEALED_FILES = ('report.json', 'selection.json', 'backlog.jsonl', 'controls.jsonl')


def _seal(settings, generation):
    return {'module_path': str(MODULE), 'module_sha256': file_sha256(MODULE),
            'generation_path': str(generation),
            'generation_sha256': {name: file_sha256(generation / name) for name in SEALED_FILES},
            'code_sha256': dependencies(), 'config_sha256': digest(settings.cfg)}


def _tip_candidate(row):
    image = row['input']['image']
    tip = row.get('tip_evidence') or {}
    return (row.get('eligible') is True and image['split'] == 'train'
            and image['source'] == 'a2d2' and image['status'] == 'curated'
            and tip.get('eligible') is True and tip.get('version') == TIP_VERSION)


def _candidate(row, mechanism):
    if mechanism == 'tip':
        return _tip_candidate(row)
    if mechanism != 'shared-order':
        raise ValueError('Unknown diagnostic mechanism')
    image = row['input']['image']
    order = row.get('shared_order_evidence') or {}
    return (row.get('eligible') is True and image['split'] == 'train'
            and image['source'] == 'a2d2' and image['status'] == 'curated'
            and order.get('eligible') is True and order.get('version') == ORDER_VERSION)


def _current(con, row, settings):
    image_id = row['image_id']
    check = con.execute('SELECT * FROM source_conversion_checks WHERE image_id=?', (image_id,)).fetchone()
    if (not check or check['state'] != 'flagged' or protected(con, image_id)
            or snapshot(con, check, settings) != row['input']
            or row['input']['group'].get('state') == 'blocked'):
        return False
    # A later approval must not double as a vote for any existing cohort.
    if con.execute('''SELECT 1 FROM source_validation_groups g,json_each(g.sample_ids) s
        WHERE s.value=? LIMIT 1''', (image_id,)).fetchone():
        return False
    # Do not replace a different diagnostic draft already awaiting its reviewer.
    return not con.execute('''SELECT 1 FROM conversion_diagnostic_samples
        WHERE image_id=? AND geometry_id=? AND source_id=?''',
        (image_id, check['geometry_id'], check['source_id'])).fetchone()


def _fingerprints(row):
    previous = row['input']
    fingerprints = row['fingerprints']
    for key in ('geometry', 'source'):
        annotation, check = previous[key], previous['check']
        if (annotation['id'] != check[key + '_id'] or annotation['path'] != check[key + '_path']
                or fingerprints.get(annotation['path']) != check[key + '_sha256']):
            raise ValueError('Incomplete or moved source/geometry fingerprint')
    params = json.loads(previous['source']['params'] or '{}')
    if params.get('human') is not True:
        raise ValueError('Diagnostic source must be human authored')
    if params.get('palette_path') and fingerprints.get(params['palette_path']) != params.get('palette_sha256'):
        raise ValueError('Incomplete pinned palette fingerprint')
    if any(file_sha256(path) != expected for path, expected in fingerprints.items()):
        raise ValueError('Source, geometry, palette or comparison bytes changed')


def _revalidate(row, generation, mechanism='tip'):
    if mechanism not in ('tip', 'shared-order'):
        raise ValueError('Unknown diagnostic mechanism')
    _fingerprints(row)
    path = Path(row['candidate_path']).resolve()
    if not path.is_relative_to(generation / 'geometry') or file_sha256(path) != row['candidate_sha256']:
        raise ValueError('Sealed diagnostic draft changed or escaped generation')
    candidate = json.loads(path.read_text(encoding='utf-8'))
    previous = row['input']
    if (candidate.get('image_id') != row['image_id'] or candidate.get('version') != row['candidate_version']
            or (candidate.get('width'), candidate.get('height')) !=
               (previous['image']['width'], previous['image']['height'])):
        raise ValueError('Diagnostic image identity or dimensions changed')
    original = json.loads(Path(previous['geometry']['path']).read_text(encoding='utf-8'))
    paint = load_paint(previous['source'])[0]
    if paint.shape != (previous['image']['height'], previous['image']['width']):
        raise ValueError('Diagnostic source mask dimensions changed')
    if not retains_physical_traces(original, candidate, paint):
        raise ValueError('Diagnostic draft lost an original physical boundary')
    native = measure_native(candidate, paint)
    tip = measure_tip(candidate, paint, native) if row.get('tip_evidence') else None
    order = measure_shared_order(candidate, paint, native) if row.get('shared_order_evidence') else None
    if (native.get('eligible_for_fresh_audit') is not True
            or row.get('evidence', {}).get('eligible_for_fresh_audit') is not True
            or (mechanism == 'tip' and not tip) or (mechanism == 'shared-order' and not order)
            or tip != row.get('tip_evidence') or order != row.get('shared_order_evidence')):
        raise ValueError('Fresh native coverage or source proof receipt differs')
    proof = {'candidate_path': str(path), 'candidate_sha256': row['candidate_sha256'],
             'native_target_evidence': native, 'tip_evidence': tip}
    if order:
        proof['shared_order_evidence'] = order
    return proof


def _balanced(rows, limit):
    """Deterministic round-robin across distinct recording sessions."""
    sessions = defaultdict(list)
    for row in rows:
        sessions[row['input']['image']['session_id']].append(row)
    for values in sessions.values():
        values.sort(key=lambda row: digest(POLICY + row['image_id']))
    order = sorted(sessions, key=lambda session: digest(POLICY + session))
    selected = []
    while len(selected) < limit:
        added = False
        for session in order:
            if sessions[session]:
                selected.append(sessions[session].pop(0))
                added = True
                if len(selected) == limit:
                    break
        if not added:
            break
    return selected


def prepare(settings, generation, output, limit=MAXIMUM, mechanism='tip'):
    """Write a reviewable plan; do not change registry or sealed evaluation."""
    if type(limit) is not int or not 1 <= limit <= MAXIMUM:
        raise ValueError('Diagnostic limit must be between 1 and 20')
    if mechanism not in ('tip', 'shared-order'):
        raise ValueError('Unknown diagnostic mechanism')
    generation, output = Path(generation).resolve(), Path(output).resolve()
    if output == generation or output.is_relative_to(generation):
        raise ValueError('Diagnostic output must be outside the sealed generation')
    seal = _seal(settings, generation)
    report, records, _, _, control_evidence = verify(settings, generation)
    if report['code_sha256'] != seal['code_sha256']:
        raise ValueError('Sealed evaluation code differs')
    con = sqlite3.connect(settings.db_path, timeout=60)
    con.row_factory = sqlite3.Row
    try:
        eligible = [row for row in records if _candidate(row, mechanism) and _current(con, row, settings)]
        selected = _balanced(eligible, limit)
        details = []
        for row in selected:
            proof = _revalidate(row, generation, mechanism)
            details.append({'image_id': row['image_id'], 'session_id': row['input']['image']['session_id'],
                            'record_sha256': digest(row), 'proof': proof})
    finally:
        con.close()
    if seal != _seal(settings, generation):
        raise ValueError('Diagnostic module or sealed evaluation changed while planning')
    batch = POLICY + ':' + digest({'seal': seal, 'selected': details, 'mechanism': mechanism})[:20]
    plan = {'policy': POLICY, 'batch_id': batch, 'seal': seal, 'maximum_samples': limit,
            'mechanism': mechanism,
            'eligible_train_candidates': len(eligible), 'selected': details,
            'sessions': len({item['session_id'] for item in details}),
            'human_control_evidence': control_evidence,
            'meaning': 'Individual diagnostic drafts only; each stays flagged until an actual human decision.',
            'human_decisions_created': 0, 'cohorts_created': 0, 'cohorts_released': 0,
            'training_held': True, 'created_at': time.time()}
    output.mkdir(parents=True, exist_ok=False)
    atomic_json(output / 'plan.json', plan)
    return plan


def _controls_unchanged(con, controls):
    # Existing human examples are evidence, never new votes. Recheck the real
    # tip-route controls without applying the bulk route's publication gate.
    for row in controls:
        variants = [row] + row.get('alternative_controls', [])
        if not any(item.get('eligible') is True and ((item.get('tip_evidence') or {}).get('eligible') is True
                   or (item.get('shared_order_evidence') or {}).get('eligible') is True)
                   for item in variants):
            continue
        image = con.execute('SELECT * FROM images WHERE image_id=?', (row['image_id'],)).fetchone()
        human = con.execute("SELECT * FROM annotations WHERE image_id=? AND kind='human' ORDER BY created_at DESC,id DESC LIMIT 1",
                            (row['image_id'],)).fetchone()
        if (not image or not image['locked']
                or image['review_status'] != row['input']['image']['review_status']
                or not human or dict(human) != row['input']['human']
                or not con.execute('SELECT 1 FROM reviews WHERE image_id=?', (row['image_id'],)).fetchone()
                or any(file_sha256(path) != expected for path, expected in row['fingerprints'].items())):
            raise ValueError('Actual human tip control changed')


def install(settings, output):
    """Install only the prepared individual drafts, retaining all warning holds."""
    output = Path(output).resolve()
    plan_path = output / 'plan.json'
    plan_sha = file_sha256(plan_path)
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    mechanism = plan.get('mechanism', 'tip')
    if mechanism not in ('tip', 'shared-order'):
        raise ValueError('Unknown diagnostic mechanism')
    if plan.get('policy') != POLICY or (output / 'receipt.json').exists():
        raise ValueError('Wrong diagnostic policy or already installed plan')
    generation = Path(plan['seal']['generation_path']).resolve()
    if output == generation or output.is_relative_to(generation) or plan['seal'] != _seal(settings, generation):
        raise ValueError('Diagnostic plan, module or sealed generation changed')
    report, records, controls, _, _ = verify(settings, generation)
    by_id = {row['image_id']: row for row in records}
    selected = plan['selected']
    if (len(selected) > MAXIMUM or len(selected) > plan['maximum_samples']
            or len({item['image_id'] for item in selected}) != len(selected)):
        raise ValueError('Invalid diagnostic selection')
    rows = []
    for item in selected:
        row = by_id.get(item['image_id'])
        if not row or not _candidate(row, mechanism) or digest(row) != item['record_sha256']:
            raise ValueError('Diagnostic selection differs from sealed TRAIN tip candidates')
        rows.append((row, item))
    identity = {'seal': plan['seal'], 'selected': selected}
    if 'mechanism' in plan:
        identity['mechanism'] = mechanism
    expected_batch = POLICY + ':' + digest(identity)[:20]
    if plan['batch_id'] != expected_batch or report['code_sha256'] != plan['seal']['code_sha256']:
        raise ValueError('Diagnostic plan identity changed')
    store = ReviewStore(settings)
    backup = store.backup()
    con = sqlite3.connect(settings.db_path, timeout=60)
    con.row_factory = sqlite3.Row
    installed, skipped = [], []
    try:
        con.execute('BEGIN IMMEDIATE')
        # execute statements individually: executescript would commit the lock.
        for statement in _SCHEMA_STATEMENTS:
            con.execute(statement)
        if con.execute('SELECT 1 FROM trace_repair_diagnostic_batches WHERE batch_id=?', (plan['batch_id'],)).fetchone():
            raise ValueError('Diagnostic batch already installed')
        _controls_unchanged(con, controls)
        now = time.time()
        for row, item in rows:
            image_id, previous = row['image_id'], row['input']
            if not _current(con, row, settings):
                skipped.append({'image_id': image_id, 'reason': 'review_claim_deferral_or_binding_changed'})
                continue
            proof = _revalidate(row, generation, mechanism)
            if proof != item['proof']:
                raise ValueError('Fresh diagnostic source or tip proof changed')
            annotation = previous['geometry']
            cursor = con.execute('''INSERT INTO annotations
                (image_id,kind,producer,model_version,license_ids,path,params,created_at)
                VALUES (?,'geometry','geometry',?,?,?,?,?)''',
                (image_id, row['candidate_version'], annotation['license_ids'], proof['candidate_path'],
                 json.dumps({'policy': POLICY, 'diagnostic_only': True, 'previous_geometry_id': annotation['id'],
                             'plan_path': str(plan_path), 'plan_sha256': plan_sha}), now))
            geometry_id = cursor.lastrowid
            diagnostics = json.loads(previous['check']['diagnostics'])
            # Keep current reasons verbatim. Candidate evidence never clears QC.
            diagnostics['diagnostic_only'] = True
            diagnostics['requires_individual_human_review'] = True
            diagnostics['repair_diagnostic'] = {
                'policy': POLICY, 'batch_id': plan['batch_id'], 'diagnostic_only': True,
                'requires_individual_human_review': True, 'generation_report_sha256': plan['seal']['generation_sha256']['report.json'],
                'module_sha256': plan['seal']['module_sha256'], 'plan_path': str(plan_path),
                'candidate_remaining_reasons': row.get('remaining_reasons', []),
                'candidate_quality': row.get('quality'), **proof}
            con.execute('''UPDATE source_conversion_checks SET geometry_id=?,geometry_version=?,geometry_path=?,
                geometry_sha256=?,diagnostics=?,created_at=? WHERE image_id=?''',
                (geometry_id, row['candidate_version'], proof['candidate_path'], row['candidate_sha256'],
                 json.dumps(diagnostics), now, image_id))
            con.execute('INSERT INTO conversion_diagnostic_samples VALUES (?,?,?,?,?,?)',
                        (plan['batch_id'], image_id, 'a2d2:native-' + ('distal-tip' if mechanism == 'tip' else 'shared-order'),
                         geometry_id, previous['source']['id'], now))
            replacement = {'geometry_id': geometry_id, 'geometry_path': proof['candidate_path'],
                           'geometry_sha256': row['candidate_sha256'], 'state': 'flagged',
                           'batch_id': previous['check']['batch_id'], 'group_key': previous['check']['group_key'],
                           'diagnostics': diagnostics}
            con.execute('INSERT INTO trace_repair_diagnostic_history VALUES (?,?,?,?,?)',
                        (image_id, plan['batch_id'], json.dumps(previous), json.dumps(replacement), now))
            con.execute('INSERT INTO review_versions VALUES (?,1) ON CONFLICT(image_id) DO UPDATE SET revision=revision+1',
                        (image_id,))
            installed.append(image_id)
        if installed:
            con.execute("UPDATE local_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        receipt = {'policy': POLICY, 'batch_id': plan['batch_id'], 'seal': plan['seal'], 'plan_sha256': plan_sha,
                   'mechanism': mechanism,
                   'installed': len(installed), 'sample_ids': installed, 'skipped': skipped, 'backup': backup,
                   'state': 'flagged', 'diagnostic_only': True, 'human_decisions_created': 0,
                   'cohorts_created': 0, 'cohorts_released': 0, 'training_held': True, 'created_at': now}
        con.execute('INSERT INTO trace_repair_diagnostic_batches VALUES (?,?,?)',
                    (plan['batch_id'], json.dumps(receipt), now))
        for row, _ in rows:
            if row['image_id'] in installed:
                _fingerprints(row)
                if file_sha256(row['candidate_path']) != row['candidate_sha256']:
                    raise ValueError('Diagnostic candidate bytes changed during installation')
        _controls_unchanged(con, controls)
        if file_sha256(plan_path) != plan_sha or _seal(settings, generation) != plan['seal']:
            raise ValueError('Diagnostic plan, module or sealed generation changed during installation')
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    atomic_json(output / 'receipt.json', receipt)
    return receipt


# Kept separate from executescript so schema creation participates in the same
# transaction as geometry binding, diagnostic queue insertion and history.
_SCHEMA_STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS trace_repair_diagnostic_history (
        image_id TEXT NOT NULL,batch_id TEXT NOT NULL,previous_state TEXT NOT NULL,
        replacement TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(image_id,batch_id))''',
    '''CREATE TABLE IF NOT EXISTS trace_repair_diagnostic_batches (
        batch_id TEXT PRIMARY KEY,receipt TEXT NOT NULL,created_at REAL NOT NULL)''',
    """CREATE TRIGGER IF NOT EXISTS trace_diagnostic_history_no_update BEFORE UPDATE ON trace_repair_diagnostic_history
        BEGIN SELECT RAISE(ABORT,'Immutable diagnostic history'); END""",
    """CREATE TRIGGER IF NOT EXISTS trace_diagnostic_history_no_delete BEFORE DELETE ON trace_repair_diagnostic_history
        BEGIN SELECT RAISE(ABORT,'Immutable diagnostic history'); END""",
    """CREATE TRIGGER IF NOT EXISTS trace_diagnostic_batches_no_update BEFORE UPDATE ON trace_repair_diagnostic_batches
        BEGIN SELECT RAISE(ABORT,'Immutable diagnostic receipt'); END""",
    """CREATE TRIGGER IF NOT EXISTS trace_diagnostic_batches_no_delete BEFORE DELETE ON trace_repair_diagnostic_batches
        BEGIN SELECT RAISE(ABORT,'Immutable diagnostic receipt'); END""",
)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generation', type=Path, help='Sealed evaluation; required when creating a plan')
    parser.add_argument('--output', type=Path, required=True, help='New diagnostic plan folder, outside evaluation')
    parser.add_argument('--limit', type=int, default=MAXIMUM)
    parser.add_argument('--mechanism', choices=('tip', 'shared-order'), default='tip')
    parser.add_argument('--install', action='store_true', help='Install an existing plan as individual flagged examples')
    args = parser.parse_args()
    if args.install:
        result = install(local_settings(), args.output)
    elif args.generation:
        result = prepare(local_settings(), args.generation, args.output, args.limit, args.mechanism)
    else:
        parser.error('--generation is required to create a plan')
    print(json.dumps(result), flush=True)
