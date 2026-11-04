"""Stage duplicate-boundary repairs while preserving live review history."""
import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from itertools import islice
import json
from pathlib import Path
import sqlite3
import time

from filelock import FileLock

from .bootstrap import atomic_json
from .lane_boundary_deduplication import consolidate_boundaries
from .zod_mirror import connect, encoded, sha
from .zod_review_migration import lane_shape

PREVIOUS = 'repairs/continuous-boundaries-v5'
REPAIR = 'repairs/boundary-consolidation-v6'
VERSION = 'zod-physical-boundaries-v6'
COLUMNS = ('image_id', 'geometry_json', 'geometry_sha', 'correction_json', 'correction_sha', 'qc_json',
           'revision', 'action', 'reviewer', 'decision_fingerprint', 'correction_reviewer',
           'correction_time', 'resolved_json', 'decision_time', 'native_sha', 'image_sha')


def prepare(row):
    original = dict(zip(COLUMNS, row))
    result = dict(original)
    before = json.loads(original['correction_json'] or original['geometry_json'])
    source = json.loads(original['geometry_json'])
    source_new = consolidate_boundaries(source)
    correction = json.loads(original['correction_json']) if original['correction_json'] else None
    correction_new = consolidate_boundaries(correction) if correction else None
    source_changed = source_new != source
    correction_changed = correction_new != correction
    if not source_changed and not correction_changed:
        return None
    effective = correction_new or source_new
    changed_labels = lane_shape(before) != lane_shape(effective)
    for field, geometry, changed in (('geometry', source_new, source_changed), ('correction', correction_new, correction_changed)):
        if changed:
            geometry.update(version=VERSION, parent_geometry_sha256=original[field + '_sha'])
            raw = encoded(geometry)
            result[field + '_json'], result[field + '_sha'] = raw, sha(raw.encode())
    info = effective.get('boundary_deduplication', {})
    source_info = source_new.get('boundary_deduplication', {})
    summary = {'source_changed': source_changed, 'labels_changed': changed_labels,
               'merged_pairs': len(info.get('merged_pairs', [])),
               'suspect_pairs': len(info.get('suspect_pairs', [])),
               'source_merged_pairs': len(source_info.get('merged_pairs', [])),
               'approved_requeued': original['action'] == 'approve' and (changed_labels or bool(info.get('suspect_pairs')))}
    if summary['approved_requeued']:
        result['action'] = None
    if changed_labels or info.get('suspect_pairs'):
        warnings = set(json.loads(result['qc_json']))
        warnings.add('paired_paint_boundary_consolidated_requires_review' if changed_labels else 'close_outer_main_boundaries_require_review')
        result['qc_json'] = encoded(sorted(warnings))
        if correction_changed:
            result['correction_reviewer'] = 'converter:' + VERSION
    result['revision'] += 1
    fingerprint = sha(encoded([result['image_id'], result['native_sha'], result['image_sha'],
                              result['geometry_sha'], result['correction_sha']]).encode())
    result['decision_fingerprint'] = fingerprint if result['action'] else None
    return original, result, summary


def apply(db, source, prepared):
    if not prepared:
        return
    original, result, summary = prepared
    image_id = result['image_id']
    db.execute('INSERT OR REPLACE INTO zod_boundary_repair_current VALUES(?,?,?)',
               (image_id, original['revision'], encoded(summary)))
    db.execute('INSERT OR IGNORE INTO zod_boundary_repair_history VALUES(?,?,?,?)',
               (image_id, original['revision'], encoded(original), encoded(summary)))
    fields = [key for key in COLUMNS if key not in ('image_id', 'native_sha', 'image_sha')]
    db.execute('UPDATE frames SET ' + ','.join(key + '=?' for key in fields) + ' WHERE image_id=?',
               [*(result[key] for key in fields), image_id])
    db.execute('UPDATE zod_audit_members SET geometry_sha=?,repair_required=1 WHERE image_id=?',
               (result['geometry_sha'], image_id))
    prior = source.execute('SELECT finding,origin FROM zod_conversion_findings WHERE image_id=?', (image_id,)).fetchone()
    finding = (prior['finding'] + '; ' if prior else '') + 'Outer/main boundary consolidation: inspect repaired paired paint or ambiguous close boundaries'
    db.execute('INSERT OR REPLACE INTO zod_conversion_findings VALUES(?,?,?)', (image_id, finding, VERSION))
    event_id = 'repair-v6-' + sha(encoded([image_id, original['revision'], original['geometry_sha'], original['correction_sha']]).encode())[:40]
    db.execute('INSERT OR IGNORE INTO audit_events VALUES(?,?,?,?)', (event_id, image_id, encoded({
        'image_id': image_id, 'kind': 'converter_repair', 'reviewer': 'converter:' + VERSION,
        'human_review_created': False, 'source_batch': PREVIOUS, 'summary': summary,
        'before_source_sha': original['geometry_sha'], 'before_correction_sha': original['correction_sha'],
        'after_source_sha': result['geometry_sha'], 'after_correction_sha': result['correction_sha']}), time.time()))


def summarize(db):
    counts = Counter()
    for raw, in db.execute('SELECT summary_json FROM zod_boundary_repair_current'):
        item = json.loads(raw)
        counts['photos_affected'] += 1
        counts['photos_with_merged_boundaries'] += int(item['merged_pairs'] > 0)
        counts['photos_with_ambiguous_close_boundaries'] += int(item['suspect_pairs'] > 0)
        counts['approved_photos_requeued'] += int(item['approved_requeued'])
        counts['boundary_pairs_merged'] += item['merged_pairs']
    return dict(counts)


