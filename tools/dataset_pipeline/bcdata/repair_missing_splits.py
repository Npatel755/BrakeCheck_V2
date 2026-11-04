"""Fill missing curated recording splits without moving existing assignments."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import time

from .bootstrap import atomic_json
from .expand_dashcam import split_for
from .local import local_settings
from .review_store import ReviewStore


def run(settings):
    store = ReviewStore(settings)
    backup = store.backup()
    repairs = []
    with store.registry() as reg:
        with reg.tx() as con:
            con.execute('BEGIN IMMEDIATE')
            known = defaultdict(set)
            for row in con.execute('SELECT session_id,split FROM images WHERE split IS NOT NULL'):
                if row['split'] not in ('train', 'val', 'test'):
                    raise ValueError('Invalid existing split; refusing to guess')
                known[row['session_id']].add(row['split'])
            if any(len(values) > 1 for values in known.values()):
                raise ValueError('Existing recording-session split conflict')
            missing = con.execute("SELECT * FROM images WHERE status='curated' AND split IS NULL").fetchall()
            missing_ids = {row['image_id'] for row in missing}
            # A historical frozen benchmark may contain an image whose live
            # metadata was damaged later. Its recorded assignment takes priority.
            frozen = defaultdict(set)
            for manifest in settings.path('models', 'ufld_baseline').glob('*/dataset/manifest.json'):
                for row in json.loads(manifest.read_text()):
                    if row['image_id'] in missing_ids:
                        frozen[row['image_id']].add(row['split'])
            for reserved in settings.path('models', 'ufld_baseline').glob('*/dataset/reserved_test_ids.json'):
                for image_id in json.loads(reserved.read_text()):
                    if image_id in missing_ids:
                        frozen[image_id].add('test')
            for row in missing:
                known[row['session_id']].update(frozen[row['image_id']])
            if any(len(values) > 1 or values - {'train', 'val', 'test'} for values in known.values()):
                raise ValueError('Frozen or live recording assignment conflict')
            for row in missing:
                votes = frozen[row['image_id']] | known[row['session_id']]
                if len(votes) > 1:
                    raise ValueError('Frozen or live recording assignment conflict: ' + row['image_id'])
                if votes:
                    chosen = next(iter(votes))
                    reason = 'existing_or_frozen_recording_assignment'
                else:
                    chosen = split_for(row['session_id'])
                    reason = 'deterministic_recovery_extension_train_or_val'
                known[row['session_id']].add(chosen)
                repairs.append({'image_id': row['image_id'], 'session_id': row['session_id'],
                                'previous_split': None, 'split': chosen, 'reason': reason})
            con.execute('''CREATE TABLE IF NOT EXISTS missing_split_repair_history(
                image_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,previous_split TEXT,
                assigned_split TEXT NOT NULL,reason TEXT NOT NULL,created_at REAL NOT NULL)''')
            for repair in repairs:
                con.execute('UPDATE images SET split=?,updated_at=? WHERE image_id=? AND split IS NULL',
                            (repair['split'], time.time(), repair['image_id']))
                con.execute('INSERT INTO missing_split_repair_history VALUES (?,?,?,?,?,?)',
                            (repair['image_id'], repair['session_id'], None, repair['split'], repair['reason'], time.time()))
            if repairs:
                con.execute("UPDATE local_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
    result = {'repaired': len(repairs), 'repairs': repairs, 'backup': backup,
              'existing_assignments_moved': 0, 'human_labels_changed': 0, 'created_at': time.time()}
    if repairs:
        atomic_json(settings.path('reports', 'missing_split_repair.json'), result)
    return result


if __name__ == '__main__':
    print(json.dumps(run(local_settings())), flush=True)
