"""Select three representative repair examples per source/failure mechanism.

These are diagnostic examples, not source-validation audit cohorts. Reviewing
one changes only that photo; related conversions stay held for an actual fix.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import time

from .bootstrap import atomic_json
from .local import local_settings
from .recheck_flagged_conversions import digest, protected
from .review_store import ReviewStore
from .source_validation import select_samples
from .teachers.base import file_sha256

POLICY = 'conversion-repair-diagnostics-v1'


def family(reasons):
    codes = ' '.join(reasons)
    for name, tokens in (
        ('invalid-or-transverse', ('invalid_coordinates', 'transverse_source_promoted')),
        ('crossings-or-shape', ('crossing', 'implausible_curve', 'slot_conflict')),
        ('lane-identity', ('slot_disjoint', 'slot_identity', 'slot_order', 'duplicate_slot', 'ego_slot')),
        ('ambiguous-continuation', ('ambiguous', 'inward_', 'branched', 'unresolved_dash')),
        ('curve-or-extent', ('uncertain_source_curve', 'unresolved_existing', 'retention_uncertain', 'steep')),
        ('paint-without-boundaries', ('no_lanes_with_paint',)),
        ('missing-fragments', ('unassigned_source_fragment', 'recovered_source_fragment')),
        ('source-coverage', ('mask_coverage', 'boundary_uncovered', 'polyline_off_mask')),
        ('model-disagreement', ('model_source_count_disagreement',)),
        ('short-or-faded', ('short_lane', 'faded')),
    ):
        if any(token in codes for token in tokens):
            return name
    return 'other-conversion-warning'


def prepare(settings, directory, apply=False):
    store = ReviewStore(settings)
    groups, strata, bindings = defaultdict(list), {}, {}
    all_counts = Counter()
    with store.registry() as reg:
        checks = reg.conn.execute("SELECT c.*,i.source,i.session_id,i.split,i.tags FROM source_conversion_checks c JOIN images i USING(image_id) WHERE c.state='flagged' AND i.status='curated' AND i.locked=0 AND i.review_status NOT IN ('approved','corrected','rejected')").fetchall()
        for check in checks:
            reasons = json.loads(check['diagnostics']).get('reasons', [])
            key = check['source'] + ':' + family(reasons)
            all_counts[key] += 1
            if protected(reg.conn, check['image_id']):
                continue
            group = reg.conn.execute('SELECT state FROM source_validation_groups WHERE group_key=?', (check['group_key'],)).fetchone()
            if group and group['state'] == 'blocked':
                continue
            geometry = reg.annotation(check['image_id'], 'geometry', 'geometry')
            source = reg.annotation(check['image_id'], 'source_label')
            if not geometry or not source or geometry['id'] != check['geometry_id'] or source['id'] != check['source_id']:
                continue
            groups[key].append(check['image_id'])
            lighting = json.loads(check['tags'] or '{}').get('lighting', {}).get('value', 'unknown')
            strata[check['image_id']] = f"{check['split']}:{lighting}:{check['session_id']}"
            bindings[check['image_id']] = dict(check)
        selected = select_samples(groups, minimum_per_group=3, maximum_per_group=3,
                                  sample_strata=strata, policy=POLICY)
        batch = POLICY + ':' + digest({key: [(image_id, bindings[image_id]['geometry_id'], bindings[image_id]['source_id'])
                                             for image_id in ids] for key, ids in selected.items()})[:16]
        directory.mkdir(parents=True, exist_ok=False)
        plan = {'policy': POLICY, 'batch_id': batch, 'sample_ids': selected, 'backlog_by_family': dict(all_counts),
                'sample_count': sum(map(len, selected.values())), 'family_count': len(selected),
                'meaning': 'Diagnostic examples only. A decision never validates related photos.',
                'created_at': time.time()}
        atomic_json(directory / 'plan.json', plan)
    installed, skipped = [], []
    backup = store.backup() if apply else None
    if apply:
        with store.registry() as reg:
            with reg.tx() as con:
                con.execute('BEGIN IMMEDIATE')
                for key, ids in selected.items():
                    for image_id in ids:
                        check = con.execute('SELECT * FROM source_conversion_checks WHERE image_id=?', (image_id,)).fetchone()
                        expected = bindings[image_id]
                        if (not check or any(check[name] != expected[name] for name in check.keys())
                                or protected(con, image_id)
                                or file_sha256(check['geometry_path']) != check['geometry_sha256']
                                or file_sha256(check['source_path']) != check['source_sha256']):
                            skipped.append(image_id); continue
                        con.execute('INSERT OR IGNORE INTO conversion_diagnostic_samples VALUES (?,?,?,?,?,?)',
                                    (batch, image_id, key, check['geometry_id'], check['source_id'], time.time()))
                        installed.append(image_id)
                if installed:
                    con.execute("UPDATE local_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
    result = {**plan, 'applied': apply, 'installed': len(installed), 'skipped': skipped, 'backup': backup,
              'human_decisions_created': 0, 'cohorts_released': 0}
    atomic_json(directory / 'report.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generation', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(prepare(local_settings(), args.generation.resolve(), args.apply)), flush=True)
