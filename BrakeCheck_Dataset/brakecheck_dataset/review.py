"""Prioritize disagreement with source masks without calling it model accuracy."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import numpy as np
from PIL import Image

from .pipeline import setup


LANE_COLOR = np.array((255, 0, 0), dtype=np.uint8)
ROAD_COLOR = np.array((64, 32, 32), dtype=np.uint8)


def overlap(prediction: np.ndarray, reference: np.ndarray) -> dict:
    prediction = prediction.astype(bool)
    reference = reference.astype(bool)
    true_positive = int(np.count_nonzero(prediction & reference))
    predicted = int(np.count_nonzero(prediction))
    labeled = int(np.count_nonzero(reference))
    union = predicted + labeled - true_positive
    return {"iou": true_positive / union if union else 1.0,
            "precision": true_positive / predicted if predicted else (1.0 if not labeled else 0.0),
            "recall": true_positive / labeled if labeled else (1.0 if not predicted else 0.0),
            "predicted_pixels": predicted, "source_pixels": labeled}


def make_review_queue(root: Path) -> dict:
    db = setup(root)
    rows = db.execute("""SELECT i.image_id,i.image_path,i.mask_path,i.split,
        p.lane_mask_path,p.road_mask_path,p.checkpoint_sha256
        FROM images i JOIN predictions p ON i.image_id=p.image_id
        WHERE i.source='comma10k' AND p.model_name='comma10k-segnet'
        ORDER BY i.image_id""").fetchall()
    cases = []
    for image_id, image_path, source_mask, split, lane_path, road_path, checkpoint in rows:
        with Image.open(root / source_mask) as image:
            labels = np.asarray(image.convert("RGB"))
        with Image.open(root / lane_path) as image:
            lane = np.asarray(image.convert("L")) > 0
        with Image.open(root / road_path) as image:
            road = np.asarray(image.convert("L")) > 0
        lane_result = overlap(lane, np.all(labels == LANE_COLOR, axis=2))
        road_result = overlap(road, np.all(labels == ROAD_COLOR, axis=2))
        if lane_result["iou"] < .50:
            priority = "high"
        elif lane_result["iou"] < .80:
            priority = "medium"
        else:
            # Keep a reproducible random QC sample from apparently easy cases.
            sample = int(hashlib.sha256(image_id.encode()).hexdigest()[:8], 16) % 10 == 0
            priority = "random_qc" if sample else "low"
        cases.append({"image_id": image_id, "image": image_path,
                      "original_mask": source_mask, "predicted_lane_mask": lane_path,
                      "split": split, "priority": priority, "lane": lane_result,
                      "road": road_result, "checkpoint_sha256": checkpoint,
                      "note": "Source-mask agreement only; checkpoint trained on comma10k, so this is not independent validation."})
    order = {"high": 0, "medium": 1, "random_qc": 2, "low": 3}
    cases.sort(key=lambda row: (order[row["priority"]], row["lane"]["iou"], row["image_id"]))
    path = root / "review_queue/comma-source-disagreements.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")
    counts = {priority: sum(case["priority"] == priority for case in cases)
              for priority in order}
    result = {"images": len(cases), "priority_counts": counts,
              "mean_lane_source_iou": round(sum(c["lane"]["iou"] for c in cases) / len(cases), 4) if cases else None,
              "mean_road_source_iou": round(sum(c["road"]["iou"] for c in cases) / len(cases), 4) if cases else None,
              "warning": "Same-source agreement is a pipeline sanity check, not an unbiased teacher benchmark."}
    (root / "reports/comma-source-agreement.json").write_text(json.dumps(result, indent=2) + "\n")
    db.close()
    return result
