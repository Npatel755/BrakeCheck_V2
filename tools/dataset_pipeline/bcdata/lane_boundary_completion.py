"""Bounded lane-path interpolation, retaining the original observed paint."""
from copy import deepcopy
import math

import numpy as np

POLICY = "continuous-lane-boundary-between-observations:v2"
FULL_PATH_POLICY = "one-continuous-path-per-supported-boundary:v1"


def complete_lane_paths(geometry):
    """A lane instance is one continuous path throughout its observed extent.

    Association is owned by the source/model converter. Dash spacing and
    polygon-tip tangents must not split that already assigned lane instance.
    Uncertain association/order is flagged for repair, not represented as
    alternating positive/negative training rows inside the same lane.
    """
    if geometry.get("boundary_completion", {}).get("policy") == FULL_PATH_POLICY:
        return deepcopy(geometry)
    result = deepcopy(geometry)
    joins, issues = 0, []
    for lane in result["lanes"]:
        original = deepcopy(lane.get("observed_paint_segments", lane.get("segments", [lane["points"]])))
        rows = {}
        sections = []
        for raw_section in original:
            section = raw_section if isinstance(raw_section, list) else raw_section["points"]
            section = sorted([point[:2] for point in section], key=lambda point: point[1])
            if len(section) < 2:
                raise ValueError("A supported boundary section needs two points")
            sections.append(section)
            for x, y in section:
                if not math.isfinite(x) or not math.isfinite(y) or not 0 <= x < geometry["width"] or not 0 <= y < geometry["height"]:
                    raise ValueError("Boundary observations must stay inside the photo")
                rows.setdefault(y, []).append(x)
        ys = np.asarray(sorted(rows), float)
        xs = np.asarray([rows[y][0] if len(rows[y]) == 1 else np.median(rows[y]) for y in ys], float)
        if len(ys) < 2:
            raise ValueError("A supported boundary needs two distinct image rows")
        if (any(max(values) - min(values) > 8 for values in rows.values())
                and not lane.get('boundary_consolidation', {}).get('same_physical_boundary')):
            issues.append({"slot": lane["slot"], "reason": "conflicting_observations_on_same_row"})
        # Include every actual observation row, every intervening integer row,
        # and both endpoints. No coordinates beyond the observed extent.
        target_ys = np.unique(np.concatenate([ys, np.arange(math.ceil(ys[0]), math.floor(ys[-1]) + 1)]))
        target_xs = np.interp(target_ys, ys, xs)
        points = [[round(float(x), 6), float(y)] for x, y in zip(target_xs, target_ys)]
        spans = sorted((section[0][1], section[-1][1]) for section in sections)
        inferred, reach = [], spans[0][1]
        for first, last in spans[1:]:
            if first > reach + 1:
                bridge = [[round(float(np.interp(y, ys, xs)), 6), float(y)]
                          for y in np.unique(np.concatenate([[reach, first], np.arange(math.floor(reach) + 1, math.ceil(first))]))]
                inferred.append({"points": bridge, "observed_paint": False,
                                 "method": "lane_instance_path_between_observations"})
            reach = max(reach, last)
        lane.update(points=points, segments=[points], observed_paint_segments=original,
                    inferred_sections=inferred, annotation_role="continuous_lane_boundary",
                    physical_evidence="supported_lane_instance_with_interpolated_dash_gaps")
        joins += len(inferred)
    from .college_repair_batch import observed_slot_order_ok
    if not observed_slot_order_ok(result):
        issues.append({"reason": "boundary_order_conflict"})
    result["boundary_completion"] = {"policy": FULL_PATH_POLICY, "interpolated_gaps": joins,
        "blocked_gaps": [], "association_issues": issues, "requires_repair": bool(issues),
        "observed_paint_preserved": True, "extends_beyond_observed_endpoints": False,
        "human_review_created": False,
        "interpretation": "One continuous lane-instance path; interpolated gaps are not painted pixels."}
    result["annotation_policy"] = "one_continuous_lane_path_inside_observed_endpoints"
    return result


def _slope(points):
    ys = np.asarray([p[1] for p in points], float)
    xs = np.asarray([p[0] for p in points], float)
    return float(np.polyfit(ys, xs, 1)[0]) if len(points) >= 2 and np.ptp(ys) >= 2 else None


