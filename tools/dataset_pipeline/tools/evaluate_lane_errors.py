"""Populate current-validation error review without occupying the training GPU."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bcdata.bootstrap import atomic_json
from bcdata.lane_error_artifacts import ErrorCollection
from bcdata.local import local_settings
from bcdata.teachers.base import file_sha256
from bcdata import ufld_baseline as baseline


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot-run', required=True, type=Path)
    parser.add_argument('--checkpoint', required=True, type=Path)
    parser.add_argument('--collection-id', required=True)
    args = parser.parse_args()
    import torch
    from torch.utils.data import DataLoader
    import cv2
    torch.set_num_threads(4); cv2.setNumThreads(1)
    settings = local_settings()
    snapshot = args.snapshot_run.resolve() / 'dataset'
    hashes = json.loads((snapshot / 'hashes.json').read_text())
    assert file_sha256(snapshot / 'manifest.json') == hashes['manifest.json']
    records = [r for r in json.loads((snapshot / 'manifest.json').read_text()) if r['split'] == 'val']
    for row in records:
        for relative in (row['image'], str(Path(row['image']).with_suffix('.lines.txt')).replace('\\', '/')):
            path = (snapshot / relative).resolve()
            assert path.is_relative_to(snapshot) and file_sha256(path) == hashes[relative]
    checkpoint_sha = file_sha256(args.checkpoint)
    provenance = {'run': str(args.snapshot_run.resolve()), 'stage': 'starting-checkpoint-cpu',
                  'checkpoint': str(args.checkpoint.resolve()), 'checkpoint_sha256': checkpoint_sha,
                  'manifest_sha256': hashes['manifest.json'], 'device': 'cpu', 'precision': 'float32',
                  'interpretation': 'Same CPU forward pass for all decoder comparisons; small differences from GPU bfloat16 evaluation are possible.',
                  'decoder_policies': baseline.DECODER_POLICIES}
    collection = ErrorCollection(settings.root, args.collection_id, snapshot, provenance)
    print(json.dumps({'phase': 'loading_cpu_model', 'images': len(records)}), flush=True)
    vendor = Path(__file__).resolve().parents[1] / 'vendor/ufld_v2'
    saved = torch.load(args.checkpoint, map_location='cpu', weights_only=False, mmap=True)
    baseline.validate_warm_start_config(saved['config'])
    model = baseline.upstream_model(vendor, pretrained=False)
    model.load_state_dict(saved['model']); del saved
    model.eval()
    dataset = baseline.NativeDataset(snapshot, 'val')
    loader = DataLoader(dataset, batch_size=4, shuffle=False, num_workers=0)
    totals = {p: Counter() for p in baseline.DECODER_POLICIES}
    processed, started = 0, time.time()
    with torch.inference_mode():
        for batch in loader:
            outputs = model(batch['images'])
            for i, index in enumerate(batch['index'].tolist()):
                record = dataset.records[index]
                reference = []
                for line in (snapshot / Path(record['image']).with_suffix('.lines.txt')).read_text().splitlines():
                    values = [float(v) for v in line.split()]
                    if len(values) >= 4:
                        reference.append([values[j:j+2] for j in range(0, len(values), 2)])
                predictions = {p: baseline.decode(outputs, i, p) for p in totals}
                counts = collection.add(record, reference, predictions)
                for policy in totals:
                    totals[policy].update({k: counts[policy][k] for k in ('tp', 'fp', 'fn')})
                    totals[policy]['images'] += 1
                processed += 1
            if processed % 40 == 0 or processed == len(records):
                collection.metadata['processed'] = processed
                atomic_json(collection.path / 'metadata.json', collection.metadata)
                print(json.dumps({'phase': 'cpu_evaluation', 'processed': processed, 'total': len(records),
                                  'seconds': round(time.time()-started, 1)}), flush=True)
    metrics = {p: baseline.validation_metrics(0., dict(counts)) for p, counts in totals.items()}
    for result in metrics.values():
        result.pop('loss')  # this exporter does not calculate a training loss
    collection.finish(metrics)
    print(json.dumps({'phase': 'complete', 'metrics': metrics, 'cases': len(collection.rows)}), flush=True)


if __name__ == '__main__':
    main()
