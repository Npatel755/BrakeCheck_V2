"""Shared, dependency-free eligibility for optional copied-proposal approval.

Human acceptance can override advisory teacher QC. It cannot turn an error,
missing prediction, or malformed paint geometry into a training label. This
policy never changes proposal bytes, source labels, audits, or dataset splits.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re


_ADVISORY_REASONS = frozenset({
    "low_paint_confidence", "short_observed_paint_span",
    "flip_prediction_disagreement", "ambiguous_lane_geometry", "unsupported_curve",
})
_NON_PAINT = frozenset({"road_edge", "curb", "pavement_edge"})
_COPY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")


def preferred_proposal_bytes(run, copy_id):
    """Use a verified derived repair, retaining the original proposal verbatim.

    Selection changes only future snapshots and the displayed candidate. A
    running UFLD dataset is already frozen elsewhere and is never opened here.
    """
    if not isinstance(copy_id, str) or not _COPY_ID.fullmatch(copy_id) or ".." in copy_id:
        raise ValueError("Invalid proposal copy identifier")
    run = Path(run).resolve()

    def inside(directory):
        path = (run / directory / (copy_id + ".json")).resolve()
        if not path.is_relative_to(run):
            raise ValueError("Proposal path leaves its isolated experiment")
        return path

    original_bytes = inside("proposals").read_bytes()
    repaired_path = inside("proposals_hood_repaired")
    if not repaired_path.exists():
        return original_bytes
    original = json.loads(original_bytes)
    repaired_bytes = repaired_path.read_bytes()
    repaired = json.loads(repaired_bytes)
    if not isinstance(original, dict) or not isinstance(repaired, dict):
        raise ValueError("Invalid repaired proposal provenance")
    evidence = repaired.get("repair_provenance")
    original_teacher = original.get("teacher_provenance")
    repaired_teacher = repaired.get("teacher_provenance")
    if not all(isinstance(value, dict) for value in (evidence, original_teacher, repaired_teacher)):
        raise ValueError("Repaired proposal is missing its original provenance")
    expected_checkpoint = original_teacher.get("checkpoint_sha256")
    expected_input = original.get("input_sha256")
    policy = evidence.get("policy")
    if (evidence.get("original_proposal_sha256") != hashlib.sha256(original_bytes).hexdigest()
            or not isinstance(expected_checkpoint, str) or len(expected_checkpoint) != 64
            or not isinstance(expected_input, str) or len(expected_input) != 64
            or evidence.get("checkpoint_sha256") != expected_checkpoint
            or repaired_teacher.get("checkpoint_sha256") != expected_checkpoint
            or evidence.get("input_sha256") != expected_input
            or repaired.get("input_sha256") != expected_input
            or policy != "bottom-connected-broad-band-v1"
            or not isinstance(repaired.get("geometry"), dict)
            or repaired["geometry"].get("hood_crop_policy") != policy):
        raise ValueError("Repaired proposal no longer matches its original input, model or geometry policy")
    return repaired_bytes


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def proposal_approval_policy(proposal, *, width=None, height=None):
    """Return approval_allowed, block_reason and overrides_quality.

    Supply the copy's source width/height when available. Coordinates use source
    pixels, unique physical-paint slots 1..4, and at least two distinct rows per
    lane, matching the isolated UFLD exporter's structural geometry contract.
    Unknown QC holds fail closed rather than silently treating new errors as
    advisory. Passing this check does not change automatic QC eligibility.
    """
    def blocked(reason):
        return {"approval_allowed": False, "block_reason": reason, "overrides_quality": False}

    if not isinstance(proposal, dict):
        return blocked("The proposal is malformed; wait for a regenerated prediction.")
    if proposal.get("error"):
        return blocked("The model reported a prediction error; wait for a regenerated prediction.")
    geometry = proposal.get("geometry")
    if not isinstance(geometry, dict) or not isinstance(geometry.get("lanes"), list):
        return blocked("The proposal has no valid painted-line geometry.")
    width = geometry.get("width") if width is None else width
    height = geometry.get("height") if height is None else height
    if any(not _number(value) or value <= 0 or int(value) != value for value in (width, height)):
        return blocked("The source image dimensions are unavailable or invalid.")
    for field, expected in (("width", width), ("height", height)):
        if field in geometry and (not _number(geometry[field]) or geometry[field] != expected):
            return blocked("The proposal dimensions do not match this copied image.")
    if not geometry["lanes"]:
        return blocked("No painted lines were predicted. An empty model result is not a verified no-lines label.")
    slots = set()
    for lane in geometry["lanes"]:
        if not isinstance(lane, dict):
            return blocked("A proposed painted line is malformed.")
        slot = lane.get("slot")
        if isinstance(slot, bool) or not isinstance(slot, int) or slot not in (1, 2, 3, 4) or slot in slots:
            return blocked("Painted-line slots must be unique integers from 1 to 4.")
        slots.add(slot)
        kind = lane.get("kind")
        if (lane.get("physical", True) is not True
                or kind is not None and (not isinstance(kind, str) or kind in _NON_PAINT)):
            return blocked("This experiment accepts painted lines, not inferred boundaries or road edges.")
        points = lane.get("points")
        if not isinstance(points, list) or len(points) < 2:
            return blocked("Each painted line needs at least two valid points.")
        rows = set()
        for point in points:
            if (not isinstance(point, (list, tuple)) or len(point) < 2
                    or not _number(point[0]) or not _number(point[1])):
                return blocked("Painted-line coordinates must be finite numbers.")
            x, y = point[:2]
            if not 0 <= x < width or not 0 <= y < height:
                return blocked("Painted-line points must lie inside the copied image.")
            if y in rows:
                return blocked("A painted line has more than one point on the same row.")
            rows.add(y)
    quality = proposal.get("quality")
    if not isinstance(quality, dict) or not isinstance(quality.get("eligible_for_experiment"), bool):
        return blocked("The proposal quality report is unavailable; wait for regeneration.")
    reasons = quality.get("reasons", [])
    if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
        return blocked("The proposal quality report is malformed; wait for regeneration.")
    if any(reason not in _ADVISORY_REASONS for reason in reasons):
        return blocked("The quality report contains a structural or unsupported error; regenerate the proposal.")
    passes = quality["eligible_for_experiment"]
    if not passes and not reasons:
        return blocked("The quality hold has no explanation; wait for a complete quality report.")
    return {"approval_allowed": True, "block_reason": None, "overrides_quality": not passes}
