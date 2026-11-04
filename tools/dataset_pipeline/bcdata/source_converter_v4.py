"""Conservative source-paint association repairs, still subject to human audit.

Existing observed chains are retained unless an independently supported dash
continuation, an inward fragment, or a duplicate-boundary association supplies
specific evidence for changing them. Neither human geometry nor model output is
an input. Unresolved crossings and uncertain curves remain explicit QC issues.
"""
from copy import deepcopy
import math

import numpy as np

from .geometry import GeoParams, _Geo, _x_at, lane_checks, lane_xs, paint_window
from .source_converter import merge_chains
from .source_converter_v3 import repair_geometry_v3, risk_diagnostics as v3_risk


VERSION = "source-boundaries-v4"


def _observed(lane, paint=None, geo=None):
    points = []
    for point in lane.get("points", []):
        if len(point) > 2 and not point[2]:
            continue
        x, y = map(float, point[:2])
        if not np.isfinite([x, y]).all():
            continue
        if paint is not None:
            xi, yi = round(x), round(y)
            if not (0 <= yi < paint.shape[0] and 0 <= xi < paint.shape[1]):
                continue
            radius = max(4, paint_window(lane, yi, hy=geo["horizon_y"], by=geo["valid_bottom"]))
            if not paint[yi, max(0, xi-radius):xi+radius+1].any():
                continue
        points.append([x, y])
    return np.asarray(points, float).reshape(-1, 2)


def _tangent(lane, at_top, paint, geo):
    points = _observed(lane, paint, geo)
    if len(points) < 2:
        return None
    edge = points[:, 1].min() if at_top else points[:, 1].max()
    use = points[np.abs(points[:, 1]-edge) <= max(12., min(45., .4*np.ptp(points[:, 1])))]
    if len(use) < 2 or np.ptp(use[:, 1]) < 4:
        return None
    a, b = np.polyfit(use[:, 1], use[:, 0], 1)
    return float(a), float(b)


def _supported(lane, paint, geo):
    raw = _observed(lane)
    actual = _observed(lane, paint, geo)
    # Stored five-row samples near dash/occlusion endpoints can be marked seen
    # although the exact row has no paint. They cannot supply endpoint tangents.
    return len(actual) >= 2 and len(actual)/max(1, len(raw)) >= .75


def continuation_score(a, b, paint, geo):
    """Use both measured tangents; a smooth joint polynomial is insufficient."""
    lower, upper = (a, b) if a["y_bottom"] > b["y_bottom"] else (b, a)
    gap = lower["y_top"]-upper["y_bottom"]
    if gap < 2 or gap > .18*geo["height"]:
        return None
    if not _supported(lower, paint, geo) or not _supported(upper, paint, geo):
        return None
    lt, ut = _tangent(lower, True, paint, geo), _tangent(upper, False, paint, geo)
    if lt is None or ut is None or max(abs(lt[0]), abs(ut[0])) > 5.5:
        return None
    angle = abs(math.atan(lt[0])-math.atan(ut[0]))
    if angle > math.radians(10):
        return None
    yl, yu = float(lower["y_top"]), float(upper["y_bottom"])
    xl, xu = _x_at(lower, yl), _x_at(upper, yu)
    error = max(abs(lt[0]*yu+lt[1]-xu), abs(ut[0]*yl+ut[1]-xl))
    tolerance = max(7., .007*geo["width"])+.14*gap
    if error > tolerance:
        return None
    secant = math.atan((xl-xu)/(yl-yu))
    # Across a seven-row mask break, a few pixels of run-centre quantization
    # change the secant substantially. Two agreeing measured tangents and a
    # <=5px endpoint residual still identify the same painted boundary.
    near = gap <= .02*geo["height"] and error <= 5 and angle <= math.radians(6)
    if not near and max(abs(secant-math.atan(t[0])) for t in (lt, ut)) > math.radians(10):
        return None
    slots = {s for lane in (a, b) for s in lane.get("repair_original_slots", [lane.get("slot")]) if s}
    if slots & {1, 2} and slots & {3, 4}:
        return None
    # This repair addresses ego-boundary under-coverage and duplicated lane
    # identities. Extending an accepted outer trace into an otherwise unassigned
    # fragment is a separate policy choice, even if the source paint is nearby.
    if slots and not slots & {2, 3}:
        return None
    if a.get("slot") and b.get("slot") and abs(a["x_ref"]-b["x_ref"]) > .06*geo["width"]:
        return None
    return error/tolerance + gap/geo["height"]


def risk_diagnostics(original):
    lanes = [lane for lane in original.get("lanes", []) if lane.get("slot")]
    close = []
    for i, a in enumerate(lanes):
        for b in lanes[i+1:]:
            if (a["slot"] <= 2) != (b["slot"] <= 2):
                continue
            if abs(a.get("x_ref", 0)-b.get("x_ref", 0)) < .05*original["width"]:
                close.append([a["slot"], b["slot"]])
    curved = v3_risk(original)["curved_long_gaps"]
    return {"close_slot_pairs": close, "curved_long_gaps": curved,
            "reassociate": bool(close and curved)}


