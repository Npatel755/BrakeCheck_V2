"""Source-only Audi paint reconstruction with measured, piecewise centre lines.

The generic converter fits a polynomial through an entire dashed chain. A
curved solid boundary can consequently bow away from its own source paint.
This converter keeps measured run centres, joins only unique local dash
continuations, and orders recovered boundaries using actual shared rows.
It never consumes model predictions, human labels, or review decisions.
"""
from __future__ import annotations

from copy import deepcopy
import math

import cv2
import numpy as np

from .geometry import (GeoParams, _Geo, _x_at, filter_segments, lane_checks,
                       lane_xs, paint_window, track_segments)

VERSION = "a2d2-piecewise-source-v1"


def _validate(original, probability, masks):
    if probability.ndim != 2 or probability.shape != (original["height"], original["width"]):
        raise ValueError("Audi source mask dimensions do not match geometry")
    if not np.isfinite(probability).all():
        raise ValueError("Audi source mask contains non-finite values")
    for name, mask in masks.items():
        if mask is not None and (np.shape(mask) != probability.shape or not np.isfinite(mask).all()):
            raise ValueError(name + " mask dimensions or finite values do not match source paint")


def _endpoint(segment, top):
    ys, xs = segment.ys.astype(float), segment.xs.astype(float)
    edge = ys.min() if top else ys.max()
    use = np.abs(ys - edge) <= max(8., min(40., .4 * np.ptp(ys)))
    if use.sum() < 5 or np.ptp(ys[use]) < 4:
        return None
    a, b = np.polyfit(ys[use], xs[use], 1)
    return float(a), float(b)


def _paint_type(segment, solid, dashed):
    if solid is None or dashed is None:
        return None
    xs = np.round(segment.xs).astype(int)
    ys = segment.ys.astype(int)
    values = np.array([np.asarray(solid)[ys, xs].sum(), np.asarray(dashed)[ys, xs].sum()], float)
    return int(values.argmax()) if values.max() >= .8 * max(1., values.sum()) else None


def continuation_score(lower, upper, width, height, lower_type=None, upper_type=None):
    """Bidirectional measured tangents and a bounded curved-gap secant."""
    gap = lower.y_top - upper.y_bottom
    if gap < 1 or gap > .18 * height:
        return None
    if lower_type is not None and upper_type is not None and lower_type != upper_type:
        return None
    lt, ut = _endpoint(lower, True), _endpoint(upper, False)
    if lt is None or ut is None:
        return None
    angles = [math.atan(tangent[0]) for tangent in (lt, ut)]
    if abs(angles[0] - angles[1]) > math.radians(24):
        return None
    yl, yu = float(lower.y_top), float(upper.y_bottom)
    xl = float(np.mean(lower.xs[lower.ys == lower.y_top]))
    xu = float(np.mean(upper.xs[upper.ys == upper.y_bottom]))
    error = max(abs(lt[0] * yu + lt[1] - xu), abs(ut[0] * yl + ut[1] - xl))
    tolerance = max(6., .007 * width) + .10 * gap
    if error > tolerance:
        return None
    secant = math.atan((xl - xu) / gap)
    # On a monotonic curve the secant can lie between two different tangents.
    # Requiring agreement with both would discard the observed bend itself.
    if not min(angles) - math.radians(7) <= secant <= max(angles) + math.radians(7):
        return None
    return error / tolerance + .25 * gap / height


def _group(segments, width, height, solid, dashed):
    chains = [[segment] for segment in segments]
    warnings, joins = [], 0
    while True:
        pairs = []
        for i, chain in enumerate(chains):
            lower = min(chain, key=lambda segment: segment.y_top)
            for j, other in enumerate(chains):
                if i == j:
                    continue
                upper = max(other, key=lambda segment: segment.y_bottom)
                if max(segment.y_bottom for segment in other) >= min(segment.y_top for segment in chain):
                    continue
                score = continuation_score(lower, upper, width, height,
                                           _paint_type(lower, solid, dashed), _paint_type(upper, solid, dashed))
                if score is not None:
                    pairs.append((score, i, j))
        if not pairs:
            break
        pairs.sort()
        chosen = None
        for score, i, j in pairs:
            # A fork is ambiguity, not an invitation to select the lowest
            # polynomial residual. Require a unique partner at both endpoints.
            competitors = [other for other in pairs if other[1:] != (i, j)
                           and (other[1] == i or other[2] == j) and other[0] <= score + .18]
            if not competitors:
                chosen = i, j
                break
        if chosen is None:
            warnings.append("ambiguous_source_continuation")
            break
        i, j = chosen
        merged = chains[i] + chains[j]
        chains = [chain for index, chain in enumerate(chains) if index not in (i, j)] + [merged]
        joins += 1
    return chains, joins, warnings


