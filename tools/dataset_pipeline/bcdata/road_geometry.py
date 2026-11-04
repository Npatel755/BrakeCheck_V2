"""Conservative road-mask contours for review, never human road-edge labels.

Only model predictions enter this function. A segmentation contour is an
approximation of a road edge; it cannot establish curb identity or traversability.
Unreliable sides are unknown, never automatic negative examples.
"""
from __future__ import annotations

import cv2
import numpy as np


def road_edges_from_probability(prob, *, horizon_y=.42, confidence_threshold=.5,
                                min_span_frac=.08, hood_mask=None, occlusion_mask=None):
    prob = np.asarray(prob, dtype=np.float32)
    if prob.ndim != 2 or min(prob.shape) < 8 or not np.isfinite(prob).all():
        raise ValueError("Road probability must be a finite H x W image")
    h, w = prob.shape
    result = {side: {"status": "unknown", "kind": "other", "points": [],
                     "reason": "No reliable model road contour", "automatic": True}
              for side in ("left", "right")}
    top = max(0, min(h - 1, int(horizon_y * h)))
    mask = (prob >= confidence_threshold).astype(np.uint8)
    mask[:top] = 0
    excluded = np.zeros((h, w), np.uint8)
    for supplied in (hood_mask, occlusion_mask):
        if supplied is not None:
            supplied = np.asarray(supplied)
            if supplied.shape != prob.shape:
                raise ValueError("Exclusion masks must match the road image")
            excluded |= (supplied >= confidence_threshold).astype(np.uint8)
    if excluded.any():
        radius = max(3, int(.012 * w))
        excluded = cv2.dilate(excluded, np.ones((radius * 2 + 1, radius * 2 + 1), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count < 2:
        return result
    # Prefer a road component under the camera's forward region. No invented
    # straight-ahead path or human mask is used to join disconnected regions.
    seeds = labels[int(.56 * h):int(.90 * h), int(.30 * w):int(.70 * w)]
    support = np.bincount(seeds.ravel(), minlength=count)
    support[0] = 0
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_AREA] < max(32, .005 * h * w):
            support[label] = 0
    chosen = int(support.argmax())
    if chosen == 0 or support[chosen] < .002 * h * w:
        return result
    component = labels == chosen
    margin, step = max(2, int(.008 * w)), max(2, h // 180)
    min_span = max(8, int(min_span_frac * h))
    tracks = {side: [] for side in result}
    runs = {side: [] for side in result}

    def finish(side):
        if tracks[side]:
            runs[side].append(tracks[side])
            tracks[side] = []

    for y in range(min(h - 2, int(.97 * h)), top + 2, -step):
        xs = np.flatnonzero(component[y])
        # Fragmented rows caused by obstacles/junctions do not define reliable
        # left/right limits of one contiguous road surface.
        if len(xs) < 3 or len(xs) / (xs[-1] - xs[0] + 1) < .60:
            for side in result:
                finish(side)
            continue
        for side, x in (("left", int(xs[0])), ("right", int(xs[-1]))):
            if x <= margin or x >= w - 1 - margin or excluded[y, x]:
                finish(side)
                continue
            current = tracks[side]
            if current and abs(x - current[-1][0]) > 3.0 * (current[-1][1] - y) + 2:
                finish(side)
            tracks[side].append([float(x), float(y)])
    for side in result:
        finish(side)
        eligible = [run for run in runs[side] if len(run) >= 3 and run[0][1] - run[-1][1] >= min_span]
        if not eligible:
            continue
        best = max(eligible, key=lambda run: run[0][1] - run[-1][1])
        # Keep curved boundaries while reducing the number of touch handles.
        points = cv2.approxPolyDP(np.asarray(best, np.float32).reshape(-1, 1, 2), 1.5, False).reshape(-1, 2).tolist()
        if len(points) < 2:
            continue
        confidence = float(np.mean([prob[int(y), int(x)] for x, y in best]))
        result[side] = {"status": "visible", "kind": "other", "points": points,
                        "confidence": round(confidence, 4), "automatic": True,
                        "flags": ["model_road_contour_requires_human_review"]}
    return result
