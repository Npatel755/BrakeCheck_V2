"""Validation-only predictions, matching and immutable error-review collections."""
import hashlib
import json
from pathlib import Path
import time

import cv2
import numpy as np
from scipy.optimize import linear_sum_assignment

from .bootstrap import atomic_json


def match_lanes(predicted, reference):
    def raster(points):
        mask = np.zeros((590, 1640), np.uint8)
        if len(points) >= 2:
            cv2.polylines(mask, [np.round(points).astype(np.int32)], False, 1, thickness=30)
        return mask.astype(bool)
    p, r = [raster(v) for v in predicted], [raster(v) for v in reference]
    overlaps = np.zeros((len(p), len(r)), np.float32)
    for i, a in enumerate(p):
        for j, b in enumerate(r):
            overlaps[i, j] = np.count_nonzero(a & b) / max(1, np.count_nonzero(a | b))
    pairs = []
    if p and r:
        a, b = linear_sum_assignment(-overlaps)
        pairs = [(int(i), int(j), float(overlaps[i, j])) for i, j in zip(a, b) if overlaps[i, j] >= .5]
    matched_p, matched_r = {v[0] for v in pairs}, {v[1] for v in pairs}
    return {'tp': len(pairs), 'fp': len(p) - len(pairs), 'fn': len(r) - len(pairs),
            'matches': pairs, 'false_positive_indices': [i for i in range(len(p)) if i not in matched_p],
            'missed_indices': [j for j in range(len(r)) if j not in matched_r]}


class ErrorCollection:
    def __init__(self, root, collection_id, snapshot, provenance):
        if not collection_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in collection_id):
            raise ValueError('Invalid error collection ID')
        self.path = Path(root) / 'reports/model_errors' / collection_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.snapshot, self.rows = Path(snapshot).resolve(), []
        self.metadata = {**provenance, 'id': collection_id, 'schema': 'lane_errors:v1', 'phase': 'building',
                         'snapshot': str(self.snapshot), 'created_at': time.time(),
                         'split': 'val', 'test_images_used': 0, 'labels_changed': False}
        atomic_json(self.path / 'metadata.json', self.metadata)

    def add(self, record, reference, predictions):
        if record['split'] != 'val':
            raise ValueError('Error review may contain validation images only')
        image_id = record['image_id']
        if not image_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in image_id):
            raise ValueError('Invalid image ID')
        policies = {}
        for policy, predicted in predictions.items():
            result = match_lanes(predicted, reference)
            policies[policy] = {**result, 'predicted': predicted}
        if not any(v['fp'] or v['fn'] for v in policies.values()):
            return policies
        payload = {'image_id': image_id, 'image': record['image'], 'split': 'val',
                   'source': record['source'], 'reference': reference, 'policies': policies}
        raw = json.dumps(payload, sort_keys=True).encode()
        payload['fingerprint'] = hashlib.sha256(raw).hexdigest()
        atomic_json(self.path / 'cases' / (image_id + '.json'), payload)
        self.rows.append({'image_id': image_id, 'source': record['source'],
                          'fingerprint': payload['fingerprint'],
                          'policies': {k: {v: item[v] for v in ('tp', 'fp', 'fn')} for k, item in policies.items()}})
        return policies

    def finish(self, metrics):
        atomic_json(self.path / 'index.json', self.rows)
        self.metadata.update(phase='complete', cases=len(self.rows), metrics=metrics, completed_at=time.time())
        atomic_json(self.path / 'metadata.json', self.metadata)
        return self.path