def _from_chain(chain):
    observed = {}
    widths = {}
    for segment in chain:
        for x, y, width in zip(segment.xs, segment.ys, segment.ws):
            observed[int(y)] = float(x)
            widths[int(y)] = float(width)
    ys = sorted(observed)
    xs = [observed[y] for y in ys]
    fit = {"basis": "interp", "ys": ys, "xs": xs}
    points = []
    sample_rows = sorted(set(range(ys[-1], ys[0] - 1, -5)) | {ys[0], ys[-1]}
                         | {segment.y_top for segment in chain} | {segment.y_bottom for segment in chain}, reverse=True)
    for y in sample_rows:
        points.append([round(float(np.interp(y, ys, xs)), 3), int(y), y in observed])
    ordered = sorted(chain, key=lambda segment: segment.y_bottom)
    gaps = [[lower.y_bottom, upper.y_top] for lower, upper in zip(ordered, ordered[1:])
            if upper.y_top - lower.y_bottom > 1]
    return {"fit": fit, "points": points, "y_top": ys[0], "y_bottom": ys[-1],
            "segments": len(chain), "kind": "dashed" if gaps else "solid", "gaps": gaps,
            "fit_rms_px": 0., "physical": True, "flags": [], "mean_confidence": 1.,
            "slot": None, "source_observed_rows": ys, "source_observed_widths": [widths[y] for y in ys]}


def _on_paint(geometry, paint):
    hits = total = 0
    for lane in geometry.get("lanes", []):
        if not lane.get("slot"):
            continue
        for point in lane["points"]:
            if len(point) > 2 and not point[2]:
                continue
            x, y = map(round, point[:2])
            if 0 <= y < paint.shape[0] and 0 <= x < paint.shape[1]:
                radius = paint_window(lane, y, hy=geometry["horizon_y"], by=geometry["valid_bottom"], base=4)
                hits += bool(paint[y, max(0, x - radius):x + radius + 1].any())
                total += 1
    return hits / total if total else 1.


def _retention(old, candidates, geometry, paint):
    retained = total = 0
    for point in old["points"]:
        if len(point) > 2 and not point[2]:
            continue
        x, y = map(float, point[:2])
        xi, yi = round(x), round(y)
        if not (0 <= yi < paint.shape[0] and 0 <= xi < paint.shape[1]):
            continue
        radius = paint_window(old, yi, hy=geometry["horizon_y"], by=geometry["valid_bottom"], base=4)
        if not paint[yi, max(0, xi-radius):xi+radius+1].any():
            continue
        total += 1
        for candidate in candidates:
            if not candidate["y_top"] <= y <= candidate["y_bottom"]:
                continue
            width = float(np.interp(y, candidate["source_observed_rows"], candidate["source_observed_widths"]))
            if abs(float(lane_xs(candidate, y)) - x) <= max(8., .006 * geometry["width"], .5 * width + 4):
                retained += 1
                break
    return retained / total if total else 1.


