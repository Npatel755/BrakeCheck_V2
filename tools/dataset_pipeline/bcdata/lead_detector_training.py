"""Recover lead proposals from human boxes, including previous detector misses.

The target is *lead_vehicle*, not generic vehicle: other visible cars are not
fully annotated. The existing Apache RTMDet detector remains untouched. Its
backbone initializes this separate, single-class experimental proposal model.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import json
import math
from pathlib import Path
import random
import shutil
import sqlite3
import time

import cv2
import numpy as np
import torch
from torch import nn
from torchvision.ops import batched_nms, box_iou

from .bootstrap import atomic_json
from .lead_review import file_sha, inside
from .lead_training import internal_partition, validate_records

RECIPE = 'lead-rtmdet-tiny-transfer-v1'
TEACHER_SHA = '79b8a0b1e22b100e36a5591b24a1a97eae2fc3f29ad73cb4809d8c9c6cd19a52'
SIZE = 640
VEHICLES = (2, 3, 5, 7)


def partitions(rows):
    fit = [r for r in rows if r['split'] == 'train' and internal_partition(r['session_id']) == 'fit']
    dev = [r for r in rows if r['split'] == 'train' and internal_partition(r['session_id']) == 'dev']
    val = [r for r in rows if r['split'] == 'val']
    groups = [{r['session_id'] for r in group} for group in (fit, dev, val)]
    if any(not group for group in groups) or any(groups[i] & groups[j] for i in range(3) for j in range(i)):
        raise ValueError('Missing or overlapping session partitions')
    if len(fit) + len(dev) + len(val) != len(rows):
        raise ValueError('Unassigned records')
    return fit, dev, val


def letterbox(image, box=None, flip=False, size=SIZE):
    """BGR, centered pad=114; transform supervision with the actual rounded resize."""
    h, w = image.shape[:2]
    gt = np.asarray(box, dtype=np.float32).reshape(-1, 4).copy() if box is not None else np.zeros((0, 4), np.float32)
    if flip:
        image = np.ascontiguousarray(image[:, ::-1])
        old = gt[:, 0].copy(); gt[:, 0] = w - gt[:, 2]; gt[:, 2] = w - old
    scale = min(size / w, size / h)
    nw, nh = round(w * scale), round(h * scale)
    ox, oy = (size - nw) // 2, (size - nh) // 2
    canvas = np.full((size, size, 3), 114, np.uint8)
    canvas[oy:oy + nh, ox:ox + nw] = cv2.resize(image, (nw, nh), interpolation=cv2.INTER_LINEAR)
    gt[:, [0, 2]] = gt[:, [0, 2]] * nw / w + ox
    gt[:, [1, 3]] = gt[:, [1, 3]] * nh / h + oy
    pixels = torch.from_numpy(canvas.transpose(2, 0, 1).copy()).float()
    return pixels, torch.from_numpy(gt), (nw / w, nh / h, ox, oy)


def restore_boxes(boxes, transform, width, height):
    sx, sy, ox, oy = transform
    boxes = boxes.clone()
    boxes[:, [0, 2]] = ((boxes[:, [0, 2]] - ox) / sx).clamp(0, width)
    boxes[:, [1, 3]] = ((boxes[:, [1, 3]] - oy) / sy).clamp(0, height)
    return boxes


class LeadDetectionDataset(torch.utils.data.Dataset):
    def __init__(self, rows, root, augment=False):
        self.rows, self.augment = rows, augment
        self.images = []
        for row in rows:
            raw = np.frombuffer(inside(root, row['image']).read_bytes(), np.uint8)
            image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError('Unreadable image')
            self.images.append(image)

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        row, image = self.rows[i], self.images[i]
        if self.augment:
            image = cv2.convertScaleAbs(image, alpha=random.uniform(.85, 1.15), beta=random.uniform(-10, 10))
        box = row['box'] if row['decision'] == 'box' else None
        pixels, gt, transform = letterbox(image, box, flip=self.augment and random.random() < .5)
        return pixels, gt, transform


def collate(samples):
    return torch.stack([s[0] for s in samples]), [s[1] for s in samples], [s[2] for s in samples]


def normalize(images):
    mean = images.new_tensor([103.53, 116.28, 123.675])[None, :, None, None]
    std = images.new_tensor([57.375, 57.12, 58.395])[None, :, None, None]
    return (images - mean) / std


def postprocess(prediction, transform, row, generic=False, minimum=.01, max_boxes=100):
    if generic:
        scores, cls = prediction[:, 4 + torch.tensor(VEHICLES, device=prediction.device)].max(1)
        cls = torch.tensor(VEHICLES, device=prediction.device)[cls]
    else:
        scores = prediction[:, 4]; cls = torch.zeros_like(scores, dtype=torch.long)
    keep = scores >= minimum
    boxes, scores, cls = prediction[keep, :4], scores[keep], cls[keep]
    if not len(boxes):
        return []
    keep = batched_nms(boxes, scores, cls, .5)[:max_boxes]
    boxes = restore_boxes(boxes[keep], transform, row['width'], row['height']).cpu()
    scores, cls = scores[keep].cpu(), cls[keep].cpu()
    return [{'box': box.tolist(), 'confidence': float(score), 'class_id': int(label)}
            for box, score, label in zip(boxes, scores, cls) if box[2] > box[0] and box[3] > box[1]]


@torch.inference_mode()
def predict(model, dataset, device, generic=False):
    model.eval(); model.head.export = True
    predictions, offset = [], 0
    loader = torch.utils.data.DataLoader(dataset, batch_size=8, shuffle=False, num_workers=0, collate_fn=collate)
    for images, _, transforms in loader:
        result = model(normalize(images.to(device)))
        for output, transform in zip(result, transforms):
            predictions.append(postprocess(output, transform, dataset.rows[offset], generic))
            offset += 1
    model.head.export = False
    return predictions


def overlap(boxes, gt):
    if not boxes or gt is None:
        return []
    return box_iou(torch.tensor(boxes), torch.tensor([gt])).squeeze(1).tolist()


def detector_metrics(rows, predictions, threshold):
    if len(rows) != len(predictions):
        raise ValueError('Prediction count differs from images')
    positives = sum(r['decision'] == 'box' for r in rows)
    negatives = len(rows) - positives
    covered = correct = top_correct = false_alarms = n_proposals = 0
    for row, detections in zip(rows, predictions):
        detections = [d for d in detections if d['confidence'] >= threshold]
        n_proposals += len(detections)
        if row['decision'] == 'no_lead':
            false_alarms += bool(detections); correct += not detections
        else:
            ious = overlap([d['box'] for d in detections], row['box'])
            covered += bool(ious and max(ious) >= .5)
            good = bool(ious and ious[0] >= .5)
            top_correct += good; correct += good
    recall = covered / positives if positives else 0.
    top_recall = top_correct / positives if positives else 0.
    specificity = 1 - false_alarms / negatives if negatives else 0.
    return {'images': len(rows), 'positive_leads': positives, 'no_lead_images': negatives,
            'threshold': threshold, 'covered_leads': covered, 'proposal_recall_iou50': recall,
            'correct_top1_leads': top_correct, 'correct_decisions': correct,
            'decision_accuracy': correct / len(rows), 'no_lead_false_acquisitions': false_alarms,
            'no_lead_false_acquisition_rate': false_alarms / negatives if negatives else None,
            'balanced_top1_accuracy': .5 * (top_recall + specificity), 'proposal_count': n_proposals}


def average_precision(rows, predictions):
    """101-point lead AP@IoU.5, including negative images and duplicate false positives."""
    total = sum(r['decision'] == 'box' for r in rows)
    ordered = sorted([(d['confidence'], i, d['box']) for i, ds in enumerate(predictions) for d in ds], reverse=True)
    claimed, tp, fp = set(), [], []
    for _, i, box in ordered:
        good = rows[i]['decision'] == 'box' and i not in claimed and overlap([box], rows[i]['box'])[0] >= .5
        if good:
            claimed.add(i)
        tp.append(int(good)); fp.append(int(not good))
    if not total or not tp:
        return 0.
    tp, fp = np.cumsum(tp), np.cumsum(fp)
    recall, precision = tp / total, tp / np.maximum(tp + fp, 1)
    return float(np.mean([max(precision[recall >= level], default=0.) for level in np.linspace(0, 1, 101)]))


def select_threshold(rows, predictions):
    options = [.05 + .025 * i for i in range(35)]
    return max(options, key=lambda t: (detector_metrics(rows, predictions, t)['balanced_top1_accuracy'], t))


def initialize(weights, device):
    from libreyolo import LibreYOLO
    wrapper = LibreYOLO(str(weights), device=device)
    cls_weights = [layer.weight.detach().clone() for layer in wrapper.model.head.rtm_cls]
    cls_bias = [layer.bias.detach().clone() for layer in wrapper.model.head.rtm_cls]
    wrapper._rebuild_for_new_classes(1)
    model = wrapper.model
    with torch.no_grad():
        for i, layer in enumerate(model.head.rtm_cls):
            layer.weight.copy_(cls_weights[i][list(VEHICLES)].mean(0, keepdim=True))
            layer.bias.copy_(cls_bias[i][list(VEHICLES)].mean(0, keepdim=True))
    for parameter in model.backbone.parameters():
        parameter.requires_grad_(False)
    return model


def train(args):
    from libreyolo.models.rtmdet.loss import RTMDetLoss
    import importlib.metadata
    torch.set_num_threads(4); cv2.setNumThreads(1)
    random.seed(args.seed); np.random.seed(args.seed); torch.manual_seed(args.seed)
    if not torch.cuda.is_available():
        raise RuntimeError('CUDA is required')
    torch.cuda.manual_seed_all(args.seed)
    out, root = Path(args.out).resolve(), Path(args.root).resolve()
    rows = json.loads(Path(args.manifest).read_text())
    validate_records(rows, root)
    if file_sha(args.weights) != TEACHER_SHA:
        raise ValueError('Unexpected initialization checkpoint')
    fit, dev, val = partitions(rows)
    metadata = sqlite3.connect((root.parents[1] / 'metadata.sqlite').as_uri() + '?mode=ro', uri=True)
    protected = list(metadata.execute("SELECT source,session_id,sha256 FROM images WHERE split IN ('val','test')"))
    metadata.close()
    protected_sessions = {(r[0], r[1]) for r in protected}; protected_hashes = {r[2] for r in protected}
    assert not any((r['source'], r['session_id']) in protected_sessions or r['source_sha256'] in protected_hashes for r in rows)
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(args.manifest, out / 'manifest.json'); shutil.copy2(__file__, out / 'lead_detector_training.py')
    shutil.copytree(Path(args.manifest).parent / 'licenses', out / 'licenses')
    device = torch.device('cuda')
    model = initialize(args.weights, device)
    manifest = {'recipe': RECIPE, 'created_at_unix': time.time(), 'seed': args.seed,
        'manifest_sha256': file_sha(args.manifest), 'code_sha256': file_sha(__file__),
        'initial_weights_sha256': TEACHER_SHA, 'initial_weights_license': 'Apache-2.0',
        'initial_weights_source': 'https://huggingface.co/LibreYOLO/LibreRTMDett',
        'libreyolo': importlib.metadata.version('libreyolo'), 'torch': str(torch.__version__),
        'gpu': torch.cuda.get_device_name(), 'class_names': ['lead_vehicle'],
        'parameters': sum(p.numel() for p in model.parameters()),
        'trainable_parameters': sum(p.numel() for p in model.parameters() if p.requires_grad),
        'frozen_backbone': True, 'batch_norm_running_statistics': 'frozen pretrained',
        'initial_classification': 'mean of COCO vehicle class filters 2,3,5,7',
        'fit_ids': [r['image_id'] for r in fit], 'internal_dev_ids': [r['image_id'] for r in dev],
        'validation_ids': [r['image_id'] for r in val],
        'previously_unmatched_fit_leads': sum(r['candidate_targets']['detector_missed_lead'] for r in fit),
        'previously_unmatched_dev_leads': sum(r['candidate_targets']['detector_missed_lead'] for r in dev),
        'human_labels': dict(Counter(r['decision'] for r in rows)), 'epochs': args.epochs,
        'selection': 'highest 101-point internal-dev lead AP@IoU.5; threshold by internal-dev balanced top1 accuracy',
        'preprocessing': 'BGR 640x640 centered letterbox pad114; exact rounded per-axis box transform; upstream mean/std',
        'augmentation': 'horizontal flip and mild brightness/contrast only; no mosaic, crop, mixup or synthetic boxes',
        'original_evaluation_images_used_for_fitting': 0, 'external_images_used': 0,
        'validation_caveat': 'same 94-session benchmark as previous experiment; untrained and untuned, but not a fresh test',
        'generic_vehicle_detector_modified': False, 'phone_integrated': False}
    atomic_json(out / 'provenance.json', manifest)
    atomic_json(out / 'status.json', {'status': 'training', 'epoch': 0, 'epochs': args.epochs})
    print(json.dumps({k: v for k, v in manifest.items() if not k.endswith('_ids')}, indent=2), flush=True)
    fit_data, dev_data = LeadDetectionDataset(fit, root, True), LeadDetectionDataset(dev, root)
    loader = torch.utils.data.DataLoader(fit_data, batch_size=args.batch, shuffle=True,
        num_workers=0, collate_fn=collate, generator=torch.Generator().manual_seed(args.seed))
    loss_fn = RTMDetLoss(num_classes=1, distributed_normalize=False).to(device)
    optimizer = torch.optim.AdamW([{'params': model.neck.parameters(), 'lr': .0001},
                                  {'params': model.head.parameters(), 'lr': .0005}], weight_decay=.01)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs, eta_min=.00001)
    best, best_epoch, history, started = -1., 0, [], time.time()
    for epoch in range(1, args.epochs + 1):
        model.train(); model.head.export = False
        for module in model.modules():
            if isinstance(module, nn.BatchNorm2d):
                module.eval()
        total, count = 0., 0
        for images, boxes, _ in loader:
            images = normalize(images.to(device)); boxes = [b.to(device) for b in boxes]
            labels = [torch.zeros(len(b), dtype=torch.long, device=device) for b in boxes]
            optimizer.zero_grad(set_to_none=True)
            cls, reg = model(images)
            loss = loss_fn(cls, reg, boxes, labels)['total_loss']
            if not torch.isfinite(loss):
                raise RuntimeError('Nonfinite detector loss')
            loss.backward(); nn.utils.clip_grad_norm_(model.parameters(), 10); optimizer.step()
            total += float(loss.detach()) * len(images); count += len(images)
        scheduler.step()
        predictions = predict(model, dev_data, device)
        ap = average_precision(dev, predictions)
        entry = {'epoch': epoch, 'epochs': args.epochs, 'fit_loss': total / count,
                 'internal_dev_AP50': ap, 'internal_dev_at_028': detector_metrics(dev, predictions, .28),
                 'elapsed_seconds': time.time() - started}
        if ap > best:
            best, best_epoch = ap, epoch
            torch.save({'state_dict': {k: v.detach().cpu() for k, v in model.state_dict().items()},
                        'recipe': RECIPE, 'epoch': epoch, 'provenance': manifest}, out / 'best.pt')
            atomic_json(out / 'best-dev-predictions.json', predictions)
        history.append(entry); atomic_json(out / 'history.json', history)
        atomic_json(out / 'status.json', {'status': 'training', 'best_epoch': best_epoch, **entry})
        print(json.dumps(entry), flush=True)
    chosen = torch.load(out / 'best.pt', map_location='cpu', weights_only=False)
    model.load_state_dict(chosen['state_dict'])
    dev_predictions = json.loads((out / 'best-dev-predictions.json').read_text())
    threshold = select_threshold(dev, dev_predictions)
    # Original validation is only opened after the checkpoint and threshold freeze.
    val_data = LeadDetectionDataset(val, root)
    predictions = predict(model, val_data, device)
    from libreyolo import LibreYOLO
    base = LibreYOLO(str(Path(args.weights).resolve()), device=device).model
    baseline_predictions = predict(base, val_data, device, generic=True)
    baseline_dev = predict(base, dev_data, device, generic=True)
    union = [[d for d in old if d['confidence'] >= .28] + [d for d in new if d['confidence'] >= threshold]
             for old, new in zip(baseline_predictions, predictions)]
    union_metrics = detector_metrics(val, union, .0)
    result = {'status': 'complete', 'recipe': RECIPE, 'best_epoch': best_epoch,
        'epochs': args.epochs, 'threshold': threshold, 'threshold_source': 'internal development only',
        'internal_development': detector_metrics(dev, dev_predictions, threshold),
        'internal_dev_AP50': best, 'validation_AP50': average_precision(val, predictions),
        'validation_lead_detector': detector_metrics(val, predictions, threshold),
        'validation_generic_baselines': {str(t): detector_metrics(val, baseline_predictions, t) for t in (.1, .28, .45)},
        'internal_dev_generic_baselines': {str(t): detector_metrics(dev, baseline_dev, t) for t in (.1, .28, .45)},
        'validation_union_coverage': {k: union_metrics[k] for k in
            ['images', 'positive_leads', 'covered_leads', 'proposal_recall_iou50', 'proposal_count']},
        'union_note': 'coverage-only generic + lead candidates; no-lead decisions/top1 are not a calibrated selector',
        'checkpoint_sha256': file_sha(out / 'best.pt'), 'checkpoint_bytes': (out / 'best.pt').stat().st_size,
        'elapsed_seconds': time.time() - started, 'phone_integrated': False}
    atomic_json(out / 'validation-predictions.json', [{'image_id': r['image_id'], 'lead': p, 'generic': b}
        for r, p, b in zip(val, predictions, baseline_predictions)])
    # Preserve an immutable pool for a later scorer experiment, never overwrite human reviews.
    all_data = LeadDetectionDataset(rows, root)
    atomic_json(out / 'all-lead-proposals.json', [{'image_id': r['image_id'], 'detections': p}
        for r, p in zip(rows, predict(model, all_data, device))])
    atomic_json(out / 'metrics.json', result); atomic_json(out / 'status.json', result)
    print(json.dumps(result, indent=2), flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', required=True); p.add_argument('--root', required=True)
    p.add_argument('--out', required=True); p.add_argument('--weights', required=True)
    p.add_argument('--epochs', type=int, default=40); p.add_argument('--batch', type=int, default=8)
    p.add_argument('--seed', type=int, default=20261025)
    args = p.parse_args(); existed = Path(args.out).exists()
    if args.epochs < 1 or args.batch < 2:
        p.error('Invalid epoch/batch settings')
    try:
        train(args)
    except BaseException as exc:
        if not existed and Path(args.out).is_dir():
            atomic_json(Path(args.out) / 'status.json', {'status': 'failed', 'error': str(exc)})
        raise


if __name__ == '__main__':
    main()
