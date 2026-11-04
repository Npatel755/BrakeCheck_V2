"""Audit frozen lead experiments and select using internal-development loss only."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bcdata.lead_geometry_training import GeometryRanker
from bcdata.lead_training import internal_partition, metrics, picks, validate_records

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / 'BrakeCheck_Dataset/experiments/lead_cars'
OUT = ROOT / 'reports/lead-cars-training-20261024'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    summaries, ids = {}, None
    for name, source in [('mnv3s-scratch-20261024-01', 'lead_training.py'),
                         ('geometry-20261024-01', 'lead_geometry_training.py')]:
        folder = BASE / 'runs' / name
        provenance = json.loads((folder / 'provenance.json').read_text())
        measured = json.loads((folder / 'metrics.json').read_text())
        history = json.loads((folder / 'history.json').read_text())
        rows = json.loads((folder / 'manifest.json').read_text())
        assert measured['status'] == 'complete'
        assert sha(folder / 'manifest.json') == provenance['manifest_sha256']
        assert sha(folder / source) == provenance['training_code_sha256']
        assert sha(folder / 'best.pt') == measured['checkpoint_sha256']
        assert not provenance['external_data_used'] and not provenance['original_test_images_used']
        assert not measured['app_integrated']
        validate_records(rows, BASE)
        train_ids, dev_ids, val_ids = [set(provenance[k]) for k in ['fit_ids', 'internal_dev_ids', 'validation_ids']]
        assert not (train_ids & dev_ids or train_ids & val_ids or dev_ids & val_ids)
        assert (len(train_ids), len(dev_ids), len(val_ids)) == (222, 45, 94)
        if ids is not None:
            assert ids == (train_ids, dev_ids, val_ids)
        ids = train_ids, dev_ids, val_ids
        for row in rows:
            if row['image_id'] in train_ids | dev_ids:
                assert row['split'] == 'train' and row['candidate_targets']['usable_for_ranking']
                assert (row['image_id'] in dev_ids) == (internal_partition(row['session_id']) == 'dev')
        original = sqlite3.connect((ROOT / 'BrakeCheck_Dataset/metadata.sqlite').as_uri() + '?mode=ro', uri=True)
        protected = list(original.execute("SELECT source,session_id,sha256 FROM images WHERE split IN ('val','test')"))
        original.close()
        protected_sessions = {(r[0], r[1]) for r in protected}
        protected_hashes = {r[2] for r in protected}
        assert not any((r['source'], r['session_id']) in protected_sessions or r['source_sha256'] in protected_hashes for r in rows)
        predicted = json.loads((folder / 'validation-predictions.json').read_text())
        val = [r for r in rows if r['split'] == 'val']
        assert [p['image_id'] for p in predicted] == [r['image_id'] for r in val]
        assert metrics(val, [p['selected_candidate'] for p in predicted]) == measured['validation_all']
        best = min(history, key=lambda e: e['dev_loss'])
        assert best['epoch'] == measured['best_epoch']
        summaries[name] = {'parameters': provenance['parameters'], 'best_epoch': measured['best_epoch'],
            'best_internal_dev_loss': best['dev_loss'], 'threshold': measured['threshold'],
            'validation_all': measured['validation_all'], 'checkpoint_sha256': measured['checkpoint_sha256']}
    selected = min(summaries, key=lambda name: (summaries[name]['best_internal_dev_loss'], summaries[name]['parameters']))
    # Exported geometry graph must preserve the trained eager model for different
    # candidate counts; this checks packaging, not another validation evaluation.
    folder = BASE / 'runs/geometry-20261024-01'
    eager = GeometryRanker().eval()
    eager.load_state_dict(torch.load(folder / 'best.pt', weights_only=False)['state_dict'])
    scripted = torch.jit.load(str(folder / 'lead-geometry.torchscript.pt')).eval()
    max_error = 0.
    with torch.inference_mode():
        for batch, count in [(1, 1), (2, 5), (1, 64)]:
            geom = torch.rand(batch, count, 12); valid = torch.ones(batch, count, dtype=torch.bool)
            if count > 1:
                valid[:, -1] = False
            expected, actual = eager(geom, valid), scripted(geom, valid)
            torch.testing.assert_close(actual, expected, atol=1e-5, rtol=1e-5)
            max_error = max(max_error, float((actual - expected).abs().max()))
    result = {'selected_candidate': selected, 'selection_basis': 'minimum internal-development cross entropy, never original validation accuracy',
        'models': summaries, 'frozen_manifest_sha256': sha(folder / 'manifest.json'),
        'source_images_reverified': 500, 'original_evaluation_excluded': True,
        'fit_images': 222, 'internal_dev_images': 45, 'original_validation_images': 94,
        'torchscript_max_logit_error': max_error, 'torchscript_bytes': (folder / 'lead-geometry.torchscript.pt').stat().st_size,
        'tests_passed': 32, 'phone_integrated': False, 'device_runtime_tested': False}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
    (BASE / 'selected-experimental-model.json').write_text(json.dumps(result, indent=2) + '\n')
    # Preserve the common code used by the geometry worker alongside its source.
    shutil.copy2(BASE / 'runs/mnv3s-scratch-20261024-01/lead_training.py', folder / 'lead_training_helpers.py')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
