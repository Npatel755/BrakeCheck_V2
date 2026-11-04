"""Separate human labels for painted lane markings and visible road edges.

Missing legacy edge labels always mean ``not_reviewed``. They never mean that
an annotator checked the image and found no road edge.
"""
from __future__ import annotations

import copy
import math


LABELING_POLICY = {
    "version": "brakecheck-boundaries-v2",
    "painted_lanes": "visible_paint_only",
    "road_edges": "visible_road_surface_boundary",
}
ROAD_EDGE_SIDES = ("left", "right")
ROAD_EDGE_STATUSES = ("visible", "absent", "unknown", "not_reviewed")
ROAD_EDGE_KINDS = ("curb", "pavement_edge", "shoulder", "barrier", "other")


def default_road_edges() -> dict:
    """Return fresh, explicitly unreviewed left and right edge records."""
    return {side: {"status": "not_reviewed", "kind": "pavement_edge", "points": []}
            for side in ROAD_EDGE_SIDES}


def normalize_road_edges(road_edges=None) -> dict:
    """Fill missing records without inferring negative or visible labels.

    This does not check coordinates; use :func:`validate_road_edges` before
    saving or exporting submitted edges. Existing interpolation metadata is
    preserved, and callers always receive a copy.
    """
    result = default_road_edges()
    if road_edges is None:
        return result
    if not isinstance(road_edges, dict):
        raise ValueError("Road edges must contain left and right edge records")
    if any(side not in ROAD_EDGE_SIDES for side in road_edges):
        raise ValueError("Road edges may only use left and right")
    for side, edge in road_edges.items():
        if not isinstance(edge, dict):
            raise ValueError(f"The {side} road edge must be an object")
        status = edge.get("status", "not_reviewed")
        kind = edge.get("kind", "pavement_edge")
        if status not in ROAD_EDGE_STATUSES:
            raise ValueError(f"Invalid {side} road-edge status")
        if kind not in ROAD_EDGE_KINDS:
            raise ValueError(f"Invalid {side} road-edge kind")
        result[side] = {**copy.deepcopy(edge), "status": status, "kind": kind,
                        "points": copy.deepcopy(edge.get("points", []))}
    return result


def validate_road_edges(road_edges, width: int, height: int) -> dict:
    """Return validated edge records with exact, piecewise linear geometry.

    ``points`` retains the submitted order and floating-point coordinates.
    ``fit`` sorts those same coordinates by image row for interpolation; it
    never adds vertices, rounds rows, extrapolates, or fits a smooth curve.
    """
    normalized = normalize_road_edges(road_edges)
    result = {}
    for side, edge in normalized.items():
        status, kind, points = edge["status"], edge["kind"], edge["points"]
        if not isinstance(points, list):
            raise ValueError(f"The {side} road-edge points must be a list")
        record = {"status": status, "kind": kind, "points": []}
        if status != "visible":
            if points:
                raise ValueError(f"A {status} {side} road edge must have no points")
            result[side] = record
            continue
        if not 2 <= len(points) <= 500:
            raise ValueError(f"A visible {side} road edge needs between 2 and 500 points")
        clean = []
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError(f"Invalid {side} road-edge point; use [x, y]")
            x, y = point
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in (x, y)):
                raise ValueError("Road-edge coordinates must be numbers")
            if not math.isfinite(x) or not math.isfinite(y) or not (0 <= x < width and 0 <= y < height):
                raise ValueError("Road-edge points must lie inside the image")
            clean.append([float(x), float(y)])
        ys = [point[1] for point in clean]
        if len({round(y, 4) for y in ys}) != len(ys) or max(ys) - min(ys) < 2:
            raise ValueError("A road edge must span distinct image rows; remove duplicate points")
        ordered = sorted(clean, key=lambda point: point[1])
        record.update(points=clean,
                      fit={"basis": "interp", "ys": [point[1] for point in ordered],
                           "xs": [point[0] for point in ordered]},
                      y_top=min(ys), y_bottom=max(ys))
        result[side] = record
    return result


def with_label_policy(geometry: dict) -> dict:
    """Copy geometry, stamp policy, and expose explicit legacy edge defaults."""
    result = copy.deepcopy(geometry)
    result["labeling_policy"] = copy.deepcopy(LABELING_POLICY)
    result["road_edges"] = normalize_road_edges(result.get("road_edges"))
    return result
