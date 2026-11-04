"""Semantic-segmentation export (road / lane marking), for training a road-segmentation model.

classes: 0 other, 1 road, 2 lane marking, 255 ignore (uncertain pixels of automatic labels).
Human/source labels are exported as-is; automatic labels mark the fusion's uncertain band as ignore.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import cv2
import numpy as np

from .config import Settings
from .db import Registry
from .export_ufld import eligible_rows
from .fusion import load_fused
from .quality import write_png

CLASSES = {"0": "other", "1": "road", "2": "lane_marking", "255": "ignore"}


def export(settings: Settings, registry: Registry, out: Path | None = None) -> dict:
    out = Path(out or settings.path("exports", "semantic_segmentation", "BrakeCheck_RoadLane"))
    low, high = float(settings.get("fusion.uncertain_low", 0.3)), float(settings.get("fusion.uncertain_high", 0.7))
    rows = eligible_rows(settings, registry)
    lists: dict[str, list[str]] = {"train": [], "val": [], "test": []}
    manifest = []
    counts = {"eligible": len(rows), "exported": 0, "no_mask": 0}
    for r in rows:
        fused = load_fused(registry, r["image_id"])
        if fused is None:
            counts["no_mask"] += 1
            continue
        record, maps = fused
        lane, road = maps["lane"], maps.get("road")
        label = np.zeros(lane.shape, np.uint8)
        if road is not None:
            label[road >= 0.5] = 1
        label[lane >= 0.5] = 2
        auto = record.get("mode") != "source_label"
        if auto:
            unc = ((lane > low) & (lane < high))
            if road is not None:
                unc |= (road > low) & (road < high)
            label[unc] = 255
        human = registry.annotation(r["image_id"], "human")
        if human is not None:                          # reviewed lanes override the lane class near the polylines
            geo = json.loads(Path(human["path"]).read_text())
            label[label == 2] = 1 if road is not None else 0
            for l in geo["lanes"]:
                pts = np.array([[p[0], p[1]] for p in l["points"]], np.float32)
                cv2.polylines(label, [np.round(pts).astype(np.int32)], False, 2, thickness=max(3, r["width"] // 300))
        rel_img = f"images/{r['split']}/{r['image_id']}{Path(r['path']).suffix.lower()}"
        rel_lbl = f"labels/{r['split']}/{r['image_id']}.png"
        (out / rel_img).parent.mkdir(parents=True, exist_ok=True)
        if not (out / rel_img).exists():
            (out / rel_img).write_bytes(Path(r["path"]).read_bytes())
        write_png(out / rel_lbl, label)
        lists[r["split"]].append(f"{rel_img} {rel_lbl}")
        manifest.append({"image": rel_img, "label": rel_lbl, "image_id": r["image_id"], "split": r["split"],
                         "source": r["source"], "license": r["license_id"], "pool": r["pool"], "synthetic": r["synthetic"],
                         "label_origin": "human" if human is not None else ("source" if not auto else "auto")})
        counts["exported"] += 1
    for split, lines in lists.items():
        (out / f"{split}.txt").write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    (out / "classes.json").write_text(json.dumps(CLASSES, indent=1))
    with open(out / "manifest.csv", "w", newline="", encoding="utf-8") as f:
        if manifest:
            w = csv.DictWriter(f, fieldnames=list(manifest[0]))
            w.writeheader()
            w.writerows(manifest)
    counts["out"] = str(out)
    return counts
