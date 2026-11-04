"""Conservative recovery of source-mask lane fragments, with explicit audit provenance.

This operates on observed paint chains, never on model predictions or human edits.
Every changed conversion still needs the source audit gate before export.
"""
from copy import deepcopy
import math

import numpy as np

from .geometry import GeoParams, _Geo, _x_at, assign_slots, lane_checks, paint_window

VERSION = "source-fragments-v2"


def observed(lane):
    return np.asarray([p[:2] for p in lane.get("points", []) if len(p) < 3 or p[2]], float).reshape(-1, 2)


def source_supported(lane, lane_prob, geo=None):
    """Check observed centers against source paint, excluding unpainted dash gaps.

    Keeping model-fit fragments in a geometry file is not evidence that they
    remain on the source mask. In particular, joining two unsupported fragments
    must not make them eligible merely by increasing their combined extent.
    """
    pts = observed(lane)
    if len(pts) < 2 or not np.isfinite(pts).all() or lane.get("physical") is False:
        return False
    supported = 0
    for x, y in pts:
        xi, yi = round(float(x)), round(float(y))
        if 0 <= yi < lane_prob.shape[0] and 0 <= xi < lane_prob.shape[1]:
            radius = 4
            if geo is not None:
                radius = max(radius, paint_window(lane, yi, hy=geo["horizon_y"], by=geo["valid_bottom"]))
            supported += bool(lane_prob[yi, max(0, xi-radius):xi+radius+1].max() >= .5)
    return supported / len(pts) >= .85


def tangent(lane, at_top):
    points = observed(lane)
    if len(points) < 2:
        return None
    edge = points[:, 1].min() if at_top else points[:, 1].max()
    use = points[np.abs(points[:, 1] - edge) <= max(8., .35 * np.ptp(points[:, 1]))]
    if len(use) < 2 or np.ptp(use[:, 1]) < 3:
        return None
    a, b = np.polyfit(use[:, 1], use[:, 0], 1)
    return float(a), float(b)


def join_score(a, b, width, height):
    """Require separated spans and mutually agreeing local continuation tangents."""
    lower, upper = (a, b) if a["y_bottom"] > b["y_bottom"] else (b, a)
    gap = lower["y_top"] - upper["y_bottom"]
    if gap < 3 or gap > .22 * height:
        return None
    lt, ut = tangent(lower, True), tangent(upper, False)
    if lt is None or ut is None:
        return None
    angle = abs(math.atan(lt[0]) - math.atan(ut[0]))
    if angle > math.radians(16):
        return None
    yl, yu = lower["y_top"], upper["y_bottom"]
    xl = float(_x_at(lower, yl)); xu = float(_x_at(upper, yu))
    # Bidirectional endpoint extrapolation prevents a quadratic from joining
    # neighboring parallel lane boundaries across a large blank region.
    error = max(abs(lt[0] * yu + lt[1] - xu), abs(ut[0] * yl + ut[1] - xl))
    tolerance = max(7., .008 * width) + .07 * gap
    if error > tolerance:
        return None
    secant = (xl - xu) / (yl - yu)
    if max(abs(math.atan(secant) - math.atan(t[0])) for t in (lt, ut)) > math.radians(12):
        return None
    return error / tolerance + gap / height


def merge_chains(a, b):
    merged = deepcopy(a)
    points = sorted([p for lane in (a, b) for p in lane["points"]], key=lambda p: p[1])
    # Preserve observed chain geometry. Interpolate only between the two observed
    # spans instead of bending a high-degree polynomial through disconnected paint.
    unique = {p[1]: p for p in points}
    ys = sorted(unique)
    merged.update(fit={"basis": "interp", "ys": ys, "xs": [unique[y][0] for y in ys]},
                  points=[unique[y] for y in reversed(ys)], y_top=min(ys), y_bottom=max(ys),
                  segments=a.get("segments", 1) + b.get("segments", 1), kind="dashed",
                  gaps=a.get("gaps", []) + b.get("gaps", []) + [[min(a["y_bottom"], b["y_bottom"]), max(a["y_top"], b["y_top"])]],
                  flags=sorted((set(a.get("flags", [])) | set(b.get("flags", []))) - {"fragment", "beyond_four_lanes"}),
                  mean_confidence=min(a.get("mean_confidence", 1.), b.get("mean_confidence", 1.)))
    merged["repair_join"] = True
    merged["repair_original_slots"] = sorted(set(a.get("repair_original_slots", [])) | set(b.get("repair_original_slots", [])))
    return merged


def recoverable(lane, geo, lane_prob):
    pts = observed(lane)
    if len(pts) < 2 or np.ptp(pts[:, 1]) < 6:
        return False
    span = float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())
    if span < max(24., .025 * geo["width"]):
        return False
    t = tangent(lane, False)
    if t is None or abs(t[0]) > 5.5:
        return False
    vp_x = t[0] * geo["horizon_y"] + t[1]
    if not .2 * geo["width"] <= vp_x <= .8 * geo["width"]:
        return False
    return source_supported(lane, lane_prob, geo)


