"""Freeze and recheck every dropped similarity match without restoring labels."""
import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import time

import cv2

from .bootstrap import atomic_json
from .duplicate_verification import verify_pair, POLICY
from .local import local_settings
from .quality import assess, read_rgb
from .teachers.base import file_sha256


def compare(payload):
    first, second, quality_cfg = payload
    cv2.setNumThreads(1)
    result = {'image_id': first['image_id'], 'previous_image': first, 'comparison_image': second}
    try:
        if not Path(first['path']).is_file() or file_sha256(first['path']) != first['sha256']:
            raise ValueError('Original image file or fingerprint changed')
        rgb = read_rgb(first['path'])
        quality = assess(rgb, quality_cfg)
        if not quality.ok:
            return {**result, 'recover': False, 'reason': 'hard_quality_failure', 'quality_reason': quality.reason}
        proof = verify_pair(first, second) if second else {'duplicate': False, 'reason': 'no_recorded_duplicate_evidence'}
        return {**result, 'recover': not proof['duplicate'], 'reason': proof['reason'], 'comparison': proof}
    except Exception as error:
        return {**result, 'recover': False, 'error': type(error).__name__ + ': ' + str(error)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=6)
    args = parser.parse_args()
    settings = local_settings()
    args.output.mkdir(parents=True, exist_ok=False)
    con = sqlite3.connect(settings.db_path)
    con.row_factory = sqlite3.Row
    all_images = {row['image_id']: dict(row) for row in con.execute('SELECT * FROM images')}
    human_ids = {row[0] for row in con.execute("SELECT image_id FROM annotations WHERE kind='human' UNION SELECT image_id FROM review_events UNION SELECT image_id FROM reviews")}
    claimed = {row[0] for row in con.execute('SELECT image_id FROM review_claims WHERE expires>?', (time.time(),))}
    selected = [row for row in all_images.values() if row['status'] == 'dropped' and not row['locked']
                and row['image_id'] not in human_ids | claimed]
    con.close()
    inputs = [(row, all_images.get((row['reject_reason'] or '').partition(':')[2]), settings.get('quality', {})) for row in selected]
    atomic_json(args.output / 'selection.json', {'images': selected, 'policy': POLICY,
                'code_sha256': {str(Path(__file__)): file_sha256(__file__),
                               str(Path(__file__).with_name('duplicate_verification.py')): file_sha256(Path(__file__).with_name('duplicate_verification.py'))}})
    state = {'phase': 'comparing_original_images', 'pid': os.getpid(), 'total': len(inputs),
             'processed': 0, 'recoverable': 0, 'verified_duplicates': 0, 'errors': 0, 'started_at': time.time()}
    rows = []
    with (args.output / 'comparisons.jsonl').open('w', encoding='utf-8') as output:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for result in pool.map(compare, inputs, chunksize=8):
                rows.append(result)
                output.write(json.dumps(result) + '\n')
                output.flush()
                state.update(processed=len(rows), recoverable=sum(row.get('recover', False) for row in rows),
                             verified_duplicates=sum(row.get('comparison', {}).get('duplicate', False) for row in rows),
                             errors=sum('error' in row for row in rows), updated_at=time.time())
                if len(rows) % 100 == 0:
                    atomic_json(args.output / 'progress.json', state)
                    print(json.dumps(state), flush=True)
    report = {**state, 'phase': 'compared', 'reasons': dict(Counter(row.get('reason', 'error') for row in rows)),
              'selection_sha256': file_sha256(args.output / 'selection.json'),
              'comparisons_sha256': file_sha256(args.output / 'comparisons.jsonl'),
              'human_decisions_created': 0, 'images_restored': 0}
    atomic_json(args.output / 'report.json', report)
    atomic_json(args.output / 'progress.json', report)
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