def run(data_root):
    root = Path(data_root).resolve() / 'experiments/zod_mirror'
    target = root / REPAIR
    target.mkdir(parents=True, exist_ok=True)
    status_path = target / 'repair_status.json'
    with FileLock(str(target / '.repair.lock'), timeout=0):
        if status_path.exists() and json.loads(status_path.read_text()).get('state') in ('staged', 'audit_ready'):
            return json.loads(status_path.read_text())
        source = sqlite3.connect((root / PREVIOUS / 'audits.sqlite').as_uri() + '?mode=ro', uri=True)
        source.row_factory = sqlite3.Row
        db = connect(target)
        status = {'state': 'copying_review_snapshot', 'version': VERSION, 'started': time.time(),
                  'human_reviews_created': 0, 'training_eligible': False, 'done': 0, 'total': 100000}
        atomic_json(status_path, status)
        try:
            # Freeze a WAL read snapshot: active collaborator saves must not
            # continually restart SQLite's multi-page backup.
            source.execute('BEGIN')
            source.execute('SELECT COUNT(*) FROM frames').fetchone()
            db.execute('PRAGMA synchronous=NORMAL')
            source.backup(db, pages=10000)
            db.executescript('''CREATE TABLE IF NOT EXISTS zod_boundary_repair_current(image_id TEXT PRIMARY KEY,source_revision INTEGER,summary_json TEXT);
              CREATE TABLE IF NOT EXISTS zod_boundary_repair_history(image_id TEXT,source_revision INTEGER,before_json TEXT,summary_json TEXT,PRIMARY KEY(image_id,source_revision));''')
            status['state'] = 'repairing_boundaries'
            query = 'SELECT ' + ','.join(COLUMNS) + ' FROM frames ORDER BY image_id'
            rows = (tuple(row) for row in source.execute(query))
            with ProcessPoolExecutor(max_workers=4) as pool:
                while batch := list(islice(rows, 1000)):
                    for prepared in pool.map(prepare, batch, chunksize=16):
                        apply(db, source, prepared)
                    db.commit()
                    status['done'] += len(batch)
                    atomic_json(status_path, status)
            for row in source.execute('SELECT * FROM zod_model_proposals'):
                geometry = json.loads(row['geometry_json'])
                repaired = consolidate_boundaries(geometry)
                if repaired != geometry:
                    version = row['version'] + ':physical-boundaries-v6'
                    repaired.update(teacher_version=version, parent_proposal_sha256=row['geometry_sha'])
                    raw = encoded(repaired)
                    db.execute('UPDATE zod_model_proposals SET version=?,geometry_json=?,geometry_sha=? WHERE image_id=? AND model=?',
                               (version, raw, sha(raw.encode()), row['image_id'], row['model']))
            db.commit()
            status.update(state='staged', **summarize(db))
            atomic_json(status_path, status)
            atomic_json(target / 'proposal_status.json', {'state': 'complete', 'samples': 2420,
                        'total': 4840, 'done': 4840, 'inference_reused': True, 'training_performed': False})
            return status
        except Exception as error:
            db.rollback()
            status.update(state='error', error=str(error))
            atomic_json(status_path, status)
            raise
        finally:
            db.close()
            source.close()


def finalize(data_root):
    """Call while Studio is stopped, immediately before switching batches."""
    root = Path(data_root).resolve() / 'experiments/zod_mirror'
    target = root / REPAIR
    status = json.loads((target / 'repair_status.json').read_text())
    if status['state'] != 'staged':
        raise ValueError('Complete staging is required')
    source = sqlite3.connect((root / PREVIOUS / 'audits.sqlite').as_uri() + '?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    db = connect(target)
    try:
        live_ids = {row[0] for row in source.execute('SELECT DISTINCT image_id FROM audit_events WHERE created_at>=?', (status['started'],))}
        # Reconcile every staged repair against the latest human labels. A
        # collaborator may already have corrected one during background work.
        ids = live_ids | {row[0] for row in db.execute('SELECT image_id FROM zod_boundary_repair_current')}
        for image_id in ids:
            row = source.execute('SELECT ' + ','.join(COLUMNS) + ' FROM frames WHERE image_id=?', (image_id,)).fetchone()
            fields = [key for key in COLUMNS if key != 'image_id']
            db.execute('UPDATE frames SET ' + ','.join(key + '=?' for key in fields) + ' WHERE image_id=?', [*(row[key] for key in fields), image_id])
            member = source.execute('SELECT geometry_sha,repair_required FROM zod_audit_members WHERE image_id=?', (image_id,)).fetchone()
            db.execute('UPDATE zod_audit_members SET geometry_sha=?,repair_required=? WHERE image_id=?', (*member, image_id))
            db.execute('DELETE FROM zod_boundary_repair_current WHERE image_id=?', (image_id,))
            db.execute('DELETE FROM zod_conversion_findings WHERE image_id=?', (image_id,))
            finding = source.execute('SELECT * FROM zod_conversion_findings WHERE image_id=?', (image_id,)).fetchone()
            if finding:
                db.execute('INSERT INTO zod_conversion_findings VALUES(?,?,?)', tuple(finding))
            apply(db, source, prepare(tuple(row)))
        db.executemany('INSERT OR IGNORE INTO audit_events VALUES(?,?,?,?)',
                       [tuple(row) for row in source.execute('SELECT * FROM audit_events WHERE created_at>=?', (status['started'],))])
        db.execute('DELETE FROM zod_review_claims')
        db.executemany('INSERT INTO zod_review_claims VALUES(?,?,?)',
                       [tuple(row) for row in source.execute('SELECT * FROM zod_review_claims WHERE expires>?', (time.time(),))])
        db.commit()
        status.update(state='audit_ready', completed=time.time(), live_review_updates_preserved=len(live_ids), **summarize(db))
        atomic_json(target / 'repair_status.json', status)
        return status
    finally:
        db.close()
        source.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.data_root)))
