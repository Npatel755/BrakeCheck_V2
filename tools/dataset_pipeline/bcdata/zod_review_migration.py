"""Carry human evidence forward without approving changed machine labels."""
import json
import sqlite3

from .zod_mirror import encoded, sha


def lane_shape(geometry):
    return [(lane['slot'], lane.get('kind'), lane.get('segments', [lane['points']]))
            for lane in sorted(geometry['lanes'], key=lambda lane: lane['slot'])]


def carry_reviews(root, db):
    previous = root / 'repairs/continuous-boundaries-v4/audits.sqlite'
    counts = {'reviews_carried': 0, 'changed_reviews_require_recheck': 0}
    if not previous.exists():
        return counts
    source = sqlite3.connect(previous.as_uri() + '?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS review_migrations(image_id TEXT PRIMARY KEY, receipt TEXT NOT NULL)')
    try:
        for row in source.execute('SELECT * FROM frames WHERE action IS NOT NULL OR correction_json IS NOT NULL'):
            new = db.execute('SELECT * FROM frames WHERE image_id=?', (row['image_id'],)).fetchone()
            if not new or (row['native_sha'], row['image_sha']) != (new['native_sha'], new['image_sha']):
                raise ValueError('Review migration source identity changed')
            old_geo = json.loads(row['correction_json'] or row['geometry_json'])
            new_geo = json.loads(new['geometry_json'])
            unchanged = lane_shape(old_geo) == lane_shape(new_geo)
            # Complete human corrections remain authoritative. Fragmented old
            # targets stay in immutable history and require rechecking.
            human_complete = bool(row['correction_json'] and old_geo.get('human_complete_annotation')
                                  and all(len(l.get('segments', [l['points']])) == 1 for l in old_geo['lanes']))
            current = row['decision_fingerprint'] == sha(encoded([row['image_id'], row['native_sha'],
                row['image_sha'], row['geometry_sha'], row['correction_sha']]).encode())
            carry = bool(current and (unchanged or human_complete or row['action'] in ('skip', 'exclude')))
            correction = row['correction_json'] if human_complete or unchanged else None
            correction_sha = row['correction_sha'] if correction else None
            fingerprint = sha(encoded([new['image_id'], new['native_sha'], new['image_sha'],
                                       new['geometry_sha'], correction_sha]).encode())
            db.execute('UPDATE frames SET correction_json=?,correction_sha=?,correction_reviewer=?,correction_time=?,resolved_json=?,revision=?,action=?,reviewer=?,decision_fingerprint=?,decision_time=? WHERE image_id=?',
                (correction, correction_sha, row['correction_reviewer'] if correction else None,
                 row['correction_time'] if correction else None, row['resolved_json'] if correction else '[]',
                 row['revision'], row['action'] if carry else None, row['reviewer'],
                 fingerprint if carry else None, row['decision_time'], row['image_id']))
            db.execute('INSERT OR REPLACE INTO review_migrations VALUES(?,?)', (row['image_id'], encoded({
                'source_batch': 'continuous-boundaries-v4', 'original_fingerprint': row['decision_fingerprint'],
                'new_fingerprint': fingerprint, 'label_shape_unchanged': unchanged,
                'complete_human_correction_preserved': human_complete, 'decision_carried': carry,
                'new_human_decision_created': False})))
            counts['reviews_carried' if carry else 'changed_reviews_require_recheck'] += 1
        # Preserve exact original event payloads; migration is separate evidence.
        db.executemany('INSERT OR IGNORE INTO audit_events VALUES(?,?,?,?)',
                       [tuple(row) for row in source.execute('SELECT * FROM audit_events')])
        db.commit()
        return counts
    finally:
        source.close()
