"""Conservative holds on optional training additions; never edits any labels.

The check compares observed fragments. Endpoint extrapolation is a comparison
tool only and never supplies new paint coordinates or changes lane slots.
"""
from __future__ import annotations

import math
from statistics import median

VERSION = "observed-fragment-slot-hold-v1"


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _tangent(points):
    # Median pairwise slopes reduce sensitivity to a single endpoint or a
    # rounded coordinate. Inputs are normalized to source width and height.
    return median((b[0] - a[0]) / (b[1] - a[1])
                  for index, a in enumerate(points) for b in points[index + 1:])


def training_geometry_hold(geometry, width, height):
    """Return eligibility/reasons/evidence for optional copied paint labels.

    This is an ambiguity hold, not proof that fragments belong together.
    It checks only same-side slots 1/2 and 3/4 with almost disjoint observed
    spans. Existing confidence, source split, commercial license, provenance,
    immutable-snapshot and sampled-paint requirements remain separate.
    """
    def result(reasons=(), pairs=()):
        return {"eligible_for_training": not reasons, "reasons": list(reasons),
                "fragment_pairs": list(pairs), "version": VERSION}

    if (not _finite(width) or not _finite(height) or width <= 0 or height <= 0
            or int(width) != width or int(height) != height
            or not isinstance(geometry, dict) or not isinstance(geometry.get("lanes"), list)):
        return result(["invalid_painted_geometry"])
    for key, expected in (("width", width), ("height", height)):
        if key in geometry and (not _finite(geometry[key]) or geometry[key] != expected):
            return result(["invalid_painted_geometry"])
    if not geometry["lanes"]:
        return result(["no_observed_painted_lines"])
    lanes = {}
    for lane in geometry["lanes"]:
        if not isinstance(lane, dict):
            return result(["invalid_painted_geometry"])
        slot = lane.get("slot")
        kind = lane.get("kind")
        if (isinstance(slot, bool) or not isinstance(slot, int) or slot not in (1, 2, 3, 4)
                or slot in lanes or lane.get("physical", True) is not True
                or kind is not None and (not isinstance(kind, str) or kind in {"road_edge", "curb", "pavement_edge"})):
            return result(["invalid_painted_geometry"])
        raw = lane.get("points")
        if not isinstance(raw, list) or len(raw) < 2:
            return result(["invalid_painted_geometry"])
        points, seen_rows = [], set()
        for point in raw:
            if (not isinstance(point, (list, tuple)) or len(point) < 2
                    or not _finite(point[0]) or not _finite(point[1])):
                return result(["invalid_painted_geometry"])
            x, y = point[:2]
            if not 0 <= x < width or not 0 <= y < height or y in seen_rows:
                return result(["invalid_painted_geometry"])
            seen_rows.add(y)
            # A third visibility bit is supplied by mask extraction. Only
            # observed anchors can establish extent or endpoint direction.
            if len(point) >= 3:
                if not isinstance(point[2], bool):
                    return result(["invalid_painted_geometry"])
                if not point[2]:
                    continue
            points.append((x / width, y / height))
        if len(points) < 2:
            return result(["no_observed_painted_lines"])
        points.sort(key=lambda point: point[1])
        lanes[slot] = points
    pairs = []
    for slots in ((1, 2), (3, 4)):
        if not all(slot in lanes for slot in slots):
            continue
        first, second = lanes[slots[0]], lanes[slots[1]]
        if min(first[-1][1] - first[0][1], second[-1][1] - second[0][1]) < .08:
            continue
        first_mid = (first[0][1] + first[-1][1]) / 2
        second_mid = (second[0][1] + second[-1][1]) / 2
        upper_slot, lower_slot = slots if first_mid < second_mid else slots[::-1]
        upper, lower = lanes[upper_slot], lanes[lower_slot]
        signed_gap = lower[0][1] - upper[-1][1]
        if not -.01 <= signed_gap <= .25:
            continue
        upper_end, lower_end = upper[-min(8, len(upper)):], lower[:min(8, len(lower))]
        # One nearly horizontal endpoint row cannot establish a reliable
        # tangent. Require a local observed vertical baseline of at least 1% H.
        if min(upper_end[-1][1] - upper_end[0][1], lower_end[-1][1] - lower_end[0][1]) < .01:
            continue
        up_slope, low_slope = _tangent(upper_end), _tangent(lower_end)
        difference = abs(up_slope - low_slope)
        if not _finite(difference) or difference > .2:
            continue
        forward = abs(upper[-1][0] + up_slope * signed_gap - lower[0][0])
        backward = abs(lower[0][0] - low_slope * signed_gap - upper[-1][0])
        if not all(_finite(value) and value <= .01 for value in (forward, backward)):
            continue
        pairs.append({"slots": [upper_slot, lower_slot],
                      "signed_gap_fraction_height": round(signed_gap, 8),
                      "forward_error_fraction_width": round(forward, 8),
                      "backward_error_fraction_width": round(backward, 8),
                      "normalized_slope_difference": round(difference, 8)})
    return result(["probable_same_boundary_multiple_slots"] if pairs else [], pairs)
