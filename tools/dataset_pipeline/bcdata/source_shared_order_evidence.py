"""Prove existing slot order from shared native paint, without changing labels.

The caller supplies current complete native-target evidence for these inputs.
This narrower receipt can explain mixed-depth x_ref ordering metadata only;
all other QC, retention, regression and fresh human audit gates remain active.
"""
from __future__ import annotations

import math

import numpy as np

from .geometry import lane_xs

VERSION = "native-shared-paint-order-v1"


def _number(value):
    return (not isinstance(value, (bool, np.bool_))
            and isinstance(value, (int, float, np.integer, np.floating))
            and math.isfinite(value))


def _fit_valid(fit, top, bottom):
    if not isinstance(fit, dict):
        return False
    if fit.get("basis") == "interp":
        ys, xs = fit.get("ys"), fit.get("xs")
        return (isinstance(ys, (list, tuple)) and isinstance(xs, (list, tuple))
                and len(ys) == len(xs) and len(ys) >= 2
                and all(_number(value) for value in (*ys, *xs))
                and all(a < b for a, b in zip(ys, ys[1:]))
                and ys[0] <= top and ys[-1] >= bottom)
    if fit.get("basis") not in ("poly", "persp"):
        return False
    coefficients = fit.get("coef")
    if (not isinstance(coefficients, (list, tuple)) or not coefficients
            or not all(_number(value) for value in coefficients)):
        return False
    if fit["basis"] == "persp":
        return len(coefficients) == 3 and _number(fit.get("v"))
    return (_number(fit.get("s", 1)) and fit.get("s", 1) != 0
            and _number(fit.get("y0", 0)))


def _measured_rows(lane, paint, width, height, horizon, valid_bottom):
    top, bottom = lane.get("y_top"), lane.get("y_bottom")
    if (not _number(top) or not _number(bottom) or top != int(top) or bottom != int(bottom)
            or not horizon <= top < bottom <= valid_bottom
            or not _fit_valid(lane.get("fit"), top, bottom)
            or lane.get("x_ref") is not None and not _number(lane["x_ref"])):
        return None
    points = lane.get("points")
    if not isinstance(points, list) or len(points) < 2:
        return None
    declared = []
    for point in points:
        if (not isinstance(point, (list, tuple)) or len(point) not in (2, 3)
                or not all(_number(value) for value in point[:2])
                or not 0 <= point[0] < width or not top <= point[1] <= bottom
                or point[1] != int(point[1])
                or len(point) == 3 and type(point[2]) is not bool):
            return None
        x, y = point[:2]
        fitted = float(lane_xs(lane, y))
        if not math.isfinite(fitted) or abs(fitted - x) > 2.:
            return None
        observed = len(point) < 3 or point[2]
        if observed and (not 0 <= round(x) < width or not paint[int(y), round(x)]):
            return None
        declared.append((float(x), int(y), observed))
    ordered = sorted(declared, key=lambda point: point[1])
    if (len({point[1] for point in ordered}) != len(ordered)
            or ordered[0][1] > top or ordered[-1][1] < bottom):
        return None
    rows = np.arange(int(top), int(bottom) + 1)
    xs = np.asarray(lane_xs(lane, rows), dtype=float)
    if (xs.shape != rows.shape or not np.isfinite(xs).all()
            or np.any(xs < 0) or np.any(xs >= width)):
        return None
    columns = np.rint(xs).astype(int)
    if np.any(columns < 0) or np.any(columns >= width):
        return None
    support = paint[rows, columns].copy()
    # Hidden points and spans bounded by a hidden point cannot supply an
    # observed ordering edge, even when another paint stripe lies there.
    for first, second in zip(ordered, ordered[1:]):
        if not first[2] or not second[2]:
            support[(rows >= first[1]) & (rows <= second[1])] = False
    gaps = lane.get("gaps", [])
    if not isinstance(gaps, list):
        return None
    for gap in gaps:
        if (not isinstance(gap, (list, tuple)) or len(gap) != 2
                or not all(_number(value) for value in gap)
                or not top <= gap[0] < gap[1] <= bottom):
            return None
        support[(rows > gap[0]) & (rows < gap[1])] = False
    return rows[support], xs[support]


