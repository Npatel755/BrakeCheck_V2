"""Conservative, provisional lane-marking geometry from a semantic mask.

This is a candidate generator for human review. It does not identify the ego
lane, infer hidden boundaries, or turn comma.ai predictions into ground truth.
All coordinates use the original image's top-left pixel coordinate system.
"""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


def _row_centres(component: np.ndarray, x_offset: int, y_offset: int,
                 image_width: int) -> tuple[list[list[float]], bool, int]:
    """Measure actual mask pixels at each row; never synthesize gap points."""
    points: list[list[float]] = []
    ambiguous_rows = 0
    broad_rows = 0
    max_separation = max(10, round(image_width * .014))
    max_mark_width = max(28, round(image_width * .04))
    for local_y in range(component.shape[0]):
        xs = np.flatnonzero(component[local_y])
        if xs.size == 0:
            continue
        if xs[-1] - xs[0] > max_mark_width:
            broad_rows += 1
        if np.diff(xs).max(initial=0) > max_separation:
            ambiguous_rows += 1
            continue
        points.append([float(x_offset + np.median(xs)), float(y_offset + local_y)])
    return points, ambiguous_rows > max(2, len(points) // 6), broad_rows


def _thin_points(points: list[list[float]], image_height: int) -> list[list[float]]:
    stride = max(2, round(image_height / 300))
    kept = points[::stride]
    if kept and kept[-1] != points[-1]:
        kept.append(points[-1])
    return kept


def _fit_rmse(points: list[list[float]]) -> float:
    if len(points) < 5:
        return float("inf")
    values = np.asarray(points, dtype=np.float64)
    y = values[:, 1]
    degree = min(2, len(np.unique(y)) - 1)
    if degree < 1:
        return float("inf")
    fitted = np.polyval(np.polyfit(y, values[:, 0], degree), y)
    return float(np.sqrt(np.mean((fitted - values[:, 0]) ** 2)))


def _join_score(upper: dict, lower: dict, image_width: int,
                image_height: int) -> float | None:
    first = upper["points"]
    second = lower["points"]
    gap = second[0][1] - first[-1][1]
    if not 1 < gap <= max(45, image_height * .09):
        return None
    tail = first[max(0, len(first) - 6):]
    dy = tail[-1][1] - tail[0][1]
    slope = (tail[-1][0] - tail[0][0]) / dy if dy else 0.0
    predicted_x = first[-1][0] + slope * gap
    distance = abs(predicted_x - second[0][0])
    max_distance = max(12, image_width * .018) + gap * .08
    if distance > max_distance:
        return None
    return distance / max_distance + gap / max(45, image_height * .09)


def extract_lane_geometry(mask: np.ndarray, image_id: str,
                          source_mask: str | None = None) -> dict:
    """Return physically visible marking candidates, with dashed gaps intact.

    Each connected paint component becomes one visible polyline. Components may
    be grouped as a dashed candidate only when their endpoints agree. The
    flattened ``points`` key helps simple viewers; consumers should use
    ``visible_segments`` to avoid drawing across an unobserved gap.
    """
    if mask.ndim != 2 or not np.issubdtype(mask.dtype, np.number):
        raise ValueError("Expected one numeric lane-mask channel")
    height, width = mask.shape
    if not 0 < width <= 16_384 or not 0 < height <= 16_384:
        raise ValueError("Invalid lane-mask dimensions")
    binary = (mask > 0).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    candidates: list[dict] = []
    rejected = {"tiny_or_horizontal": 0, "broad_or_forked": 0}
    min_area = max(16, round(width * height * .000012))
    min_height = max(8, round(height * .009))
    for component_id in range(1, count):
        x, y, span_x, span_y, area = map(int, stats[component_id])
        if area < min_area or span_y < min_height or span_x > span_y * 4:
            rejected["tiny_or_horizontal"] += 1
            continue
        piece = labels[y:y + span_y, x:x + span_x] == component_id
        points, forked, broad_rows = _row_centres(piece, x, y, width)
        if forked or broad_rows > max(3, len(points) // 5) or len(points) < 6:
            rejected["broad_or_forked"] += 1
            continue
        polyline = _thin_points(points, height)
        if len(polyline) < 2:
            rejected["tiny_or_horizontal"] += 1
            continue
        rmse = _fit_rmse(polyline)
        candidates.append({"points": polyline, "component_id": component_id,
                           "fit_rmse_px": round(rmse, 2)})

    # Match unambiguous endpoints, not arbitrary nearby components. Each
    # component can have at most one predecessor and one successor.
    possibilities = []
    for upper_index, upper in enumerate(candidates):
        for lower_index, lower in enumerate(candidates):
            if upper_index == lower_index:
                continue
            score = _join_score(upper, lower, width, height)
            if score is not None:
                possibilities.append((score, upper_index, lower_index))
    successors: dict[int, int] = {}
    predecessors: dict[int, int] = {}
    for score, upper_index, lower_index in sorted(possibilities):
        if upper_index in successors or lower_index in predecessors:
            continue
        alternatives_upper = [item for item in possibilities
                              if item[1] == upper_index and item[2] != lower_index]
        alternatives_lower = [item for item in possibilities
                              if item[2] == lower_index and item[1] != upper_index]
        if any(other[0] <= score * 1.25 for other in alternatives_upper + alternatives_lower):
            continue
        successors[upper_index] = lower_index
        predecessors[lower_index] = upper_index

    lanes = []
    visited: set[int] = set()
    for start in range(len(candidates)):
        if start in predecessors or start in visited:
            continue
        members = []
        current = start
        while current not in visited:
            visited.add(current)
            members.append(candidates[current])
            if current not in successors:
                break
            current = successors[current]
        segments = [member["points"] for member in members]
        flags = []
        if len(segments) > 1:
            flags.append("dashed_group_candidate")
        if max(member["fit_rmse_px"] for member in members) > max(8, width * .012):
            flags.append("curvature_review")
        lanes.append({"kind": "physical", "status": "provisional",
                      "points": [point for segment in segments for point in segment],
                      "visible_segments": segments,
                      "source_component_ids": [member["component_id"] for member in members],
                      "fit_rmse_px": max(member["fit_rmse_px"] for member in members),
                      "flags": flags})
    lanes.sort(key=lambda lane: lane["points"][-1][0])
    for index, lane in enumerate(lanes, 1):
        lane["lane_id"] = f"candidate-{index}"
    flags = []
    if not lanes:
        flags.append("no_usable_marking_candidates")
    if len(lanes) > 4:
        flags.append("more_than_four_candidates_review_required")
    return {"schema_version": 1, "image_id": image_id, "width": width,
            "height": height, "coordinate_system": "image_pixels_top_left",
            "status": "provisional", "source_mask": source_mask,
            "lanes": lanes, "rejected_components": rejected, "flags": flags}


def extract_lane_geometry_file(mask_path: Path, image_id: str,
                               dataset_root: Path | None = None) -> dict:
    with Image.open(mask_path) as opened:
        mask = np.asarray(opened.convert("L"))
    source = (mask_path.relative_to(dataset_root).as_posix()
              if dataset_root is not None else str(mask_path))
    return extract_lane_geometry(mask, image_id, source)