def _crossing(lanes, geo):
    assigned = sorted((lane for lane in lanes if lane.get("slot")), key=lambda lane: lane["slot"])
    for index, a in enumerate(assigned):
        for b in assigned[index+1:]:
            lo = max(a["y_top"], b["y_top"], geo["horizon_y"]+.08*geo["height"])
            hi = min(a["y_bottom"], b["y_bottom"])
            if hi-lo < 10:
                continue
            ys = np.arange(lo, hi+1, 2)
            reversed_order = lane_xs(a, ys)-lane_xs(b, ys) > max(8., .012*geo["width"])
            if reversed_order.sum() >= 5:
                return True
    return False


def _recoverable(lane, paint, geo):
    points = _observed(lane, paint, geo)
    if len(points) < 3 or np.ptp(points[:, 1]) < max(12., .015*geo["height"]):
        return False
    if not _supported(lane, paint, geo):
        return False
    t = _tangent(lane, False, paint, geo)
    if t is None or abs(t[0]) > 5.5:
        return False
    vp_x = t[0]*geo["horizon_y"]+t[1]
    return .2*geo["width"] <= vp_x <= .8*geo["width"]


def _distinct_observed_boundary(candidate, existing, paint, geo, side):
    """Extrapolated ordering cannot distinguish a curved dash continuation.

    A missing inner boundary must coexist with the existing outer boundary on
    actual painted rows. Non-overlapping curve/occlusion/intersection fragments
    remain association uncertainty, even if their extrapolated x_ref differs.
    """
    lo = max(candidate["y_top"], existing["y_top"])
    hi = min(candidate["y_bottom"], existing["y_bottom"])
    if hi-lo < 6:
        return False
    supported_separations = []
    for y in range(int(np.ceil(lo)), int(np.floor(hi))+1):
        xs = [float(lane_xs(lane,y)) for lane in (candidate,existing)]
        actual = []
        for lane,x in zip((candidate,existing),xs):
            xi = round(x)
            radius = max(4,paint_window(lane,y,hy=geo["horizon_y"],by=geo["valid_bottom"]))
            actual.append(0 <= xi < paint.shape[1] and bool(paint[y,max(0,xi-radius):xi+radius+1].any()))
        if all(actual):
            supported_separations.append((xs[1]-xs[0])*side)
    return (len(supported_separations) >= max(5,int(.006*geo["height"]))
            and min(supported_separations) > max(16.,.018*geo["width"]))