def complete_boundaries(geometry, occlusion=None):
    """Connect compatible sections inside observed endpoints; never extrapolate.

    A join is a boundary-path target, not a claim that paint exists in its gap.
    Large, inconsistent, crossing or explicitly occluded gaps remain separate.
    """
    if geometry.get("boundary_completion", {}).get("policy") == POLICY:
        return deepcopy(geometry)
    result = deepcopy(geometry)
    height, width = geometry["height"], geometry["width"]
    if occlusion is not None and np.asarray(occlusion).shape != (height, width):
        raise ValueError("Occlusion evidence must match the photo")
    total, blocked = 0, []
    for lane in result["lanes"]:
        original = deepcopy(lane.get("observed_paint_segments", lane.get("segments", [lane["points"]])))
        sections = [sorted([p[:2] for p in (s if isinstance(s, list) else s["points"])], key=lambda p: p[1]) for s in original]
        sections.sort(key=lambda points: points[0][1] if points else -1)
        if not sections or any(len(s) < 2 or any(a[1] >= b[1] for a, b in zip(s, s[1:])) for s in sections):
            raise ValueError("Boundary observations must be single-valued sections")
        joined, inferred, local_blocked = [deepcopy(sections[0])], [], []
        for previous, section in zip(sections, sections[1:]):
            a, b = previous[-1], section[0]
            gap = b[1] - a[1]
            reason = None
            if gap <= 0:
                reason = "overlapping_observed_sections"
            elif gap > .15 * height:
                reason = "gap_too_large"
            elif gap <= max(2., .015 * height) and abs(b[0] - a[0]) <= .025 * width:
                # These sections already share a boundary identity. At a
                # polygon tip, a few rows of centroid jitter make the local
                # tangent unreliable; use bounded endpoint proximity instead.
                pass
            else:
                slope = (b[0] - a[0]) / gap
                before, after = _slope(previous[-12:]), _slope(section[:12])
                tolerance = max(6., .22 * gap)
                if (before is None or after is None or
                        abs((a[0] + before * gap) - b[0]) > tolerance or
                        abs((b[0] - after * gap) - a[0]) > tolerance or
                        abs(before - slope) > 1.2 or abs(after - slope) > 1.2):
                    reason = "gap_direction_ambiguous"
            points = []
            if reason is None:
                for y in range(math.floor(a[1]) + 1, math.ceil(b[1])):
                    x = a[0] + (b[0] - a[0]) * (y - a[1]) / gap
                    if not 0 <= x < width:
                        reason = "outside_photo"
                        break
                    if occlusion is not None and occlusion[int(y), int(round(min(width - 1, max(0, x))))] >= .5:
                        reason = "explicit_occlusion"
                        break
                    points.append([round(x, 6), y])
            if reason:
                local_blocked.append({"slot": lane["slot"], "from": a, "to": b, "reason": reason})
                joined.append(deepcopy(section))
            else:
                joined[-1].extend(points)
                joined[-1].extend(deepcopy(section))
                if gap > 1:
                    inferred.append({"points": [deepcopy(a), *points, deepcopy(b)],
                                     "observed_paint": False, "method": "bounded_linear_endpoint_interpolation"})
        lane.update(points=[p for section in joined for p in section], segments=joined,
                    observed_paint_segments=original, inferred_sections=inferred,
                    annotation_role="continuous_lane_boundary",
                    physical_evidence="observed_paint_plus_bounded_boundary_interpolation")
        total += len(inferred)
        blocked.extend(local_blocked)
    # New interpolated sections must not cross another boundary's observed or
    # interpolated path. Revert the implicated lanes rather than invent order.
    from .college_repair_batch import observed_slot_order_ok
    if not observed_slot_order_ok(result):
        for lane, original_lane in zip(result["lanes"], geometry["lanes"]):
            if lane["inferred_sections"]:
                blocked.append({"slot": lane["slot"], "reason": "boundary_order_conflict"})
                lane.update(points=deepcopy(original_lane["points"]),
                            segments=deepcopy(original_lane.get("segments", [original_lane["points"]])),
                            inferred_sections=[])
        total = 0
    result["boundary_completion"] = {"policy": POLICY, "interpolated_gaps": total,
        "blocked_gaps": blocked, "observed_paint_preserved": True,
        "extends_beyond_observed_endpoints": False, "human_review_created": False,
        "interpretation": "Lane path between supported observations; inferred sections are not painted pixels."}
    result["annotation_policy"] = "continuous_boundary_between_supported_observations_no_endpoint_extension"
    return result
