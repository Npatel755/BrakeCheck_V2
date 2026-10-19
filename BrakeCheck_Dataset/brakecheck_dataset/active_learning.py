"""Conservative, reproducible gate for promoting a retrained teacher.

This module does not fine-tune or silently activate models. Those steps need
human-approved labels, a licensed checkpoint, and an independent evaluation.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np


def assess_candidate(baseline: dict, candidate: dict, *, minimum_cases: int = 100,
                     minimum_iou_gain: float = 0.01) -> dict:
    """Use paired, session-level bootstrap to reject noisy improvements.

    A source or checkpoint evaluated on a different test set cannot win by
    changing which images were measured. Group resampling by recording session
    prevents nearly adjacent frames from pretending to be independent samples.
    """
    if baseline.get("status") != "independent_human_reviewed_evaluation" or candidate.get("status") != "independent_human_reviewed_evaluation":
        raise ValueError("Both reports must come from independent, human-reviewed evaluation")
    if baseline.get("evaluation_set_digest") != candidate.get("evaluation_set_digest"):
        raise ValueError("Candidate and baseline use different fixed evaluation sets")
    old = {case["image_id"]: case for case in baseline.get("cases", [])}
    new = {case["image_id"]: case for case in candidate.get("cases", [])}
    if old.keys() != new.keys() or len(old) < minimum_cases:
        raise ValueError("Reports need identical case IDs and the minimum evaluation size")
    sessions: dict[tuple[str, str], list[float]] = {}
    for image_id in old:
        original, proposed = old[image_id], new[image_id]
        key = (original["source"], original["session_id"])
        if key != (proposed["source"], proposed["session_id"]):
            raise ValueError(f"Session identity changed for {image_id}")
        sessions.setdefault(key, []).append(proposed["iou"] - original["iou"])
    if len(sessions) < 10:
        raise ValueError("At least ten held-out sessions are needed for promotion")
    old_iou = float(np.mean([case["iou"] for case in old.values()]))
    new_iou = float(np.mean([case["iou"] for case in new.values()]))
    observed_gain = new_iou - old_iou
    session_gains = np.array([np.mean(values) for values in sessions.values()])
    rng = np.random.default_rng(20261016)
    indices = rng.integers(0, len(session_gains), size=(2000, len(session_gains)))
    lower_bound = float(np.percentile(session_gains[indices].mean(axis=1), 2.5))
    old_metrics, new_metrics = baseline["metrics"], candidate["metrics"]
    boundary_loss = old_metrics["boundary_f1"] - new_metrics["boundary_f1"]
    recall_loss = old_metrics["recall"] - new_metrics["recall"]
    approved = (observed_gain >= minimum_iou_gain and lower_bound > 0 and
                boundary_loss <= 0.01 and recall_loss <= 0.01)
    reasons = []
    if observed_gain < minimum_iou_gain:
        reasons.append("Mean lane IoU gain below threshold")
    if lower_bound <= 0:
        reasons.append("Session-bootstrap 95% lower bound does not exceed zero")
    if boundary_loss > 0.01:
        reasons.append("Boundary F1 regressed by more than 0.01")
    if recall_loss > 0.01:
        reasons.append("Lane recall regressed by more than 0.01")
    return {"promotion_recommended": approved, "reasons": reasons,
            "evaluation_set_digest": baseline["evaluation_set_digest"],
            "baseline_checkpoint": baseline["checkpoint_sha256"],
            "candidate_checkpoint": candidate["checkpoint_sha256"],
            "case_count": len(old), "session_count": len(sessions),
            "mean_iou_gain": observed_gain, "session_bootstrap_lower_95": lower_bound,
            "boundary_f1_regression": boundary_loss, "recall_regression": recall_loss,
            "note": "A recommendation is not activation; verify rights and deployment performance before switching."}


def compare_reports(baseline_path: Path, candidate_path: Path, output_path: Path) -> dict:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    decision = assess_candidate(baseline, candidate)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")
    return decision
