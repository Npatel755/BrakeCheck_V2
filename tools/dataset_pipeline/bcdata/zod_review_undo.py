"""Transactional undo for one reviewer's latest unchanged ZOD decision."""
import json
import time

from fastapi import HTTPException

from .zod_mirror import encoded, sha

FIELDS = ('correction_json', 'correction_sha', 'correction_reviewer', 'correction_time',
          'resolved_json', 'action', 'reviewer', 'decision_fingerprint', 'decision_time')


def snapshot(db, row):
    member = db.execute('SELECT repair_required FROM zod_audit_members WHERE image_id=?', (row['image_id'],)).fetchone()
    return {'fields': {key: row[key] for key in FIELDS}, 'revision': row['revision'],
            'repair_required': member[0],
            'source_binding': [row['native_sha'], row['image_sha'], row['geometry_sha']]}


def undo(store, event_id, reviewer, owner=None, image_id=None):
    db = store._db()
    try:
        db.execute('BEGIN IMMEDIATE')
        event = db.execute('SELECT * FROM audit_events WHERE event_id=?', (event_id,)).fetchone()
        if not event or (image_id and event['image_id'] != image_id):
            raise HTTPException(404, 'ZOD decision not found')
        payload = json.loads(event['payload'])
        if payload.get('reviewer') != reviewer:
            raise HTTPException(403, 'Only the reviewer who made this decision can undo it')
        undo_id = 'undo-' + sha(event_id.encode())[:40]
        repeated = db.execute('SELECT payload FROM audit_events WHERE event_id=?', (undo_id,)).fetchone()
        if repeated:
            return json.loads(repeated[0])['result']
        if payload.get('kind') == 'undo' or 'before_state' not in payload:
            raise HTTPException(409, 'Undo is available for decisions saved after the Undo update')
        latest = db.execute('SELECT event_id FROM audit_events WHERE image_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (event['image_id'],)).fetchone()
        row = store._record(db, event['image_id'])
        store._verified(row)
        current = snapshot(db, row)
        after = payload['after_state']
        if latest[0] != event_id or any(current[key] != after[key] for key in ('fields', 'revision', 'source_binding')):
            raise HTTPException(409, 'A newer change exists. Undo cannot replace another review.')
        claim = db.execute('SELECT * FROM zod_review_claims WHERE image_id=?', (row['image_id'],)).fetchone()
        if claim and claim['expires'] > time.time() and claim['owner'] != owner:
            raise HTTPException(409, 'Another reviewer is working on this photo. Try Undo when they have finished.')
        before = payload['before_state']
        values = before['fields']
        db.execute('UPDATE frames SET ' + ','.join(key + '=?' for key in FIELDS) + ',revision=revision+1 WHERE image_id=?',
                   [*(values[key] for key in FIELDS), row['image_id']])
        # A diagnosed converter issue is independent of the human decision.
        finding = db.execute('SELECT finding,origin FROM zod_conversion_findings WHERE image_id=?', (row['image_id'],)).fetchone()
        gates = db.execute("SELECT 1 FROM sqlite_master WHERE name='zod_source_repair_audit_gates'").fetchone()
        if finding and gates:
            gate = db.execute('SELECT proof FROM zod_source_repair_audit_gates WHERE image_id=?', (row['image_id'],)).fetchone()
            proof = json.loads(gate[0]) if gate else {}
            if (proof.get('source_sha') == row['geometry_sha']
                    and proof.get('finding_binding') == sha(encoded(dict(finding)).encode())):
                finding = None  # Historical finding was repaired; its sample gate is still required.
        db.execute('UPDATE zod_audit_members SET repair_required=? WHERE image_id=?',
                   (int(bool(before['repair_required'] or finding)), row['image_id']))
        if owner:
            db.execute('INSERT OR REPLACE INTO zod_review_claims VALUES(?,?,?)', (row['image_id'], owner, time.time() + 600))
        restored = db.execute('SELECT * FROM frames WHERE image_id=?', (row['image_id'],)).fetchone()
        result = {'event_id': undo_id, 'image_id': row['image_id'], 'revision': restored['revision'],
                  'source': 'zod_mirror', 'training_eligible': False}
        db.execute('INSERT INTO audit_events VALUES(?,?,?,?)', (undo_id, row['image_id'], encoded({
            'image_id': row['image_id'], 'kind': 'undo', 'reviewer': reviewer, 'undo_of': event_id,
            'before_state': current, 'after_state': snapshot(db, restored), 'result': result}), time.time()))
        db.commit()
        return result
    finally:
        db.close()