def _preserve_supported_boundaries(candidates, matches, original, paint):
    """A repair to one boundary cannot truncate another already valid one."""
    preserved = []
    for index, old_lanes in matches.items():
        candidate = candidates[index]
        for old in old_lanes:
            retained = _retention(old, [candidate], original, paint) >= .95
            # Broad semantic strokes can accommodate several centre fits.
            # Recentring an already supported trace must not move substantial
            # source-supported portions to a different physical trajectory.
            # Use only the old source conversion here, never human geometry.
            stable_identity = True
            if _on_paint({**original, "lanes": [old]}, paint) >= .75:
                matched, total = 0, 0
                for y in range(max(0, old["y_top"]), min(paint.shape[0]-1, old["y_bottom"])+1):
                    x = float(lane_xs(old, y))
                    xi = round(x)
                    if not (0 <= xi < paint.shape[1]) or not paint[y, max(0,xi-12):min(paint.shape[1],xi+13)].any():
                        continue
                    total += 1
                    matched += bool(candidate["y_top"] <= y <= candidate["y_bottom"]
                                    and abs(float(lane_xs(candidate, y))-x) <= 12)
                stable_identity = total-matched < 5 or matched/max(1,total) >= .98
            if retained and stable_identity:
                continue
            # Keeping the original is not certifying it: existing off-mask QC
            # remains active. It prevents a successful repair elsewhere from
            # deleting an unresolved source-supported part of this boundary.
            if len(old_lanes) != 1:
                return False, preserved
            keep = deepcopy(old)
            observed_rows, widths = [], []
            for y in range(max(0, old["y_top"]), min(paint.shape[0]-1, old["y_bottom"])+1):
                x = round(float(lane_xs(old, y)))
                radius = paint_window(old, y, hy=original["horizon_y"], by=original["valid_bottom"], base=4)
                if 0 <= x < paint.shape[1] and paint[y, max(0, x-radius):x+radius+1].any():
                    observed_rows.append(y)
                    widths.append(max(3, 2*radius))
            if len(observed_rows) < 8:
                return False, preserved
            keep.update(source_observed_rows=observed_rows, source_observed_widths=widths,
                        preserved_source_boundary=True, slot=old["slot"])
            candidate.clear()
            candidate.update(keep)
            preserved.append(old["slot"])
    # Restoring a complete old chain can also restore the far fragment that
    # looked vacant during raw-track slot assignment. That same paint cannot
    # acquire a second physical lane identity as a newly recovered boundary.
    for index, candidate in enumerate(candidates):
        if index not in matches and candidate.get("slot") and _represented_by_preserved(candidate, candidates, original):
            candidate["slot"] = None
    slots = [candidate["slot"] for candidate in candidates if candidate.get("slot")]
    if len(slots) != len(set(slots)):
        return False, preserved
    return True, preserved


def _represented_by_preserved(candidate, candidates, original):
    rows = np.asarray(candidate["source_observed_rows"], float)
    for anchor in candidates:
        if not anchor.get("preserved_source_boundary"):
            continue
        # Source polygons include the rounded paint-cap pixels just beyond a
        # stored centre-line endpoint. This allowance is bounded by measured
        # stroke width, and never extends the retained exported boundary.
        cap = max(4., .5 * max(candidate["source_observed_widths"]) + 4)
        if rows.min() < anchor["y_top"] - cap or rows.max() > anchor["y_bottom"] + cap:
            continue
        radii = np.array([paint_window(anchor, y, hy=original["horizon_y"], by=original["valid_bottom"], base=6) for y in rows])
        clipped_rows = np.clip(rows, anchor["y_top"], anchor["y_bottom"])
        if (np.abs(lane_xs(candidate, rows)-lane_xs(anchor, clipped_rows)) <= radii).mean() >= .95:
            candidate["flags"].append("supporting_source_fragment")
            candidate["represented_by_existing_slot"] = anchor["slot"]
            return True
    return False


def _centre_coverage(geometry, segments):
    hits = total = 0
    old = [lane for lane in geometry.get("lanes", []) if lane.get("slot")]
    for segment in segments:
        if np.ptp(segment.ys) < max(8, .015 * geometry["height"]):
            continue
        for x, y in zip(segment.xs[::3], segment.ys[::3]):
            total += 1
            hits += any(lane["y_top"] <= y <= lane["y_bottom"]
                        and abs(float(lane_xs(lane, y)) - x) <= paint_window(
                            lane, y, hy=geometry["horizon_y"], by=geometry["valid_bottom"], base=6)
                        for lane in old)
    return hits / total if total else 1.


