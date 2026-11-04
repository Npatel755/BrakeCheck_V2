"""Select highest validation recall under a frozen false-positive budget."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bcdata.bootstrap import atomic_json
from bcdata.recall_search import STOCK_RULE, SHORT_RULE, select_indices, search_rules, score_matrix, choose_with_budget
from bcdata.ufld_baseline import validation_metrics
from bcdata.lane_error_artifacts import ErrorCollection
from bcdata.lane_improvements import GEOMETRY_RULES, STRICT_GEOMETRY_RULES


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--fp-budget', type=int, default=243)
    parser.add_argument('--collection-id', default='starting-model-recall-v4-20261023')
    args = parser.parse_args()
    if args.fp_budget < 0:
        parser.error('False-positive budget must be nonnegative')
    run = args.run.resolve()
    metadata = json.loads((run / 'cache_metadata.json').read_text())
    assert metadata['phase'] == 'complete' and metadata['split'] == 'val' and metadata['test_images_used'] == 0
    index = json.loads((run / 'candidate_index.json').read_text())
    assert len(index) == metadata['images'] == 1586
    cv2.setNumThreads(1)
    rules = search_rules()
    baselines = {'stock': STOCK_RULE, 'short_span_v1': SHORT_RULE,
                 'short_span_geometry_v2': {**SHORT_RULE, 'geometry': True, 'geometry_rules': STRICT_GEOMETRY_RULES},
                 'short_span_geometry_v3': {**SHORT_RULE, 'geometry': True, 'geometry_rules': GEOMETRY_RULES}}
    specs = list(baselines.values()) + rules
    totals = [Counter() for _ in specs]
    started = time.time(); cached = []
    def raster(points):
        mask = np.zeros((590, 1640), np.uint8)
        if len(points) >= 2:
            cv2.polylines(mask, [np.round(points).astype(np.int32)], False, 1, thickness=30)
        return mask.astype(bool)
    for offset, image_id in enumerate(index):
        data = json.loads((run / 'candidates' / (image_id + '.json')).read_text())
        assert data['record']['split'] == 'val'
        candidates, reference = data['candidates'], data['reference']
        masks = [raster(c['points']) for c in candidates]; refs = [raster(v) for v in reference]
        matrix = np.asarray([[np.count_nonzero(p & r) / max(1, np.count_nonzero(p | r)) for r in refs] for p in masks], dtype=np.float32).reshape(len(masks), len(refs))
        for i, rule in enumerate(specs):
            selected = select_indices(candidates, rule)
            counts = score_matrix(matrix, selected)
            totals[i].update(counts);totals[i]['images'] += 1
            if not reference:
                totals[i]['negative_images'] += 1
                totals[i]['false_positive_negative_images'] += bool(selected)
        cached.append(data)
        if (offset + 1) % 200 == 0 or offset + 1 == len(index):
            atomic_json(run / 'status.json', {'pid': os.getpid(), 'phase': 'calibrating_decoder',
                'processed': offset + 1, 'total': len(index), 'updated_at': time.time(), 'output': str(run)})
            print(json.dumps({'processed': offset + 1, 'total': len(index), 'seconds': round(time.time()-started,1)}), flush=True)
    results = []
    for i, (rule, counts) in enumerate(zip(specs, totals)):
        metrics = validation_metrics(0., dict(counts));metrics.pop('loss')
        results.append({'rule': rule, **metrics, 'name': list(baselines)[i] if i < len(baselines) else 'candidate-' + str(i-len(baselines))})
    reference = results[1]
    assert (reference['tp'], reference['fp'], reference['fn']) == (1740, 243, 664), 'Cached CPU baseline changed; do not compare unlike runs'
    winner = choose_with_budget(results, args.fp_budget)
    assert winner is not None
    result = {'schema': 'recall_budget_search:v1', 'objective': 'Maximum recall subject to lane false positives <= budget',
              'false_positive_budget': args.fp_budget, 'candidate_count': len(rules), 'baseline': reference,
              'selected': winner, 'all_results': results, 'checkpoint_sha256': metadata['checkpoint_sha256'],
              'manifest_sha256': metadata['manifest_sha256'], 'images': len(index), 'test_images_used': 0,
              'interpretation': 'Exploratory selection on the same frozen validation set; not independent test evidence.',
              'precision': metadata['precision'], 'device': metadata['device']}
    atomic_json(run / 'recall_search.json', result)
    atomic_json(run / 'selected_decoder.json', {'rule': winner['rule'], 'false_positive_budget': args.fp_budget,
        'manifest_sha256': metadata['manifest_sha256'], 'checkpoint_sha256': metadata['checkpoint_sha256'],
        'validation_images': len(index), 'test_images_used': 0, 'interpretation': result['interpretation']})
    data_root = Path(metadata['snapshot']).parents[3]
    assert (data_root / 'reports').is_dir(), data_root
    writer = ErrorCollection(data_root, args.collection_id, Path(metadata['snapshot']),
        {'run': str(run), 'stage': 'recall-budget-v4', 'checkpoint_sha256': metadata['checkpoint_sha256'],
         'checkpoint': metadata['checkpoint'], 'manifest_sha256': metadata['manifest_sha256'],
         'device': metadata['device'], 'precision': metadata['precision'],
         'selected_rule': winner['rule'], 'false_positive_budget': args.fp_budget,
         'baseline_recall': reference['recall'], 'selected_recall': winner['recall'],
         'interpretation': result['interpretation']})
    for data in cached:
        predictions = {p: [data['candidates'][i]['points'] for i in select_indices(data['candidates'], rule)] for p,rule in baselines.items()}
        predictions['recall_budget_v4'] = [data['candidates'][i]['points'] for i in select_indices(data['candidates'], winner['rule'])]
        writer.add(data['record'], data['reference'], predictions)
    metrics = {p: {k:v for k,v in results[i].items() if k not in ('rule','name')} for i,p in enumerate(baselines)}
    metrics['recall_budget_v4'] = {k:v for k,v in winner.items() if k not in ('rule','name')}
    writer.finish(metrics)
    atomic_json(run / 'status.json', {'pid': os.getpid(), 'phase': 'complete', 'output': str(run),
        'updated_at': time.time(), 'validation': metrics['recall_budget_v4'], 'val_images': len(index),
        'false_positive_budget': args.fp_budget, 'baseline_recall': reference['recall'],
        'selected_recall': winner['recall'], 'decoder_rule': winner['rule'], 'auto_promote': False})
    print(json.dumps({'baseline': reference, 'selected': winner, 'review_photos': len(writer.rows)}, indent=2), flush=True)


if __name__ == '__main__':
    main()