def repair_geometry_v4(original, lane_prob, my_car=None, movable=None):
    """Return a fresh draft; never mutate inputs or interpret AI checks as approval."""
    probability = np.asarray(lane_prob, np.float32)
    if probability.shape != (original["height"], original["width"]):
        raise ValueError("Source mask dimensions do not match geometry")
    if not np.isfinite(probability).all():
        raise ValueError("Source paint contains non-finite values")
    paint = probability >= .5
    for name, mask in (("ego-car", my_car), ("movable", movable)):
        if mask is not None and np.shape(mask) != paint.shape:
            raise ValueError(name + " mask dimensions do not match source paint")
    if my_car is not None:
        paint &= np.asarray(my_car) < .5
    risks = risk_diagnostics(original)
    # Re-extraction is restricted to duplicated slot identities with an existing
    # unsupported long-gap curve; normal existing source chains stay intact.
    geo = (repair_geometry_v3(original, probability, my_car, movable)
           if risks["reassociate"] else deepcopy(original))
    lanes = deepcopy(geo["lanes"])
    for lane in lanes:
        lane["repair_original_slots"] = [lane["slot"]] if lane.get("slot") else []
    input_warnings = list(original.get("source_converter", {}).get("warnings", []))
    warnings, joins, recovered = [], 0, 0
    ambiguous = False
    while True:
        pairs = sorted((score, i, j) for i, a in enumerate(lanes)
                       for j, b in enumerate(lanes[:i])
                       if (score := continuation_score(a, b, paint, geo)) is not None)
        if not pairs:
            break
        score, i, j = pairs[0]
        # Two similarly plausible continuations represent association ambiguity,
        # not permission to choose the one closest to a human drawing.
        competitors = [p for p in pairs[1:] if set(p[1:]) & {i, j} and p[0] < score+.12]
        if competitors:
            ambiguous = True
            break
        merged = merge_chains(lanes[i], lanes[j])
        old_slots = merged["repair_original_slots"]
        merged["slot"] = (max(old_slots) if max(old_slots) <= 2 else min(old_slots)) if old_slots else None
        merged["x_ref"] = _x_at(merged, geo["valid_bottom"], allow_extrapolate=True)
        # Keep exact source-supported visibility; connecting dashed endpoints
        # never turns an unpainted interval into newly observed paint.
        for point in merged["points"]:
            if len(point) == 2:
                point.append(True)
            point[2] = bool(len(_observed({"points": [point]}, paint, geo)))
        lanes = [lane for k, lane in enumerate(lanes) if k not in (i, j)] + [merged]
        joins += 1
    if ambiguous:
        warnings.append("ambiguous_source_continuation")
    slots = {lane["slot"]: lane for lane in lanes if lane.get("slot")}
    center = .5*geo["width"]
    for side, inner, outer in ((-1, 2, 1), (1, 3, 4)):
        old_inner = slots.get(inner)
        # A new isolated marking on a previously empty side may be an arrow,
        # shoulder marking or crossing road. The demonstrated bug is an inward
        # fragment omitted while the outer boundary occupied the ego slot.
        if old_inner is None:
            continue
        candidates = [lane for lane in lanes if lane.get("slot") is None
                      and _recoverable(lane, paint, geo)
                      and (lane.get("x_ref", center)-center)*side > 8
                      and (old_inner is None or (old_inner["x_ref"]-lane["x_ref"])*side > 12)]
        candidates.sort(key=lambda lane: abs(lane["x_ref"]-center))
        if not candidates:
            continue
        if len(candidates) > 1 and abs(candidates[0]["x_ref"]-candidates[1]["x_ref"]) < .025*geo["width"]:
            warnings.append("ambiguous_inward_source_fragments")
            continue
        if not _distinct_observed_boundary(candidates[0],old_inner,paint,geo,side):
            warnings.append("ambiguous_inward_source_continuation")
            continue
        if old_inner is not None and outer in slots:
            warnings.append("inward_fragment_with_full_slot_set")
            continue
        selected = candidates[0]
        if old_inner is not None:
            old_inner["slot"] = outer
            slots[outer] = old_inner
        selected["slot"] = inner
        selected["flags"] = [flag for flag in selected.get("flags", []) if flag not in ("fragment", "beyond_four_lanes")]
        slots[inner] = selected
        recovered += 1
    if _crossing(lanes, geo):
        warnings.append("crossing_source_boundaries")
    # A visible source curve can legitimately have different human extent/type.
    # Keep that uncertainty explicit instead of declaring unchanged geometry fixed.
    remaining_risks = risk_diagnostics({**geo, "lanes": lanes})
    if remaining_risks["close_slot_pairs"]:
        warnings.append("near_duplicate_slot_identity")
    if remaining_risks["curved_long_gaps"]:
        warnings.append("uncertain_source_curve_continuation")
    for lane in lanes:
        if lane.get("slot"):
            measured = [_tangent(lane, edge, paint, geo) for edge in (True, False)]
            if any(value is not None and abs(value[0]) > 5.5 for value in measured):
                warnings.append("uncertain_steep_source_boundary")
    # Retain unresolved prior diagnostics. This audit reminder is normal
    # provenance, rather than unresolved geometric defects; every output here
    # already requires its own fresh human gate.
    informational = {"recovered_source_fragment_requires_audit"}
    warnings.extend(value for value in input_warnings if value not in informational)
    changed = risks["reassociate"] or joins or recovered
    if changed:
        g = _Geo((geo["height"], geo["width"]), GeoParams(horizon_y=geo["horizon_y"]/geo["height"]), geo["valid_bottom"])
        for lane in lanes:
            lane["flags"] = sorted(set(lane.get("flags", [])) - {"crossing", "merge_or_split", "implausible_curve", "curved"})
        tags = lane_checks(lanes, g)
        lanes.sort(key=lambda lane: (lane.get("slot") is None, lane.get("slot") or 0, lane.get("x_ref", 0)))
        for index, lane in enumerate(lanes):
            lane["id"] = index
        geo["lanes"] = lanes
        geo["tags"] = {key:value for key,value in geo.get("tags", {}).items() if key not in ("lane_crossing", "merge_or_split", "curve")}
        geo["tags"].update(tags, lane_count=sum(bool(lane.get("slot")) for lane in lanes))
    family = ("crossing_uncertain" if "crossing_source_boundaries" in warnings else
              "local_reassociation" if risks["reassociate"] else
              "fragment_recovery" if recovered else "boundary_join" if joins else "unchanged_clean")
    geo["source_converter"] = {"version": VERSION, "risk_family": family,
                               "joined_chains": joins, "recovered_short_chains": recovered,
                               "reassociated_source": risks["reassociate"],
                               "requires_fresh_human_audit": True,
                               "warnings": sorted(set(warnings)), "input_warnings": input_warnings,
                               "original_risk": risks}
    return geo