def _disjoint(geometry):
    slots = {lane["slot"]: lane for lane in geometry.get("lanes", []) if lane.get("slot")}
    return any(first in slots and second in slots
               and max(slots[first]["y_top"], slots[second]["y_top"]) - min(slots[first]["y_bottom"], slots[second]["y_bottom"]) > 5
               for first, second in ((1, 2), (3, 4)))


def _branched_source(paint, horizon):
    mask = paint.copy()
    mask[:int(horizon)] = False
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    for label in range(1, count):
        x, y, width, height, area = map(int, stats[label])
        if height < .03 * paint.shape[0] or area < 100:
            continue
        component = labels[y:y + height, x:x + width] == label
        padded = np.pad(component.astype(np.int8), ((0, 0), (1, 1)))
        runs = (np.diff(padded, axis=1) == 1).sum(axis=1)
        if (runs > 1).sum() >= max(10, .015 * paint.shape[0]):
            return True
    return False


def _shape_checks(candidates, g):
    # Source run centres contain sub-pixel quantization. Generic reversal
    # checks must not turn this jitter into an implausible physical S-curve.
    # Simplification is ONLY for flags; the exported measured fit is unchanged.
    checked = deepcopy(candidates)
    for lane in checked:
        points = np.ascontiguousarray(np.asarray(lane["points"], np.float32)[:, :2])
        simple = cv2.approxPolyDP(points.reshape(-1, 1, 2), max(1.5, .001 * g.W), False).reshape(-1, 2)
        simple = simple[np.argsort(simple[:, 1])]
        lane["fit"] = {"basis": "interp", "ys": simple[:, 1].tolist(), "xs": simple[:, 0].tolist()}
        lane["flags"] = []
    tags = lane_checks(checked, g)
    for actual, check in zip(candidates, checked):
        if "implausible_curve" in check["flags"]:
            ys = np.linspace(actual["y_top"], actual["y_bottom"], max(3, actual["y_bottom"] - actual["y_top"] + 1))
            xs = lane_xs(actual, ys)
            baseline = np.polyval(np.polyfit(ys, xs, 1), ys)
            deviation = float(np.abs(xs - baseline).max()) / max(1., np.ptp(ys))
            reversals = meaningful_reversals(xs, ys, g.W, g.H)
            actual["source_shape_checks"] = {"meaningful_reversals": reversals, "deviation_per_row": deviation}
            if np.isfinite(xs).all() and deviation <= 1. and reversals <= 1:
                check["flags"].remove("implausible_curve")
        actual["flags"] = sorted(set(actual.get("flags", [])) | set(check["flags"]))
        actual["curvature"] = check.get("curvature", 0.)
    return tags


def meaningful_reversals(xs, ys, width, height):
    """Count physical turns with hysteresis, excluding pixel-scale edge jitter."""
    amplitude, duration = max(6., .008 * width), max(6., .008 * height)
    direction, extreme_x, extreme_y, reversals = 0, float(xs[0]), float(ys[0]), 0
    for x, y in zip(xs[1:], ys[1:]):
        delta = float(x) - extreme_x
        if direction == 0:
            if abs(delta) >= amplitude:
                direction = 1 if delta > 0 else -1
                extreme_x, extreme_y = float(x), float(y)
        elif delta * direction >= 0:
            extreme_x, extreme_y = float(x), float(y)
        elif abs(delta) >= amplitude and abs(float(y) - extreme_y) >= duration:
            reversals += 1
            direction *= -1
            extreme_x, extreme_y = float(x), float(y)
    return reversals


def _match(old, candidate, width, height):
    ys = np.asarray(candidate["source_observed_rows"], float)
    use = (ys >= old["y_top"]) & (ys <= old["y_bottom"])
    if use.sum() < max(8, int(.012 * height)):
        return None
    ys = ys[use]
    xs = lane_xs(candidate, ys)
    source_widths = np.asarray(candidate["source_observed_widths"])[use]
    distance = np.abs(xs - lane_xs(old, ys))
    tolerance = np.maximum(max(5., .004 * width), .5 * source_widths)
    fraction = float((distance <= tolerance).mean())
    if fraction < .55:
        # A bowed polynomial can miss the middle of one continuous curved
        # source stripe. A real, measured endpoint anchor still identifies
        # that stripe; a remote parallel neighbour cannot supply this anchor.
        anchor_rows = ys >= ys.max() - max(12., .025 * height)
        if anchor_rows.sum() < max(8, .012 * height) or (distance[anchor_rows] <= tolerance[anchor_rows]).mean() < .8:
            return None
    return fraction * math.sqrt(np.ptp(ys) / max(1, old["y_bottom"] - old["y_top"]))


