"""Visual overlays for spot checks: image | lane mask + fitted lanes | per-teacher maps."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .db import Registry
from .fusion import load_fused
from .lanes import current_geometry
from .quality import read_rgb, write_png
from .teachers.base import load_output

SLOT_COLORS = {1: (255, 160, 0), 2: (255, 60, 60), 3: (60, 160, 255), 4: (0, 220, 120), None: (200, 200, 200)}


def overlay(registry: Registry, image_id: str, teachers: list[str], width: int = 640) -> np.ndarray | None:
    row = registry.image(image_id)
    rgb = read_rgb(row["path"]) if row else None
    if rgb is None:
        return None
    H, W = rgb.shape[:2]
    panels = []
    base = rgb.copy()
    fused = load_fused(registry, image_id)
    if fused is not None:
        lane = fused[1]["lane"]
        tint = base.copy()
        tint[lane >= 0.5] = (255, 255, 0)
        base = cv2.addWeighted(base, 0.55, tint, 0.45, 0)
    cur = current_geometry(registry, image_id)
    if cur is not None:
        origin, geo = cur
        for l in geo["lanes"]:
            pts = np.array([[p[0], p[1]] for p in l["points"]], np.float32)
            col = SLOT_COLORS.get(l.get("slot"), (200, 200, 200))
            cv2.polylines(base, [np.round(pts).astype(np.int32)], False, col, thickness=max(2, W // 300))
            if l.get("slot"):
                x, y = pts[0]
                cv2.putText(base, str(l["slot"]), (int(x) + 4, int(y) - 4), cv2.FONT_HERSHEY_SIMPLEX, W / 900, col, 2)
        if "horizon_y" in geo:
            cv2.line(base, (0, geo["horizon_y"]), (W, geo["horizon_y"]), (120, 120, 120), 1)
        cv2.putText(base, f"{origin} lanes", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, W / 1100, (255, 255, 255), 2)
    panels.append(base)
    for t in teachers:
        ann = registry.annotation(image_id, "teacher", t)
        if ann is None or not Path(ann["path"]).exists():
            continue
        out = load_output(ann["path"])
        if out.lane is None:
            continue
        heat = cv2.applyColorMap((out.lane * 255).astype(np.uint8), cv2.COLORMAP_INFERNO)[:, :, ::-1]
        p = cv2.addWeighted(rgb, 0.4, heat, 0.6, 0)
        cv2.putText(p, t, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, W / 1100, (255, 255, 255), 2)
        panels.append(p)
    scale = width / W
    panels = [cv2.resize(p, (width, int(H * scale)), interpolation=cv2.INTER_AREA) for p in panels]
    return np.concatenate(panels, axis=1)


def write_overlays(settings, registry: Registry, rows, out_dir: Path, teachers: list[str]) -> int:
    n = 0
    for r in rows:
        img = overlay(registry, r["image_id"], teachers)
        if img is not None:
            write_png(out_dir / f"{r['image_id']}.png", cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
            n += 1
    return n
