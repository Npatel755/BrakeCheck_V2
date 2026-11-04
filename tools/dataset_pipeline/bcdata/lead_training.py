"""Train an experimental lightweight candidate scorer from reviewed lead labels.

No downloads, app changes, or label writes. Backbone starts from random weights;
only the frozen local user export supplies learning targets. The original lead
validation split is used once after model selection on an internal train subset.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import random
import shutil
import time

import numpy as np
from PIL import Image, ImageEnhance, ImageOps
import torch
from torch import nn
from torch.nn import functional as F
from torchvision.models import mobilenet_v3_small

from .bootstrap import atomic_json
from .lead_review import file_sha, inside, candidate_targets

RECIPE = 'lead-mobilenet-v3-small-roi-scratch-v1'
WIDTH, HEIGHT, MAX_CANDIDATES = 320, 192, 64
GEOMETRY_NAMES = ['left', 'top', 'right', 'bottom', 'center_x', 'center_y',
                  'width', 'height', 'area', 'aspect_ratio_div4', 'confidence', 'forward_candidate']


def internal_partition(session):
    # Fixed before training, independent of labels. Original validation stays sealed.
    value = int(hashlib.sha256(('lead-internal-dev-v1:' + session).encode()).hexdigest()[:8], 16)
    return 'dev' if value % 5 == 0 else 'fit'


def validate_records(records, root, verify_images=True):
    if not records:
        raise ValueError('Empty reviewed export')
    ids, hashes, sessions = set(), set(), {}
    for row in records:
        if row['image_id'] in ids or row['source_sha256'] in hashes:
            raise ValueError('Duplicate image identity or source bytes')
        ids.add(row['image_id']); hashes.add(row['source_sha256'])
        if row.get('source') != 'comma10k' or row.get('license_id') != 'comma10k' or row.get('pool') != 'commercial':
            raise ValueError('Only the locally cleared comma10k collection is allowed')
        if row.get('source_split') != 'train' or row.get('split') not in ('train', 'val'):
            raise ValueError('Original evaluation photos or invalid lead split')
        if row.get('decision') not in ('box', 'no_lead') or not row.get('reviewer') or row.get('revision', 0) < 1:
            raise ValueError('A saved human review is required')
        key = (row['source'], row['session_id'])
        if key in sessions:
            raise ValueError('More than one photo per recording session')
        sessions[key] = row['split']
        w, h = row['width'], row['height']
        if min(w, h) <= 0:
            raise ValueError('Invalid image dimensions')
        boxes = [d['box'] for d in row['detections']]
        if row['decision'] == 'box':
            boxes.append(row['box'])
        elif row.get('box') is not None:
            raise ValueError('No-lead decision must not carry a box')
        if not row['detections'] or len(row['detections']) > MAX_CANDIDATES:
            raise ValueError('Unsupported number of candidate boxes')
        for box in boxes:
            if not box or len(box) != 4 or not all(math.isfinite(x) for x in box):
                raise ValueError('Invalid box')
            if not (0 <= box[0] < box[2] <= w and 0 <= box[1] < box[3] <= h):
                raise ValueError('Box outside original image')
        if row['candidate_targets'] != candidate_targets(row, row.get('box'), row['decision']):
            raise ValueError('Export candidate targets do not match human annotation')
        for d in row['detections']:
            if not math.isfinite(d['confidence']) or not 0 <= d['confidence'] <= 1:
                raise ValueError('Invalid detector confidence')
        if verify_images:
            path = inside(root, row['image'])
            if file_sha(path) != row['source_sha256']:
                raise ValueError('Source image changed since export')
            with Image.open(path) as image:
                if image.size != (w, h):
                    raise ValueError('Source dimensions changed')
    if {r['split'] for r in records} != {'train', 'val'}:
        raise ValueError('Both session-separated lead splits are required')


def prepared_image(image, boxes, flip=False):
    """Aspect-preserving centered letterbox; boxes remain normalized top-left xyxy."""
    image = image.convert('RGB')
    w, h = image.size
    boxes = np.asarray(boxes, dtype=np.float32).copy()
    if flip:
        image = ImageOps.mirror(image)
        old_left = boxes[:, 0].copy()
        boxes[:, 0] = w - boxes[:, 2]; boxes[:, 2] = w - old_left
    scale = min(WIDTH / w, HEIGHT / h)
    nw, nh = round(w * scale), round(h * scale)
    left, top = (WIDTH - nw) // 2, (HEIGHT - nh) // 2
    canvas = Image.new('RGB', (WIDTH, HEIGHT), (114, 114, 114))
    canvas.paste(image.resize((nw, nh), Image.Resampling.BILINEAR), (left, top))
    boxes[:, [0, 2]] = (boxes[:, [0, 2]] * nw / w + left) / WIDTH
    boxes[:, [1, 3]] = (boxes[:, [1, 3]] * nh / h + top) / HEIGHT
    pixels = np.asarray(canvas, np.float32).transpose(2, 0, 1) / 255.0
    return torch.from_numpy(pixels), torch.from_numpy(boxes)


def geometry(boxes, detections):
    size = boxes[:, 2:] - boxes[:, :2]
    center = (boxes[:, 2:] + boxes[:, :2]) * .5
    area = size[:, 0:1] * size[:, 1:2]
    aspect = (size[:, 0:1] / size[:, 1:2].clamp_min(1e-6) / 4).clamp_max(2)
    confidence = boxes.new_tensor([[d['confidence'], float(d['forward_candidate'])] for d in detections])
    return torch.cat([boxes, center, size, area, aspect, confidence], dim=1)


class LeadDataset(torch.utils.data.Dataset):
    def __init__(self, records, root, augment=False):
        self.records, self.augment = records, augment
        # Cache the small labeled collection in RAM; no repeated OneDrive reads.
        self.images = []
        for row in records:
            with Image.open(inside(root, row['image'])) as image:
                self.images.append(image.convert('RGB').copy())

    def __len__(self):
        return len(self.records)

    def __getitem__(self, index):
        row = self.records[index]
        image = self.images[index]
        if self.augment:
            image = ImageEnhance.Brightness(image).enhance(random.uniform(.75, 1.25))
            image = ImageEnhance.Contrast(image).enhance(random.uniform(.8, 1.2))
        pixels, boxes = prepared_image(image, [d['box'] for d in row['detections']],
                                       self.augment and random.random() < .5)
        labels = row['candidate_targets']['candidate_labels']
        return pixels, boxes, geometry(boxes, row['detections']), torch.tensor(labels or [0] * len(boxes), dtype=torch.bool)


def collate(samples):
    count = max(len(x[1]) for x in samples)
    boxes = torch.zeros(len(samples), count, 4)
    geom = torch.zeros(len(samples), count, 12)
    valid = torch.zeros(len(samples), count, dtype=torch.bool)
    target = torch.zeros(len(samples), count + 1, dtype=torch.bool)
    for i, (_, b, g, labels) in enumerate(samples):
        n = len(b); boxes[i, :n] = b; geom[i, :n] = g; valid[i, :n] = True
        target[i, :n] = labels
        target[i, count] = not labels.any()
    return torch.stack([x[0] for x in samples]), boxes, geom, valid, target


class LeadRanker(nn.Module):
    def __init__(self):
        super().__init__()
        self.encoder = mobilenet_v3_small(weights=None).features
        self.candidate_head = nn.Sequential(nn.Linear(1164, 128), nn.ReLU(), nn.Dropout(.25), nn.Linear(128, 1))
        self.none_head = nn.Sequential(nn.Linear(576, 64), nn.ReLU(), nn.Dropout(.25), nn.Linear(64, 1))
        self.register_buffer('mean', torch.tensor([.485, .456, .406])[None, :, None, None])
        self.register_buffer('std', torch.tensor([.229, .224, .225])[None, :, None, None])

    def forward(self, image, boxes, geom, valid):
        features = self.encoder((image - self.mean) / self.std)
        b, c, _, _ = features.shape
        n = boxes.shape[1]
        # Four bilinear samples per candidate. The scene encoder runs once per image.
        fraction = boxes.new_tensor([.25, .75])
        x = boxes[..., 0, None] + (boxes[..., 2] - boxes[..., 0])[..., None] * fraction
        y = boxes[..., 1, None] + (boxes[..., 3] - boxes[..., 1])[..., None] * fraction
        xx = x[:, :, None, :].expand(-1, -1, 2, -1)
        yy = y[:, :, :, None].expand(-1, -1, -1, 2)
        grid = torch.stack([xx, yy], -1).reshape(b, n * 2, 2, 2) * 2 - 1
        local = F.grid_sample(features, grid, align_corners=False, padding_mode='border')
        local = local.reshape(b, c, n, 2, 2).mean((-1, -2)).transpose(1, 2)
        scene = features.mean((-1, -2))
        logits = self.candidate_head(torch.cat([local, scene[:, None, :].expand(-1, n, -1), geom], -1)).squeeze(-1)
        logits = logits.masked_fill(~valid, -1e4)
        return torch.cat([logits, self.none_head(scene)], dim=1)


def ranking_loss(logits, targets):
    if not targets.any(dim=1).all():
        raise ValueError('Every training image needs a positive candidate or explicit none target')
    return -(torch.logsumexp(logits.masked_fill(~targets, -1e4), 1) - torch.logsumexp(logits, 1)).mean()


def metrics(records, predictions):
    correct = selected = correct_leads = false_acquisitions = missed = 0
    positives = sum(r['decision'] == 'box' for r in records)
    negatives = len(records) - positives
    for row, pick in zip(records, predictions):
        if pick is None:
            correct += row['decision'] == 'no_lead'
            missed += row['decision'] == 'box'
        else:
            selected += 1
            good = row['decision'] == 'box' and row['candidate_targets']['lead_iou'][pick] >= .5
            correct += good; correct_leads += good
            false_acquisitions += row['decision'] == 'no_lead'
    return {'images': len(records), 'positive_leads': positives, 'no_lead_images': negatives,
            'correct_decisions': int(correct), 'decision_accuracy': correct / len(records),
            'selected_leads': selected, 'correct_lead_selections': int(correct_leads),
            'lead_selection_precision': correct_leads / selected if selected else None,
            'lead_recall': correct_leads / positives if positives else None,
            'no_lead_false_acquisitions': false_acquisitions,
            'no_lead_false_acquisition_rate': false_acquisitions / negatives if negatives else None,
            'abstained_on_lead': missed,
            'detector_missed_leads': sum(r['candidate_targets']['detector_missed_lead'] for r in records)}


@torch.inference_mode()
def infer(model, dataset, device, calculate_loss=True):
    model.eval()
    probabilities, losses = [], []
    loader = torch.utils.data.DataLoader(dataset, batch_size=16, collate_fn=collate, shuffle=False)
    for images, boxes, geom, valid, target in loader:
        logits = model(images.to(device), boxes.to(device), geom.to(device), valid.to(device))
        # Detector-missed validation positives are measured as errors below,
        # never scored against a fake no-lead learning target or validation loss.
        probs = logits.softmax(1).cpu()
        for i, mask in enumerate(valid):
            n = int(mask.sum())
            probabilities.append(probs[i, :n].tolist() + [float(probs[i, -1])])
        if calculate_loss:
            losses.append(float(ranking_loss(logits, target.to(device))))
    return probabilities, float(np.mean(losses)) if losses else None


def picks(probabilities, threshold=0.0):
    result = []
    for values in probabilities:
        i = int(np.argmax(values))
        result.append(i if i < len(values) - 1 and values[i] >= threshold else None)
    return result


def train(args):
    started = time.time()
    random.seed(args.seed); np.random.seed(args.seed); torch.manual_seed(args.seed)
    torch.set_num_threads(6)
    if not torch.cuda.is_available():
        raise RuntimeError('CUDA GPU is required for this local training run')
    device = torch.device('cuda')
    torch.cuda.manual_seed_all(args.seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    out, root = Path(args.out).resolve(), Path(args.root).resolve()
    out.mkdir(parents=True, exist_ok=False)
    manifest = Path(args.manifest).resolve()
    records = json.loads(manifest.read_text())
    validate_records(records, root)
    shutil.copy2(manifest, out / 'manifest.json')
    shutil.copytree(manifest.parent / 'licenses', out / 'licenses')
    shutil.copy2(Path(__file__), out / 'lead_training.py')
    usable = [r for r in records if r['candidate_targets']['usable_for_ranking']]
    fit = [r for r in usable if r['split'] == 'train' and internal_partition(r['session_id']) == 'fit']
    dev = [r for r in usable if r['split'] == 'train' and internal_partition(r['session_id']) == 'dev']
    validation = [r for r in records if r['split'] == 'val']
    if not fit or not dev or any(not any(r['decision'] == decision for r in group)
                               for group in [fit, dev] for decision in ['box', 'no_lead']):
        raise ValueError('Fit and internal development both require lead and no-lead samples')
    model = LeadRanker().to(device)
    provenance = {'recipe': RECIPE, 'created_at_unix': started, 'seed': args.seed,
        'manifest_sha256': file_sha(manifest), 'training_code_sha256': file_sha(Path(__file__)),
        'torch': torch.__version__, 'gpu': torch.cuda.get_device_name(),
        'initialization': 'random; no external pretrained weights or downloads',
        'parameters': sum(p.numel() for p in model.parameters()), 'training_parameters': sum(p.numel() for p in model.parameters()),
        'input_shape': [3, HEIGHT, WIDTH], 'geometry_names': GEOMETRY_NAMES,
        'labels': dict(Counter(r['decision'] for r in records)),
        'original_lead_splits': dict(Counter(r['split'] for r in records)),
        'fit_images': len(fit), 'internal_dev_images': len(dev), 'validation_images': len(validation),
        'fit_ids': [r['image_id'] for r in fit], 'internal_dev_ids': [r['image_id'] for r in dev],
        'validation_ids': [r['image_id'] for r in validation],
        'excluded_training_detector_misses': [r['image_id'] for r in records if r['split'] == 'train'
                                             and not r['candidate_targets']['usable_for_ranking']],
        'external_data_used': False, 'original_test_images_used': 0, 'app_integrated': False,
        'selection': 'minimum internal-dev ranking loss; original lead validation never selects epoch or threshold',
        'epochs': args.epochs, 'learning_rate': args.lr, 'batch_size': args.batch_size,
        'preprocessing': 'RGB, bilinear aspect-preserving resize, centered 320x192 letterbox RGB114, xyxy top-left normalized; ImageNet mean/std constants only',
        'augmentation': 'training only horizontal flip, brightness .75..1.25 and contrast .8..1.2',
        'data_license': 'comma10k MIT; user lead annotations; notice retained',
        'architecture_license': 'torchvision BSD-3-Clause; installed license copied below',
        'status': 'training'}
    import torchvision
    distribution = Path(torchvision.__file__).parent.parent
    for license_path in distribution.glob('torchvision-*.dist-info/licenses/LICENSE'):
        shutil.copy2(license_path, out / 'licenses/torchvision-LICENSE.txt')
    atomic_json(out / 'provenance.json', provenance)
    atomic_json(out / 'status.json', {'status': 'training', 'epoch': 0, 'epochs': args.epochs})
    print(json.dumps({k: v for k, v in provenance.items() if not k.endswith('_ids') and 'excluded_' not in k}), flush=True)
    train_data, dev_data = LeadDataset(fit, root, True), LeadDataset(dev, root)
    loader = torch.utils.data.DataLoader(train_data, batch_size=args.batch_size, shuffle=True,
        num_workers=0, collate_fn=collate, generator=torch.Generator().manual_seed(args.seed))
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=.01)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs, eta_min=args.lr * .02)
    best, best_epoch, history = math.inf, 0, []
    for epoch in range(1, args.epochs + 1):
        model.train(); total, count = 0., 0
        for batch in loader:
            images, boxes, geom, valid, targets = [x.to(device) for x in batch]
            optimizer.zero_grad(set_to_none=True)
            loss = ranking_loss(model(images, boxes, geom, valid), targets)
            if not torch.isfinite(loss):
                raise RuntimeError('Nonfinite training loss')
            loss.backward(); nn.utils.clip_grad_norm_(model.parameters(), 5)
            optimizer.step(); total += float(loss.detach()) * len(images); count += len(images)
        scheduler.step()
        probabilities, dev_loss = infer(model, dev_data, device)
        dev_metrics = metrics(dev, picks(probabilities))
        entry = {'epoch': epoch, 'fit_loss': total / count, 'dev_loss': dev_loss,
                 'dev_accuracy': dev_metrics['decision_accuracy'], 'elapsed_seconds': time.time() - started}
        history.append(entry)
        if dev_loss < best:
            best, best_epoch = dev_loss, epoch
            torch.save({'state_dict': {k: v.detach().cpu() for k, v in model.state_dict().items()},
                        'recipe': RECIPE, 'epoch': epoch, 'provenance': provenance}, out / 'best.pt')
        atomic_json(out / 'status.json', {'status': 'training', 'best_epoch': best_epoch, **entry})
        print(json.dumps(entry), flush=True)
    state = torch.load(out / 'best.pt', map_location='cpu', weights_only=False)
    model.load_state_dict(state['state_dict']); model.eval()
    dev_probs, _ = infer(model, dev_data, device)
    # Fix the abstention threshold on internal development before touching validation.
    thresholds = [i / 20 for i in range(0, 20)]
    threshold = max(thresholds, key=lambda t: (metrics(dev, picks(dev_probs, t))['decision_accuracy'], t))
    validation_data = LeadDataset(validation, root)
    val_probs, _ = infer(model, validation_data, device, calculate_loss=False)
    selected = picks(val_probs, threshold)
    val_usable = [r for r in validation if r['candidate_targets']['usable_for_ranking']]
    usable_picks = [p for r, p in zip(validation, selected) if r['candidate_targets']['usable_for_ranking']]
    # A reproducible static geometry proxy, not the full iPhone lane/tracking algorithm.
    baseline = []
    for row in validation:
        options = [(i, d) for i, d in enumerate(row['detections']) if d['forward_candidate']]
        baseline.append(max(options, key=lambda pair: pair[1]['box'][3])[0] if options else None)
    result = {'status': 'complete', 'best_epoch': best_epoch, 'threshold': threshold,
        'threshold_source': 'internal development only', 'internal_development': metrics(dev, picks(dev_probs, threshold)),
        'validation_all': metrics(validation, selected), 'validation_detector_supported': metrics(val_usable, usable_picks),
        'validation_geometry_proxy': metrics(validation, baseline),
        'geometry_proxy_description': 'choose the forward_candidate whose box bottom is lowest; no abstention; not the full app algorithm',
        'checkpoint_sha256': file_sha(out / 'best.pt'), 'checkpoint_bytes': (out / 'best.pt').stat().st_size,
        'elapsed_seconds': time.time() - started, 'device_runtime_tested': False, 'app_integrated': False,
        'limitations': ['small selection-biased still-image sample', '94-session validation has not been used for training/model selection',
                       'detector-missed positives cannot be recovered by a candidate scorer',
                       'temporal switching and iPhone latency remain untested']}
    atomic_json(out / 'history.json', history)
    atomic_json(out / 'validation-predictions.json', [{'image_id': r['image_id'], 'decision': r['decision'],
        'selected_candidate': p, 'candidate_probabilities_and_none': values,
        'lead_iou': r['candidate_targets']['lead_iou']} for r, p, values in zip(validation, selected, val_probs)])
    atomic_json(out / 'metrics.json', result)
    atomic_json(out / 'status.json', result)
    provenance.update(status='complete', best_epoch=best_epoch, checkpoint_sha256=result['checkpoint_sha256'])
    atomic_json(out / 'provenance.json', provenance)
    print(json.dumps(result, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True); parser.add_argument('--root', required=True)
    parser.add_argument('--out', required=True); parser.add_argument('--epochs', type=int, default=80)
    parser.add_argument('--batch-size', type=int, default=16); parser.add_argument('--lr', type=float, default=.001)
    parser.add_argument('--seed', type=int, default=20261024)
    args = parser.parse_args()
    if args.epochs < 1 or args.batch_size < 2 or not math.isfinite(args.lr) or args.lr <= 0:
        parser.error('Invalid training settings')
    train(args)


if __name__ == '__main__':
    main()
