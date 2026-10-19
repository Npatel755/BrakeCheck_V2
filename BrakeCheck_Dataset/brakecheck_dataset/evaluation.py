"""Evaluate teachers only against an explicitly reviewed, held-out fixed set.

The comma10k teacher was trained on comma10k. Its current source-mask overlap
is useful for pipeline debugging, but this module will reject it as an
independent benchmark even if those masks are later imported into a manifest.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image


def _safe_file(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
        raise ValueError(f"Missing or unsafe evaluation file: {relative}")
    return candidate


def _mask(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        if image.mode not in ("1", "L"):
            raise ValueError(f"Expected a single-channel lane mask: {path}")
        pixels = np.asarray(image.convert("L"))
    if not np.isin(pixels, [0, 255]).all():
        raise ValueError(f"Expected binary values 0 and 255: {path}")
    return pixels > 0


def mask_metrics(predicted: np.ndarray, reviewed: np.ndarray, tolerance_px: int = 3) -> dict:
    """Return pixel and boundary metrics; missing masks never score as perfect."""
    if predicted.shape != reviewed.shape or predicted.ndim != 2:
        raise ValueError("Prediction and reviewed lane masks must have the same 2-D shape")
    if not reviewed.any():
        raise ValueError("Empty reviewed lane mask requires explicit no-lane evaluation protocol")
    predicted = predicted.astype(bool)
    reviewed = reviewed.astype(bool)
    tp = int(np.count_nonzero(predicted & reviewed))
    fp = int(np.count_nonzero(predicted & ~reviewed))
    fn = int(np.count_nonzero(~predicted & reviewed))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    kernel = np.ones((3, 3), np.uint8)
    pred_boundary = cv2.morphologyEx(predicted.astype(np.uint8), cv2.MORPH_GRADIENT, kernel) > 0
    truth_boundary = cv2.morphologyEx(reviewed.astype(np.uint8), cv2.MORPH_GRADIENT, kernel) > 0
    radius = 2 * tolerance_px + 1
    dilate_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius, radius))
    near_truth = cv2.dilate(truth_boundary.astype(np.uint8), dilate_kernel) > 0
    near_pred = cv2.dilate(pred_boundary.astype(np.uint8), dilate_kernel) > 0
    boundary_precision = (float(np.count_nonzero(pred_boundary & near_truth)) /
                          max(1, int(np.count_nonzero(pred_boundary))))
    boundary_recall = (float(np.count_nonzero(truth_boundary & near_pred)) /
                       max(1, int(np.count_nonzero(truth_boundary))))
    boundary_f1 = (2 * boundary_precision * boundary_recall /
                   (boundary_precision + boundary_recall)) if boundary_precision + boundary_recall else 0.0
    return {"iou": tp / (tp + fp + fn), "precision": precision, "recall": recall,
            "boundary_f1": boundary_f1, "false_positive_pixels": fp,
            "false_positive_pixels_per_megapixel": fp * 1_000_000 / reviewed.size,
            "tp": tp, "fp": fp, "fn": fn}


def validate_manifest(root: Path, manifest: dict[str, Any], model: dict[str, Any]) -> list[dict]:
    """Fail closed on unreviewed labels, source overlap, or session leakage."""
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Evaluation manifest needs a nonempty cases list")
    source_training = set(model.get("training_sources", []))
    session_training = set(model.get("training_sessions", []))
    if not source_training and not session_training:
        raise ValueError("Model training provenance is required for an independent evaluation")
    sources = json.loads((root / "config/sources.json").read_text(encoding="utf-8"))
    seen_ids: set[str] = set()
    seen_sessions: set[tuple[str, str]] = set()
    validated = []
    for case in cases:
        image_id = case.get("image_id")
        source = case.get("source")
        session = case.get("session_id")
        if not all(isinstance(x, str) and x for x in (image_id, source, session)):
            raise ValueError("Every case needs image_id, source and session_id")
        if image_id in seen_ids:
            raise ValueError(f"Duplicate evaluation image ID: {image_id}")
        seen_ids.add(image_id)
        seen_sessions.add((source, session))
        if case.get("split") != "test" or case.get("review_status") != "approved":
            raise ValueError(f"{image_id} is not an approved, held-out test case")
        if not case.get("reviewer_id") or not case.get("reviewed_at"):
            raise ValueError(f"{image_id} lacks review provenance")
        if sources.get(source, {}).get("pool") != "commercial" or sources[source].get("license_status") != "cleared":
            raise PermissionError(f"Evaluation source is not cleared: {source}")
        if source in source_training or f"{source}:{session}" in session_training:
            raise ValueError(f"Training/evaluation overlap for {image_id}: {source}:{session}")
        ground_truth = _safe_file(root, case["reviewed_lane_mask"])
        _mask(ground_truth)
        validated.append({**case, "truth_sha256": hashlib.sha256(ground_truth.read_bytes()).hexdigest()})
    if len(seen_sessions) == 0:
        raise ValueError("Evaluation set has no sessions")
    return validated


def evaluate(root: Path, manifest_path: Path, model_name: str,
             checkpoint_sha256: str, model_provenance: dict[str, Any]) -> dict:
    """Score matching provisional masks and save a fixed-set report.

    Manifest paths are relative to ``root``. Prediction paths must be supplied
    under each case's ``predictions[model_name]`` mapping so the same cases can
    be compared across model versions without changing the truth set.
    """
    if len(checkpoint_sha256) != 64 or any(c not in "0123456789abcdef" for c in checkpoint_sha256):
        raise ValueError("Checkpoint must be identified by a lowercase SHA-256 digest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = validate_manifest(root, manifest, model_provenance)
    digest_material = [{key: case[key] for key in ("image_id", "source", "session_id", "truth_sha256")}
                       for case in sorted(cases, key=lambda row: row["image_id"])]
    evaluation_set_digest = hashlib.sha256(json.dumps(digest_material, sort_keys=True).encode()).hexdigest()
    measured = []
    for case in cases:
        prediction = case.get("predictions", {}).get(model_name)
        if not prediction:
            raise ValueError(f"Missing {model_name} prediction for {case['image_id']}")
        if prediction.get("checkpoint_sha256") != checkpoint_sha256:
            raise ValueError(f"Checkpoint mismatch for {case['image_id']}")
        truth = _mask(_safe_file(root, case["reviewed_lane_mask"]))
        pred = _mask(_safe_file(root, prediction["lane_mask"]))
        measured.append({"image_id": case["image_id"], "source": case["source"],
                         "session_id": case["session_id"],
                         **mask_metrics(pred, truth)})
    metric_names = ("iou", "precision", "recall", "boundary_f1",
                    "false_positive_pixels_per_megapixel")
    averages = {name: float(np.mean([row[name] for row in measured])) for name in metric_names}
    report = {"status": "independent_human_reviewed_evaluation", "model_name": model_name,
              "checkpoint_sha256": checkpoint_sha256, "evaluation_set_digest": evaluation_set_digest,
              "image_count": len(measured), "session_count": len({(r['source'], r['session_id']) for r in measured}),
              "metrics": averages, "cases": measured,
              "unmeasured": ["lane_instance_completeness", "centerline_distance_error",
                             "human_correction_rate", "review_time_per_image"],
              "note": "Unmeasured values require reviewed lane-instance geometry or review event history."}
    output = root / "reports/evaluations" / model_name
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{checkpoint_sha256[:16]}-{evaluation_set_digest[:16]}.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