def repair_geometry(original, lane_prob, recover_short=False):
    geo = deepcopy(original)
    lanes = deepcopy(original["lanes"])
    for lane in lanes:
        lane["repair_original_slots"] = [lane["slot"]] if lane.get("slot") else []
    support = {id(lane): source_supported(lane, lane_prob, geo) for lane in lanes}
    joins = 0
    while True:
        pairs = [(score, i, j) for i, a in enumerate(lanes) for j, b in enumerate(lanes[:i])
                 if support[id(a)] and support[id(b)]
                 and not (set(a["repair_original_slots"] + b["repair_original_slots"]) & {1, 2}
                          and set(a["repair_original_slots"] + b["repair_original_slots"]) & {3, 4})
                 and not (a.get("slot") and b.get("slot")
                          and abs(a.get("x_ref", 0.)-b.get("x_ref", 0.)) > .04*geo["width"])
                 and (score := join_score(a, b, geo["width"], geo["height"])) is not None]
        if not pairs:
            break
        _, i, j = min(pairs)
        merged = merge_chains(lanes[i], lanes[j])
        support[id(merged)] = source_supported(merged, lane_prob, geo)
        lanes = [lane for k, lane in enumerate(lanes) if k not in (i, j)] + [merged]
        joins += 1
    g = _Geo((geo["height"], geo["width"]), GeoParams(horizon_y=geo["horizon_y"] / geo["height"]), geo["valid_bottom"])
    assign_slots(lanes, g)
    ego = .5 * geo["width"]
    slots = {}
    for lane in lanes:
        lane["slot"] = None
        original_slots = lane["repair_original_slots"]
        if original_slots:
            # Joining two fragments of a previously duplicated boundary keeps
            # its ego-facing slot. New fragments never displace existing labels.
            slot = max(original_slots) if max(original_slots) <= 2 else min(original_slots)
            lane["slot"] = slot
            slots[slot] = lane
    candidates = [lane for lane in lanes if not lane["repair_original_slots"] and recoverable(lane, geo, lane_prob)]
    recovered = 0
    warnings = []
    suggestions = []
    for side, order, key in ((-1, (2, 1), lambda lane: -lane["x_ref"]),
                             (1, (3, 4), lambda lane: lane["x_ref"])):
        side_candidates = sorted([lane for lane in candidates if (lane["x_ref"] - ego) * side >= 0], key=key)
        for lane in side_candidates:
            available = [slot for slot in order if slot not in slots]
            if not available:
                continue
            slot = available[0]
            existing_inner = slots.get(order[0])
            existing_outer = slots.get(order[1])
            # The source converter already established ego/outer identities.
            # Fill a vacant slot only on the appropriate side of its neighbor.
            if ((slot == order[1] and existing_inner is not None
                 and (lane["x_ref"] - existing_inner["x_ref"]) * side <= 8.)
                    or (slot == order[0] and existing_outer is not None
                        and (existing_outer["x_ref"] - lane["x_ref"]) * side <= 8.)):
                warnings.append("inward_or_duplicate_fragment")
                continue
            # Short markings remain suggestions by default. Source paint proves
            # that a fragment exists, but does not prove that a full lane should
            # be exported or justify extending it beyond its observed span.
            joined_eligible = lane.get("repair_join") and lane["y_bottom"] - lane["y_top"] >= max(8, int(.05*geo["height"]))
            suggestion = deepcopy(lane)
            suggestion["flags"] = [flag for flag in suggestion.get("flags", []) if flag not in ("fragment", "beyond_four_lanes")]
            suggestion.update(slot=slot, repair_short_chain=True)
            if recover_short or joined_eligible:
                lane.update(suggestion)
                recovered += not bool(joined_eligible)
                warnings.append("recovered_source_fragment_requires_audit")
            else:
                suggestions.append(suggestion)
                warnings.append("unassigned_source_fragment_requires_review")
            slots[slot] = lane
    provenance = {"version": VERSION, "joined_chains": joins, "recovered_short_chains": recovered,
                  "short_recovery_candidates": len(suggestions), "short_chain_suggestions": suggestions,
                  "warnings": sorted(set(warnings))}
    if not joins and not recovered:
        geo["source_converter"] = provenance
        return geo
    lanes.sort(key=lambda l: (l["slot"] is None, l["slot"] or 0, l["x_ref"]))
    for index, lane in enumerate(lanes):
        lane["id"] = index
        lane["flags"] = sorted(set(lane.get("flags", [])) - {"crossing", "implausible_curve", "merge_or_split", "curved"})
    geo["lanes"] = lanes
    geo["tags"] = {**geo.get("tags", {}), **lane_checks(lanes, g), "lane_count": sum(bool(l.get("slot")) for l in lanes)}
    geo["source_converter"] = provenance
    return geo


def source_candidate(original, lane_prob, source_sha256, geometry_sha256, repaired=None):
    """Optional comparison overlay; adoption always creates an actual human edit."""
    if repaired is None:
        candidate = repair_geometry(original, lane_prob, recover_short=True)
    else:
        candidate = deepcopy(repaired)
        suggestions = candidate.get('source_converter', {}).get('short_chain_suggestions', [])
        for suggestion in suggestions:
            candidate['lanes'] = [lane for lane in candidate['lanes']
                                  if lane.get('slot') or lane['points'] != suggestion['points']]
            candidate['lanes'].append(deepcopy(suggestion))
        candidate['lanes'].sort(key=lambda lane: (lane.get('slot') is None, lane.get('slot') or 0))
        for index, lane in enumerate(candidate['lanes']):
            lane['id'] = index
        candidate['tags']['lane_count'] = sum(bool(lane.get('slot')) for lane in candidate['lanes'])
    candidate.update(teacher="source_refined", source_guided=True,
                     teacher_version=VERSION + ":" + geometry_sha256[:16] + ":" + source_sha256[:16],
                     model_meta={"source_sha256":source_sha256, "geometry_sha256":geometry_sha256})
    return candidate
