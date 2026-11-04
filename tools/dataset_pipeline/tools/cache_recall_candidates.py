"""CPU forward-pass cache for recall calibration while GPU training continues."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bcdata.bootstrap import atomic_json
from bcdata.recall_search import candidate_heads
from bcdata.teachers.base import file_sha256


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot-run', required=True, type=Path)
    parser.add_argument('--checkpoint', required=True, type=Path)
    parser.add_argument('--output-run', required=True, type=Path)
    args = parser.parse_args()
    import torch
    import cv2
    from torch.utils.data import DataLoader
    from bcdata import ufld_baseline as baseline
    torch.set_num_threads(4); cv2.setNumThreads(1)
    output = args.output_run.resolve(); output.mkdir(parents=True, exist_ok=False)
    started = time.time()
    def status(**values):
        state = {'pid': os.getpid(), 'phase': 'caching_validation_candidates', 'processed': 0, 'total': 1586,
                 'output': str(output), 'updated_at': time.time(), **values}
        atomic_json(output / 'status.json', state)
        print(json.dumps(state), flush=True)
    status()
    try:
        snapshot = args.snapshot_run.resolve() / 'dataset'
        hashes = json.loads((snapshot / 'hashes.json').read_text())
        assert file_sha256(snapshot / 'manifest.json') == hashes['manifest.json']
        dataset = baseline.NativeDataset(snapshot, 'val')
        for row in dataset.records:
            assert row['split'] == 'val'
            for name in (row['image'], Path(row['image']).with_suffix('.lines.txt').as_posix()):
                path = (snapshot / name).resolve()
                assert path.is_relative_to(snapshot) and file_sha256(path) == hashes[name]
        checkpoint_sha = file_sha256(args.checkpoint)
        metadata = {'schema': 'recall_candidates:v1', 'checkpoint': str(args.checkpoint.resolve()),
                    'checkpoint_sha256': checkpoint_sha, 'snapshot': str(snapshot),
                    'manifest_sha256': hashes['manifest.json'], 'images': len(dataset.records),
                    'split': 'val', 'test_images_used': 0, 'device': 'cpu', 'precision': 'float32',
                    'labels_changed': False, 'created_at': time.time()}
        atomic_json(output / 'cache_metadata.json', metadata)
        saved = torch.load(args.checkpoint, map_location='cpu', weights_only=False, mmap=True)
        baseline.validate_warm_start_config(saved['config'])
        vendor = Path(__file__).resolve().parents[1] / 'vendor/ufld_v2'
        model = baseline.upstream_model(vendor, pretrained=False)
        model.load_state_dict(saved['model']); del saved
        model.eval(); loader = DataLoader(dataset, batch_size=4, shuffle=False, num_workers=0)
        index = []; offset = 0
        with torch.inference_mode():
            for batch in loader:
                pred = model(batch['images'])
                for i, number in enumerate(batch['index'].tolist()):
                    record = dataset.records[number]
                    reference = []
                    for line in (snapshot / Path(record['image']).with_suffix('.lines.txt')).read_text().splitlines():
                        values = [float(v) for v in line.split()]
                        if len(values) >= 4:
                            reference.append([values[j:j+2] for j in range(0, len(values), 2)])
                    candidates = candidate_heads(pred, i)
                    # Verify cached selection is exactly the shipped decoder, not an approximation.
                    from bcdata.recall_search import select_indices, STOCK_RULE, SHORT_RULE
                    for policy, rule in (('stock', STOCK_RULE), ('short_span_v1', SHORT_RULE)):
                        lines = [candidates[j]['points'] for j in select_indices(candidates, rule)]
                        assert lines == baseline.decode(pred, i, policy), (record['image_id'], policy)
                    atomic_json(output / 'candidates' / (record['image_id'] + '.json'),
                                {'record': record, 'reference': reference, 'candidates': candidates})
                    index.append(record['image_id']); offset += 1
                if offset % 80 == 0 or offset == len(dataset.records):
                    status(processed=offset, total=len(dataset.records), seconds=round(time.time()-started, 1))
        atomic_json(output / 'candidate_index.json', index)
        metadata.update(phase='complete', completed_at=time.time(), index_sha256=file_sha256(output / 'candidate_index.json'))
        atomic_json(output / 'cache_metadata.json', metadata)
        status(phase='candidate_cache_complete', processed=offset, total=offset)
    except Exception as error:
        status(phase='error', error_reason=str(error)); raise


if __name__ == '__main__':
    main()