def _relative(candidate, anchor, height):
    rows = np.intersect1d(candidate["source_observed_rows"], anchor["source_observed_rows"])
    if len(rows) < max(8, .015 * height):
        return None
    delta = lane_xs(candidate, rows) - lane_xs(anchor, rows)
    minimum = max(7., .008 * height)
    if np.quantile(delta, .05) > minimum:
        return 1
    if np.quantile(delta, .95) < -minimum:
        return -1
    return None


def _outside_four(candidate, candidates, width):
    """Record an outboard chain only from two coexisting measured neighbours."""
    by_slot = {lane["slot"]: lane for lane in candidates if lane.get("slot")}
    observed = candidate["source_observed_rows"]
    for outer, inner, side in ((1, 2, -1), (4, 3, 1)):
        if outer not in by_slot or inner not in by_slot:
            continue
        rows = np.intersect1d(observed, by_slot[outer]["source_observed_rows"])
        rows = np.intersect1d(rows, by_slot[inner]["source_observed_rows"])
        if len(rows) < 12 or len(rows) < .85 * len(observed):
            continue
        cx, ox, ix = [lane_xs(lane, rows) for lane in (candidate, by_slot[outer], by_slot[inner])]
        minimum = max(12., .02 * width)
        if np.all((cx - ox) * side > minimum) and np.all((ox - ix) * side > minimum):
            candidate["flags"].append("beyond_four_lanes")
            candidate["beyond_four_lanes_evidence"] = {"outer_slot": outer, "inner_slot": inner,
                                                       "shared_observed_rows": int(len(rows)),
                                                       "component_observed_rows": len(observed),
                                                       "shared_fraction": len(rows) / len(observed),
                                                       "minimum_separation_px": minimum,
                                                       "ordering_from_actual_paint": True}
            return True
    return False


def _slots(candidates, original):
    height, width = original["height"], original["width"]
    old = [lane for lane in original.get("lanes", []) if lane.get("slot")]
    matches, unresolved = {}, []
    for lane in old:
        scores = sorted((score, index) for index, candidate in enumerate(candidates)
                        if (score := _match(lane, candidate, width, height)) is not None)
        if not scores:
            unresolved.append(lane["slot"])
            continue
        score, index = scores[-1]
        if len(scores) > 1 and scores[-2][0] >= .8 * score:
            other = candidates[scores[-2][1]]
            if min(other["y_bottom"], candidates[index]["y_bottom"]) - max(other["y_top"], candidates[index]["y_top"]) > .02 * height:
                unresolved.append(lane["slot"])
                continue
        matches.setdefault(index, []).append(lane)
    if unresolved:
        return False, ["unresolved_existing_source_boundary"], matches
    near = original["horizon_y"] + .55 * (original["valid_bottom"] - original["horizon_y"])
    anchors = [candidate for index, candidate in enumerate(candidates)
               if index in matches and candidate["y_bottom"] >= near]
    # Actual foreground source location anchors lane side. Far dashes cannot
    # steal an ego slot using extrapolation from a curved/tiny upper fragment.
    for candidate in candidates:
        candidate["x_ref"] = float(lane_xs(candidate, candidate["y_bottom"]))
    if len(anchors) > 1:
        common = np.asarray(anchors[0]["source_observed_rows"])
        for anchor in anchors[1:]:
            common = np.intersect1d(common, anchor["source_observed_rows"])
        if len(common) < 8 or common.max() < near:
            return False, ["uncertain_shared_foreground_slot_order"], matches
        reference_y = float(common.max())
        for anchor in anchors:
            anchor["x_ref"] = float(lane_xs(anchor, reference_y))
    for side, order in ((-1, (2, 1)), (1, (3, 4))):
        ordered = sorted((candidate for candidate in anchors if (candidate["x_ref"] - .5 * width) * side >= 0),
                         key=lambda candidate: abs(candidate["x_ref"] - .5 * width))
        for slot, candidate in zip(order, ordered):
            candidate["slot"] = slot
    warnings = []
    for index, lanes in matches.items():
        candidate = candidates[index]
        if candidate["slot"]:
            continue
        slots = {lane["slot"] for lane in lanes}
        if slots <= {1, 2}:
            candidate["slot"] = max(slots)
        elif slots <= {3, 4}:
            candidate["slot"] = min(slots)
        else:
            warnings.append("uncertain_source_slot_identity")
    occupied = {candidate["slot"] for candidate in candidates if candidate["slot"]}
    if len(occupied) != sum(bool(candidate["slot"]) for candidate in candidates):
        return False, ["ambiguous_source_slot_identity"], matches
    for candidate in sorted(candidates, key=lambda lane: -lane["y_bottom"]):
        if candidate["slot"]:
            continue
        possible = set(range(1, 5)) - occupied
        constraints = 0
        for anchor in candidates:
            if not anchor["slot"]:
                continue
            relation = _relative(candidate, anchor, height)
            if relation:
                constraints += 1
                possible &= ({slot for slot in range(1, anchor["slot"])} if relation < 0
                             else {slot for slot in range(anchor["slot"] + 1, 5)})
        if constraints and len(possible) == 1:
            candidate["slot"] = possible.pop()
            occupied.add(candidate["slot"])
    return True, warnings, matches


