"""Synthetic road scenes with known lanes, for testing geometry, fusion, QC and export without real data."""
from __future__ import annotations

import cv2
import numpy as np

H, W = 874, 1164


def lane_x(xb: float, y: np.ndarray | float, vx: float, vy: float, bottom: float, curve: float = 0.0):
    """Flat road seen by a pinhole camera: x = cx + A*t + B/t, with t ∝ (y - horizon).

    A lane at lateral offset X0 on a road of curvature k: X(Z) = X0 + k Z^2 / 2 and Z ∝ 1/t, so the
    offset gives the linear term and the curvature a shared B/t term (B = `curve`, px at t=1).
    """
    t = np.maximum(1e-3, (np.asarray(y, float) - vy) / (bottom - vy))   # 0 at horizon, 1 at bottom
    return vx + (xb - vx - curve) * t + curve / t


def scene(lanes=(("solid", 230), ("dashed", 470), ("dashed", 700), ("solid", 940)), curve: float = 0.0,
          stop_line: bool = False, arrow: bool = False, hood: bool = True, double_at: float | None = None,
          seed: int = 0, noise: float = 6.0):
    """Returns rgb (H,W,3), class map (0 unknown,1 road,2 lane,5 my car), and truth {slot: (ys, xs)}.

    lanes: (kind, x at the bottom of the visible road) left to right; slots assigned 1..4 by position
    around the image centre like the pipeline does.
    """
    rng = np.random.default_rng(seed)
    vx, vy = W / 2 + 10, int(0.40 * H)
    bottom = 720 if hood else H - 1
    rgb = np.zeros((H, W, 3), np.uint8)
    rgb[:vy] = (150, 180, 215)
    road = np.zeros((H, W), bool)
    for y in range(vy, H):
        xl = lane_x(-200, y, vx, vy, bottom, curve)
        xr = lane_x(W + 200, y, vx, vy, bottom, curve)
        road[y, max(0, int(xl)):min(W, int(xr))] = True
    rgb[vy:] = (95, 95, 95)
    rgb[road] = (70, 70, 72)
    cls = np.zeros((H, W), np.uint8)
    cls[road] = 1
    cls[vy:][~road[vy:]] = 3
    truth = {}
    xs_bottom = [x for _, x in lanes]
    centre = W / 2
    left = sorted([x for x in xs_bottom if x < centre], reverse=True)
    right = sorted([x for x in xs_bottom if x >= centre])
    slot_of = {}
    for s, x in zip((2, 1), left):
        slot_of[x] = s
    for s, x in zip((3, 4), right):
        slot_of[x] = s
    lane_mask = np.zeros((H, W), np.uint8)
    y0 = vy + 8
    for kind, xb in lanes:
        ys = np.arange(y0, bottom + 1)
        xs = lane_x(xb, ys, vx, vy, bottom, curve)
        draw = np.ones_like(ys, bool)
        if kind == "dashed":
            # dash/gap lengths shrink with depth like real paint (3 m dash, 9 m gap)
            depth = 1.0 / np.maximum(1e-3, (ys - vy) / (bottom - vy))
            draw = (np.floor(depth * 4.0) % 4) == 0
        for y, x, d in zip(ys, xs, draw):
            if not d:
                continue
            t = (y - vy) / (bottom - vy)
            w = max(2, int(round(16 * t)))
            for off in ([0] if double_at is None or abs(xb - double_at) > 1 else [-w * 1.0, w * 1.0]):
                lane_mask[y, max(0, int(x + off - w / 2)):min(W, int(x + off + w / 2) + 1)] = 1
        if xb in slot_of:
            truth[slot_of[xb]] = (ys, xs)
    if stop_line:
        lane_mask[640:665, 250:950] = 1
    if arrow:
        pts = np.array([[560, 600], [600, 560], [640, 600], [615, 600], [615, 660], [585, 660], [585, 600]], np.int32)
        cv2.fillPoly(lane_mask, [pts], 1)
    rgb[lane_mask > 0] = (235, 235, 230)
    cls[lane_mask > 0] = 2
    if hood:
        rgb[bottom + 5:] = (20, 20, 25)
        cls[bottom + 5:] = 5
    rgb = np.clip(rgb.astype(np.float32) + rng.normal(0, noise, rgb.shape), 0, 255).astype(np.uint8)
    return rgb, cls, truth


def lane_prob_from_classes(cls: np.ndarray) -> np.ndarray:
    return (cls == 2).astype(np.float32)
