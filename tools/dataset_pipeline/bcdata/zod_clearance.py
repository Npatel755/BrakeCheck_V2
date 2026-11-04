"""Separate individual human labels from source-converter audit clearance.

Repartitioning is explicit and preserves the previous plan. Machine findings
define risk strata before votes are considered; a corrected sample still cannot
approve unchanged machine labels on its peers.
"""
from collections import Counter, defaultdict
import argparse
import json
from pathlib import Path
import sqlite3
import time

from .zod_mirror import encoded, sha

POLICY = 'zod-cause-separated-audits-v2'
UNVERIFIABLE_SOURCE = {'missing_or_invalid_native_lane_annotations',
                       'invalid_native_paint_shape', 'invalid_or_out_of_bounds_native_polygon'}


def same_source_labels(source, correction):
    from .simulation_review import SimulationReview
    from fastapi import HTTPException
    def lanes(geometry):
        return sorted(SimulationReview._validate_lanes(geometry['lanes'], geometry['width'],
                      geometry['height'], source_metadata=True), key=lambda lane: lane['slot'])
    try:
        return lanes(source) == lanes(correction)
    except (HTTPException, ValueError, TypeError, KeyError):
        return False


def confirmation_is_source_evidence(source, correction, warnings):
    return not (set(warnings) & UNVERIFIABLE_SOURCE) and same_source_labels(source, correction)


def mistaken_confirmation_flags(db):
    """Prove an unchanged confirmation was the only human failure evidence."""
    result = {}
    query = '''SELECT f.*,m.repair_required FROM frames f JOIN zod_audit_members m USING(image_id)
               WHERE f.image_ready=1 AND f.action='approve' AND f.correction_json IS NOT NULL
               AND m.repair_required=1
               AND NOT EXISTS(SELECT 1 FROM zod_conversion_findings c WHERE c.image_id=f.image_id)'''
    events = defaultdict(list)
    for row in db.execute('SELECT image_id,payload FROM audit_events'):
        events[row['image_id']].append(json.loads(row['payload']))
    for row in db.execute(query):
        if not current_approval(row):
            continue
        source, correction = json.loads(row['geometry_json']), json.loads(row['correction_json'])
        if not confirmation_is_source_evidence(source, correction, json.loads(row['qc_json'])):
            continue
        history = events[row['image_id']]
        human_corrections = [e for e in history if e.get('kind') == 'correction']
        if not human_corrections:
            continue
        # An earlier genuine failure remains evidence even after a later save.
        if any(e.get('action') == 'exclude' or e.get('kind') == 'converter_repair' for e in history):
            continue
        if any(not e.get('geometry') or not same_source_labels(source, e['geometry']) for e in human_corrections):
            continue
        result[row['image_id']] = {'revision': row['revision'], 'fingerprint': row['decision_fingerprint'],
            'source_sha': row['geometry_sha'], 'correction_sha': row['correction_sha'],
            'reason': 'Whole-photo confirmation preserved source XY labels; no diagnosed or historical geometry failure.'}
    return result


def repaired_source_flags(db):
    """A completed, valid consolidation needs a new sample gate, not a forever hold."""
    from .college_repair_batch import observed_slot_order_ok
    from .lane_boundary_deduplication import coincident_outer_slots
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='zod_boundary_repair_current'").fetchone()
    if not exists:
        return {}
    result = {}
    for item in db.execute('SELECT image_id,summary_json FROM zod_boundary_repair_current'):
        summary = json.loads(item['summary_json'])
        if not summary.get('source_merged_pairs'):
            continue
        row = db.execute('SELECT * FROM frames WHERE image_id=?', (item['image_id'],)).fetchone()
        source = json.loads(row['geometry_json'])
        info = source.get('boundary_deduplication', {})
        if (not info.get('merged_pairs') or info.get('suspect_pairs')
                or source.get('boundary_completion', {}).get('requires_repair', True)
                or set(json.loads(row['qc_json'])) & UNVERIFIABLE_SOURCE
                or not same_source_labels(source, source)
                or not observed_slot_order_ok(source) or coincident_outer_slots(source['lanes'])):
            continue
        if row['action'] == 'exclude':
            continue
        if row['correction_json'] and not same_source_labels(source, json.loads(row['correction_json'])):
            continue  # A genuinely edited audit sample is still failure evidence.
        finding = db.execute('SELECT finding,origin FROM zod_conversion_findings WHERE image_id=?', (item['image_id'],)).fetchone()
        result[row['image_id']] = {'source_sha': row['geometry_sha'], 'revision': row['revision'],
            'fingerprint': row['decision_fingerprint'], 'risk': 'repaired_paired_boundary',
            'finding_binding': sha(encoded(dict(finding)).encode()) if finding else None,
            'reason': 'Source-supported consolidation passes order/ambiguity checks; sample audit still required.'}
    return result