def measure(geometry, probability, native_evidence):
    """Return strict source-only slot-order evidence, or None on any doubt.

    No x_ref, source_observed_rows metadata, inferred gap, extrapolation, or
    separate per-lane row ranges can establish a pair's measured ordering.
    The input geometry and native pixels are never modified.
    """
    try:
        if (not isinstance(native_evidence, dict)
                or native_evidence.get("version") != "native-four-target-evidence-v2"
                or native_evidence.get("eligible_for_fresh_audit") is not True
                or any(native_evidence.get(key) for key in (
                    "unrepresented_components", "unhandled_converter_warnings", "crossings", "near_duplicate_slot_pairs"))):
            return None
        for key in ("represented_fraction", "observed_points_on_paint"):
            if not _number(native_evidence.get(key)) or not .98 <= native_evidence[key] <= 1.:
                return None
        width, height = geometry["width"], geometry["height"]
        horizon, bottom = geometry["horizon_y"], geometry["valid_bottom"]
        if (type(width) is not int or type(height) is not int or width <= 0 or height <= 0
                or type(horizon) is not int or type(bottom) is not int
                or not 0 <= horizon < bottom < height):
            return None
        pixels = np.asarray(probability)
        if (pixels.shape != (height, width) or pixels.dtype.kind not in "biuf"
                or not np.isfinite(pixels).all()):
            return None
        paint = pixels >= .5
        if not isinstance(geometry["lanes"], list):
            return None
        measured = {}
        for lane in geometry["lanes"]:
            slot = lane.get("slot")
            if slot is None:
                continue
            if (type(slot) is not int or slot not in (1, 2, 3, 4) or slot in measured
                    or lane.get("physical", True) is not True):
                return None
            values = _measured_rows(lane, paint, width, height, horizon, bottom)
            if values is None:
                return None
            measured[slot] = values
        if not {2, 3}.issubset(measured):
            return None
        slots = sorted(measured)
        minimum_rows = math.ceil(max(8., .015 * height))
        minimum_separation = max(7., .008 * height)
        pairs = []
        for left, right in zip(slots, slots[1:]):
            ys, li, ri = np.intersect1d(measured[left][0], measured[right][0], return_indices=True)
            if len(ys) < minimum_rows or int(ys[-1] - ys[0]) < minimum_rows - 1:
                return None
            separation = measured[right][1][ri] - measured[left][1][li]
            if not np.isfinite(separation).all() or not np.all(separation > minimum_separation):
                return None
            pairs.append({"slots": [left, right], "shared_source_rows": len(ys),
                          "shared_y": [int(ys[0]), int(ys[-1])],
                          "minimum_separation_px": float(separation.min()),
                          "all_measured_rows_in_order": True})
        near = horizon + .55 * (bottom - horizon)
        tail_count = math.ceil(max(24., .02 * height))
        centre, margin = .5 * width, max(12., .015 * width)
        ego = []
        for slot in (2, 3):
            ys, xs = measured[slot]
            if len(ys) < tail_count or ys[-1] < near or ys[-1] - ys[-tail_count] < tail_count - 1:
                return None
            tail = xs[-tail_count:]
            distance = centre - tail if slot == 2 else tail - centre
            if not np.all(distance > margin):
                return None
            ego.append({"slot": slot, "measured_rows": tail_count,
                        "measured_y": [int(ys[-tail_count]), int(ys[-1])],
                        "minimum_centre_distance_px": float(distance.min()),
                        "correct_side_on_all_rows": True, "reaches_foreground": True})
        return {"eligible": True, "version": VERSION, "verified_slots": slots,
                "adjacent_pairs": pairs, "ego_sides": ego,
                "minimum_shared_rows": minimum_rows, "minimum_pair_separation_px": minimum_separation,
                "foreground_y": near, "geometry_changed": False, "x_ref_changed": False,
                "source_only": True, "requires_fresh_human_audit": True}
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, OverflowError):
        return None
