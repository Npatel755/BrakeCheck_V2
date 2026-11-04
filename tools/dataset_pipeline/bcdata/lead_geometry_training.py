"""Predeclared compact geometry-only comparator for the lead image experiment.

Uses the same fixed fit/dev sessions as the visual challenger. This model can
learn selection and no-lead decisions but cannot see road pixels or lane curves.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import random
import shutil
import time

import numpy as np
import torch
from torch import nn

from .bootstrap import atomic_json
from .lead_review import file_sha
from .lead_training import (HEIGHT, WIDTH, GEOMETRY_NAMES, geometry, internal_partition,
                            metrics, picks, ranking_loss, validate_records)

RECIPE = 'lead-geometry-set-mlp-v1'


class GeometryRanker(nn.Module):
    def __init__(self):
        super().__init__()
        self.embedding = nn.Sequential(nn.Linear(12, 64), nn.ReLU())
        self.candidate_head = nn.Sequential(nn.Linear(128, 64), nn.ReLU(), nn.Dropout(.15), nn.Linear(64, 1))
        self.none_head = nn.Sequential(nn.Linear(64, 32), nn.ReLU(), nn.Dropout(.15), nn.Linear(32, 1))

    def forward(self, geom, valid):
        embedded = self.embedding(geom)
        context = embedded.masked_fill(~valid[..., None], -1e4).max(1).values
        features = torch.cat([embedded, context[:, None, :].expand(-1, geom.shape[1], -1)], -1)
        candidates = self.candidate_head(features).squeeze(-1).masked_fill(~valid, -1e4)
        return torch.cat([candidates, self.none_head(context)], -1)


def tensors(records):
    n = max(len(r['detections']) for r in records)
    values = torch.zeros(len(records), n, 12)
    valid = torch.zeros(len(records), n, dtype=torch.bool)
    targets = torch.zeros(len(records), n + 1, dtype=torch.bool)
    for i, row in enumerate(records):
        w, h = row['width'], row['height']; scale = min(WIDTH / w, HEIGHT / h)
        nw, nh = round(w * scale), round(h * scale)
        boxes = torch.tensor([d['box'] for d in row['detections']], dtype=torch.float32)
        boxes[:, [0, 2]] = (boxes[:, [0, 2]] * nw / w + (WIDTH - nw) // 2) / WIDTH
        boxes[:, [1, 3]] = (boxes[:, [1, 3]] * nh / h + (HEIGHT - nh) // 2) / HEIGHT
        size = len(boxes); values[i, :size] = geometry(boxes, row['detections']); valid[i, :size] = True
        labels = row['candidate_targets']['candidate_labels'] or [0] * size
        targets[i, :size] = torch.tensor(labels, dtype=torch.bool)
        targets[i, n] = not any(labels)
    return values, valid, targets


def probabilities(model, data):
    model.eval()
    with torch.no_grad():
        geom, valid, target = data
        logits = model(geom, valid)
        probs = logits.softmax(1)
        result = [probs[i, :int(mask.sum())].tolist() + [float(probs[i, -1])] for i, mask in enumerate(valid)]
        return result, float(ranking_loss(logits, target))


def train(args):
    torch.set_num_threads(2); torch.manual_seed(20261024); random.seed(20261024); np.random.seed(20261024)
    root, manifest, out = Path(args.root).resolve(), Path(args.manifest).resolve(), Path(args.out).resolve()
    rows = json.loads(manifest.read_text()); validate_records(rows, root)
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(manifest, out / 'manifest.json'); shutil.copytree(manifest.parent / 'licenses', out / 'licenses')
    shutil.copy2(__file__, out / 'lead_geometry_training.py')
    usable = [r for r in rows if r['candidate_targets']['usable_for_ranking']]
    fit = [r for r in usable if r['split'] == 'train' and internal_partition(r['session_id']) == 'fit']
    dev = [r for r in usable if r['split'] == 'train' and internal_partition(r['session_id']) == 'dev']
    val = [r for r in rows if r['split'] == 'val']
    model = GeometryRanker(); optimizer = torch.optim.AdamW(model.parameters(), lr=.003, weight_decay=.02)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=250, eta_min=.0001)
    provenance = {'recipe': RECIPE, 'created_at_unix': time.time(), 'seed': 20261024,
        'manifest_sha256': file_sha(manifest), 'training_code_sha256': file_sha(Path(__file__)),
        'selection_predeclared_before_original_validation': True,
        'selection': 'minimum internal-dev cross entropy; threshold fixed by internal-dev accuracy',
        'parameters': sum(p.numel() for p in model.parameters()), 'geometry_names': GEOMETRY_NAMES,
        'fit_ids': [r['image_id'] for r in fit], 'internal_dev_ids': [r['image_id'] for r in dev],
        'validation_ids': [r['image_id'] for r in val], 'original_test_images_used': 0,
        'external_data_used': False, 'initialization': 'random', 'app_integrated': False,
        'limitations': 'No image pixels, lanes, direction or temporal evidence; sparse advisory experiment only'}
    atomic_json(out / 'provenance.json', provenance)
    fit_data, dev_data = tensors(fit), tensors(dev)
    best = float('inf'); history = []
    for epoch in range(1, 251):
        model.train(); losses = []
        for indices in torch.randperm(len(fit)).split(32):
            geom, valid, target = [t[indices] for t in fit_data]
            geom = geom.clone(); flip = torch.rand(len(geom)) < .5
            left = geom[flip, :, 0].clone()
            geom[flip, :, 0] = 1 - geom[flip, :, 2]
            geom[flip, :, 2] = 1 - left; geom[flip, :, 4] = 1 - geom[flip, :, 4]
            optimizer.zero_grad(set_to_none=True)
            loss = ranking_loss(model(geom, valid), target)
            if not torch.isfinite(loss):
                raise RuntimeError('Nonfinite geometry loss')
            loss.backward(); nn.utils.clip_grad_norm_(model.parameters(), 5); optimizer.step()
            losses.append(float(loss.detach()))
        scheduler.step()
        probs, dev_loss = probabilities(model, dev_data)
        entry = {'epoch': epoch, 'fit_loss': float(np.mean(losses)), 'dev_loss': dev_loss,
                 'dev_accuracy': metrics(dev, picks(probs))['decision_accuracy']}
        history.append(entry)
        if dev_loss < best:
            best = dev_loss
            torch.save({'state_dict': model.state_dict(), 'epoch': epoch, 'recipe': RECIPE,
                        'provenance': provenance}, out / 'best.pt')
        if epoch % 10 == 0:
            print(json.dumps(entry), flush=True)
        atomic_json(out / 'status.json', {'status': 'training', **entry, 'epochs': 250})
    selected = torch.load(out / 'best.pt', weights_only=False)
    model.load_state_dict(selected['state_dict']); model.eval()
    dev_probs, _ = probabilities(model, dev_data)
    threshold = max([i / 20 for i in range(20)],
        key=lambda t: (metrics(dev, picks(dev_probs, t))['decision_accuracy'], t))
    # The final model/threshold are frozen before original validation is opened.
    val_probs, _ = probabilities(model, tensors(val))
    chosen = picks(val_probs, threshold)
    result = {'status': 'complete', 'recipe': RECIPE, 'best_epoch': selected['epoch'], 'threshold': threshold,
        'internal_development': metrics(dev, picks(dev_probs, threshold)), 'validation_all': metrics(val, chosen),
        'validation_detector_supported': metrics([r for r in val if r['candidate_targets']['usable_for_ranking']],
            [p for r, p in zip(val, chosen) if r['candidate_targets']['usable_for_ranking']]),
        'checkpoint_sha256': file_sha(out / 'best.pt'), 'checkpoint_bytes': (out / 'best.pt').stat().st_size,
        'app_integrated': False, 'device_runtime_tested': False}
    atomic_json(out / 'history.json', history); atomic_json(out / 'metrics.json', result)
    atomic_json(out / 'status.json', result)
    atomic_json(out / 'validation-predictions.json', [{'image_id': r['image_id'], 'selected_candidate': p,
        'probabilities_and_none': values} for r, p, values in zip(val, chosen, val_probs)])
    example = tensors(fit[:2]); traced = torch.jit.trace(model, (example[0], example[1]))
    traced.save(str(out / 'lead-geometry.torchscript.pt'))
    print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', required=True); p.add_argument('--manifest', required=True); p.add_argument('--out', required=True)
    train(p.parse_args())