def bind_legacy_repair_gates(db):
    """Bind initial v2 receipts without clearing any flag or later diagnosis."""
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='zod_source_repair_audit_gates'").fetchone()
    if not exists:
        return 0
    added = 0
    db.execute('BEGIN IMMEDIATE')
    try:
        query = '''SELECT g.image_id,g.proof,f.geometry_sha,c.finding,c.origin
                   FROM zod_source_repair_audit_gates g JOIN frames f USING(image_id)
                   JOIN zod_audit_members m USING(image_id) JOIN zod_conversion_findings c USING(image_id)
                   WHERE m.repair_required=0 AND c.origin='zod-physical-boundaries-v6' '''
        for row in db.execute(query):
            proof = json.loads(row['proof'])
            if 'finding_binding' in proof or proof['source_sha'] != row['geometry_sha']:
                continue
            proof['finding_binding'] = sha(encoded({'finding': row['finding'], 'origin': row['origin']}).encode())
            db.execute('UPDATE zod_source_repair_audit_gates SET proof=? WHERE image_id=?',
                       (encoded(proof), row['image_id']))
            added += 1
        db.commit()
        return added
    except Exception:
        db.rollback()
        raise


def current_approval(row):
    fingerprint = sha(encoded([row['image_id'], row['native_sha'], row['image_sha'],
                              row['geometry_sha'], row['correction_sha']]).encode())
    return bool(row['image_ready'] and row['action'] == 'approve'
                and row['decision_fingerprint'] == fingerprint)


def risk_signature(finding):
    """Only machine diagnostics partition source cohorts, never human votes."""
    if not finding:
        return 'unchanged_source'
    reasons = set()
    for reason in ('boundary_order_conflict', 'conflicting_observations_on_same_row'):
        if reason in finding:
            reasons.add(reason)
    if 'Outer/main boundary consolidation' in finding:
        reasons.add('paired_or_ambiguous_boundary_repair')
    return '+'.join(sorted(reasons)) or 'diagnosed_source_conversion'