def repair_geometry(original, lane_prob, my_car=None, movable=None, *, params=None,
                    solid_mask=None, dashed_mask=None):
    """Return an auditable new draft; immutable source and input geometry stay intact."""
    probability = np.asarray(lane_prob, np.float32)
    _validate(original, probability, {"ego-car": my_car, "movable": movable,
                                     "solid": solid_mask, "dashed": dashed_mask})
    paint = probability >= .5
    for mask in (my_car, movable):
        if mask is not None:
            paint &= np.asarray(mask) < .5
    height, width = paint.shape
    previous = deepcopy(original.get("source_converter", {}))
    input_warnings = list(previous.get("warnings", []))

    def unchanged(warnings=(), reason="unchanged"):
        result = deepcopy(original)
        result["source_converter"] = {"version": VERSION, "risk_family": reason,
                                      "requires_fresh_human_audit": True, "changed": False,
                                      "input_warnings": input_warnings,
                                      "warnings": sorted(set(input_warnings + list(warnings))),
                                      "previous_converter": previous}
        return result

    if not paint.any():
        return unchanged()
    params = deepcopy(params) if params is not None else GeoParams()
    params.horizon_y = original["horizon_y"] / height
    params.max_run_frac = original.get("max_run_frac", params.max_run_frac)
    visible_rows = np.nonzero(paint)[0]
    bottom = max(original["valid_bottom"], int(visible_rows.max()))
    g = _Geo((height, width), params, min(height - 1, bottom))
    tracks, track_stats = track_segments(paint, g)
    segments, filter_stats = filter_segments(tracks, g)
    # Tiny and near-horizontal components supply no reliable lane direction.
    segments = [segment for segment in segments if np.ptp(segment.ys) >= max(8, .01 * height)
                and (segment.slope() is None or abs(segment.slope()) <= 4.)]
    if len(segments) > 160:
        return unchanged(["complex_source_topology"], "topology_uncertain")
    before_coverage, before_support = _centre_coverage(original, segments), _on_paint(original, paint)
    if before_coverage >= .90 and before_support >= .90 and not _disjoint(original) and not input_warnings:
        return unchanged()
    chains, joins, warnings = _group(segments, width, height, solid_mask, dashed_mask)
    candidates = [_from_chain(chain) for chain in chains]
    okay, slot_warnings, matches = _slots(candidates, original)
    if not okay:
        return unchanged(slot_warnings, "topology_uncertain")
    retained, preserved_slots = _preserve_supported_boundaries(candidates, matches, original, paint)
    if not retained:
        return unchanged(["source_boundary_retention_uncertain"], "topology_uncertain")
    warnings.extend(slot_warnings)
    slotted = [candidate for candidate in candidates if candidate["slot"]]
    if not slotted:
        return unchanged(["unassigned_source_fragment_requires_review"], "topology_uncertain")
    for candidate in candidates:
        if candidate["slot"] is None and candidate["y_bottom"] - candidate["y_top"] >= .025 * height:
            if not _represented_by_preserved(candidate, candidates, original) and not _outside_four(candidate, candidates, width):
                candidate["flags"].append("fragment")
                warnings.append("unassigned_source_fragment_requires_review")
    result = deepcopy(original)
    candidates.sort(key=lambda lane: (lane["slot"] is None, lane["slot"] or 0, lane["x_ref"]))
    for index, candidate in enumerate(candidates):
        candidate["id"] = index
    result.update(lanes=candidates, valid_bottom=g.by)
    after_coverage, after_support = _centre_coverage(result, segments), _on_paint(result, paint)
    if after_coverage + .005 < before_coverage or after_support + .005 < before_support:
        return unchanged(["source_repair_regression_rejected"], "topology_uncertain")
    if after_coverage < before_coverage + .015 and after_support < before_support + .05 and not _disjoint(original):
        return unchanged(warnings)
    tags = _shape_checks(candidates, g)
    if any(set(candidate.get("flags", [])) & {"crossing", "invalid_coordinates", "implausible_curve"} for candidate in slotted):
        return unchanged(["source_repair_topology_uncertain"], "topology_uncertain")
    ordered_reference = [candidate["x_ref"] for candidate in sorted(slotted, key=lambda candidate: candidate["slot"])]
    if ordered_reference != sorted(ordered_reference):
        return unchanged(["uncertain_source_slot_order"], "topology_uncertain")
    if _branched_source(paint, original["horizon_y"]):
        warnings.append("branched_source_paint_requires_review")
    # Original warning text remains evidence. Clear only the generic missing-
    # component warning if every retained source chain is now represented.
    complete = all(candidate["slot"] or set(candidate["flags"]) & {"beyond_four_lanes", "supporting_source_fragment"}
                   for candidate in candidates)
    resolved = {"unassigned_source_fragment_requires_review"} if complete else set()
    verified_resolution = {}
    if complete and not _disjoint(result) and not warnings and after_support >= .98:
        mechanisms = {"ambiguous_inward_source_continuation", "inward_or_duplicate_fragment",
                      "near_duplicate_slot_identity", "ambiguous_source_continuation",
                      "uncertain_source_curve_continuation", "uncertain_steep_source_boundary",
                      "crossing_source_boundaries"}
        resolved |= mechanisms & set(input_warnings)
        for warning in resolved & set(input_warnings):
            verified_resolution[warning] = {"all_measured_chains_assigned": True,
                                            "unique_local_continuations": True,
                                            "observed_support": after_support,
                                            "disjoint_slots": False, "crossings": False}
    warnings.extend(warning for warning in input_warnings if warning not in resolved)
    result["tags"] = {**original.get("tags", {}), **tags, "lane_count": len(slotted)}
    result["stats"] = {**original.get("stats", {}), **track_stats, **filter_stats,
                       "tracks": len(tracks), "segments": len(segments), "chains": len(chains)}
    result["source_converter"] = {"version": VERSION, "risk_family": "piecewise_source_reconstruction",
                                  "requires_fresh_human_audit": True, "changed": True,
                                  "joined_chains": joins, "input_warnings": input_warnings,
                                  "preserved_existing_slots": preserved_slots,
                                  "resolved_warnings": sorted(resolved & set(input_warnings)),
                                  "verified_warning_resolution": verified_resolution,
                                  "warnings": sorted(set(warnings)), "previous_converter": previous,
                                  "source_centre_coverage_before": before_coverage,
                                  "source_centre_coverage_after": after_coverage,
                                  "observed_point_support_before": before_support,
                                  "observed_point_support_after": after_support,
                                  "source_only": True, "piecewise_observed_centres": True}
    return result


repair_geometry_a2d2 = repair_geometry