def repartition(db):
    """Install a cause-specific audit plan without touching labels or decisions.

    Existing sample identities stay samples. Each new stratum has three samples
    (all members for smaller strata), selected independently of review outcomes.
    Old groups, flags and sample assignments remain in dedicated archive tables.
    """
    from .zod_audit_groups import ensure_plan
    ensure_plan(db)
    db.execute('CREATE TABLE IF NOT EXISTS zod_clearance_plan(policy TEXT PRIMARY KEY,receipt TEXT NOT NULL)')
    prior = db.execute('SELECT receipt FROM zod_clearance_plan WHERE policy=?', (POLICY,)).fetchone()
    if prior:
        return json.loads(prior[0])
    findings = dict(db.execute('SELECT image_id,finding FROM zod_conversion_findings'))
    flag_repairs = mistaken_confirmation_flags(db)
    source_repairs = repaired_source_flags(db)
    descriptions = dict(db.execute('SELECT group_key,description FROM zod_audit_groups'))
    partitions = defaultdict(list)
    for row in db.execute('SELECT * FROM zod_audit_members ORDER BY image_id'):
        risk = (source_repairs[row['image_id']]['risk'] if row['image_id'] in source_repairs
                else risk_signature(findings.get(row['image_id'])))
        partitions[(row['group_key'], risk)].append(dict(row))
    groups, members = [], []
    for (parent, risk), rows in sorted(partitions.items()):
        key = 'cause/' + sha(encoded([POLICY, parent, risk]).encode())[:24]
        selected = {r['image_id'] for r in rows if r['is_sample']}
        for index in (0, len(rows) // 2, len(rows) - 1):
            if len(selected) >= min(3, len(rows)):
                break
            selected.add(rows[index]['image_id'])
        if len(selected) > 3:
            raise ValueError('Existing group has more than three samples; preserve it for explicit migration')
        groups.append((key, descriptions[parent] + '; risk: ' + risk, len(rows)))
        members.extend((r['image_id'], key, r['geometry_sha'], int(r['image_id'] in selected),
                        r['repair_required']) for r in rows)
    # The membership snapshot is rechecked under the writer lock. Concurrent
    # reviews may set repair flags; never replace those with the earlier value.
    db.execute('BEGIN IMMEDIATE')
    try:
        if dict(db.execute('SELECT image_id,finding FROM zod_conversion_findings')) != findings:
            raise ValueError('Machine diagnostics changed while planning')
        live = {r['image_id']: dict(r) for r in db.execute('SELECT * FROM zod_audit_members')}
        if set(live) != {r[0] for r in members}:
            raise ValueError('ZOD membership changed while planning')
        for rows in partitions.values():
            for row in rows:
                if any(live[row['image_id']][key] != row[key] for key in ('group_key', 'geometry_sha', 'is_sample')):
                    raise ValueError('ZOD source/sample plan changed while planning')
        for table in ('zod_audit_groups', 'zod_audit_members', 'zod_audit_plan'):
            db.execute(f'CREATE TABLE {table}_before_cause_v2 AS SELECT * FROM {table}')
        db.execute('CREATE TABLE zod_confirmation_flag_repairs(image_id TEXT PRIMARY KEY,proof TEXT NOT NULL)')
        for image_id, proof in flag_repairs.items():
            row = db.execute('SELECT revision,decision_fingerprint FROM frames WHERE image_id=?', (image_id,)).fetchone()
            if row['revision'] != proof['revision'] or row['decision_fingerprint'] != proof['fingerprint']:
                raise ValueError('Human confirmation changed while planning')
            live[image_id]['repair_required'] = 0
            db.execute('INSERT INTO zod_confirmation_flag_repairs VALUES(?,?)', (image_id, encoded(proof)))
        db.execute('CREATE TABLE zod_source_repair_audit_gates(image_id TEXT PRIMARY KEY,proof TEXT NOT NULL)')
        for image_id, proof in source_repairs.items():
            row = db.execute('SELECT revision,geometry_sha,decision_fingerprint FROM frames WHERE image_id=?', (image_id,)).fetchone()
            if (row['revision'] != proof['revision'] or row['geometry_sha'] != proof['source_sha']
                    or row['decision_fingerprint'] != proof['fingerprint']):
                raise ValueError('Repaired source changed while planning')
            live[image_id]['repair_required'] = 0
            db.execute('INSERT INTO zod_source_repair_audit_gates VALUES(?,?)', (image_id, encoded(proof)))
        db.execute('DELETE FROM zod_audit_members')
        db.execute('DELETE FROM zod_audit_groups')
        db.executemany('INSERT INTO zod_audit_groups VALUES(?,?,?)', groups)
        db.executemany('INSERT INTO zod_audit_members VALUES(?,?,?,?,?)',
                       [(image_id, key, digest, sample, live[image_id]['repair_required'])
                        for image_id, key, digest, sample, _ in members])
        receipt = {'policy': POLICY, 'installed_at': time.time(), 'photos': len(members),
                   'groups': len(groups), 'samples': sum(r[3] for r in members),
                   'previous_groups': len(descriptions),
                   'previous_samples_preserved': sum(r['is_sample'] for r in live.values()),
                   'human_decisions_created': 0, 'geometry_changed': False,
                   'mistaken_confirmation_flags_corrected': len(flag_repairs),
                   'repaired_sources_moved_to_sample_gates': len(source_repairs),
                   'risk_photos': dict(Counter(risk for (parent, risk), rows in partitions.items() for _ in rows))}
        db.execute('INSERT INTO zod_clearance_plan VALUES(?,?)', (POLICY, encoded(receipt)))
        db.commit()
        return receipt
    except Exception:
        db.rollback()
        raise


def clearance_counts(db, groups):
    """Count label clearance, independent of training snapshot admission.

    Explicit current human approvals survive peer holds. Source-only clearance
    needs a passed group and no per-image flag. No skipped or stale decision is
    an approval, and no counts here fabricate a human event.
    """
    counts, approved = Counter(), set()
    query = '''SELECT f.image_id,f.native_sha,f.image_sha,f.geometry_sha,f.correction_sha,
                      f.image_ready,f.action,f.decision_fingerprint,m.repair_required
               FROM frames f JOIN zod_audit_members m USING(image_id)
               WHERE f.image_ready=1 AND f.action='approve' '''
    approved_flags = 0
    for row in db.execute(query):
        if current_approval(row):
            approved.add(row['image_id'])
            approved_flags += int(bool(row['repair_required']))
    counts['individually_approved'] = len(approved)
    for key, group in groups.items():
        if group['state'] != 'audited':
            continue
        query = '''SELECT f.image_id,f.image_ready,f.action FROM zod_audit_members m
                   JOIN frames f USING(image_id) WHERE m.group_key=? AND m.repair_required=0
                   AND m.geometry_sha=f.geometry_sha AND f.correction_json IS NULL'''
        for row in db.execute(query, (key,)):
            if row['image_id'] not in approved and row['image_ready'] and row['action'] not in ('exclude', 'skip'):
                counts['source_audit_cleared'] += 1
    counts['label_cleared'] = len(approved) + counts['source_audit_cleared']
    flagged = db.execute('SELECT COUNT(*) FROM zod_audit_members WHERE repair_required=1').fetchone()[0]
    total = db.execute('SELECT COUNT(*) FROM zod_audit_members').fetchone()[0]
    counts['explicit_repair_photos'] = flagged - approved_flags
    counts['awaiting_source_clearance'] = total - counts['label_cleared'] - counts['explicit_repair_photos']
    return {key: counts[key] for key in ('individually_approved', 'source_audit_cleared',
            'label_cleared', 'explicit_repair_photos', 'awaiting_source_clearance')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    args = parser.parse_args()
    from .zod_active_batch import active_batch
    from .zod_mirror import connect
    root = args.data_root.resolve() / 'experiments/zod_mirror'
    db = connect(active_batch(root))
    try:
        receipt = repartition(db)
        print(json.dumps({**receipt, 'legacy_gate_bindings_added': bind_legacy_repair_gates(db)}, indent=2))
    finally:
        db.close()


if __name__ == '__main__':
    main()
