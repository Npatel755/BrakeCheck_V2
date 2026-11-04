"""Re-extract source paint with a local continuation check at every dash join.

An unconstrained three-parameter curve can join a foreground dash to a distant
three-row speck. The fit residual is small although they are different lanes.
This revision requires independent local tangents across a gap. It uses only
source-mask pixels, including visible paint beside the hood, and never consumes
model predictions or human corrections. Installing its output still needs audit.
"""
from copy import deepcopy
import math

import cv2
import numpy as np

from .geometry import (Chain, GeoParams, Segment, _Geo, _gap_between,
                       _intervals_overlap, _joint_fit, _lane_from_chain,
                       assign_slots, filter_segments, lane_checks, merge_double_lines,
                       paint_window, track_segments)


VERSION = "source-continuation-v3"


def _endpoint_tangent(segment, at_top):
    """Estimate direction from a real segment, never from a previous gap fit."""
    ys, xs = segment.ys.astype(float), segment.xs.astype(float)
    if len(ys) < 5 or np.ptp(ys) < 4:
        return None
    edge = float(ys.min() if at_top else ys.max())
    use = np.abs(ys - edge) <= max(16., min(50., .65 * np.ptp(ys)))
    if use.sum() < 5 or np.ptp(ys[use]) < 4:
        return None
    a, b = np.polyfit(ys[use], xs[use], 1)
    return float(a), float(b)


def local_continuation(lower, upper, width, height):
    """Return a bounded error score or reject unrelated dash components.

    Tiny components without a measurable direction can remain source fragments;
    they cannot bend an existing foreground lane across an unobserved gap.
    """
    gap = lower.y_top - upper.y_bottom
    if gap < -2 or gap > .22 * height:
        return None
    lt = _endpoint_tangent(lower, True)
    ut = _endpoint_tangent(upper, False)
    if lt is None or ut is None:
        return None
    angle = abs(math.atan(lt[0]) - math.atan(ut[0]))
    if angle > math.radians(20):
        return None
    yl, yu = float(lower.y_top), float(upper.y_bottom)
    xl = float(np.mean(lower.xs[lower.ys == lower.y_top]))
    xu = float(np.mean(upper.xs[upper.ys == upper.y_bottom]))
    error = max(abs(lt[0] * yu + lt[1] - xu),
                abs(ut[0] * yl + ut[1] - xl))
    # Run centers on a five-row distant segment have quantized tangents. Allow
    # bounded endpoint uncertainty without granting a long gap an unbounded
    # error budget. Three-row specks are rejected before this calculation.
    span = max(4., min(np.ptp(lower.ys), np.ptp(upper.ys)))
    uncertainty = min(.006 * width, 1.5 * max(0., gap) / span)
    tolerance = max(10., .01 * width) + .13 * max(0., gap) + uncertainty
    if error > tolerance:
        return None
    if gap >= 3:
        secant = math.atan((xl - xu) / (yl - yu))
        if max(abs(secant - math.atan(t[0])) for t in (lt, ut)) > math.radians(14):
            return None
    return error / tolerance


def strict_group_dashes(segments, g):
    """Agglomerate only when every new adjacent pair has local support."""
    max_gap = g.p.dash_max_gap_frac * g.H
    rms_tol = max(2., g.p.dash_max_dx_px / 6.)
    max_tol = g.p.dash_max_dx_px / 2.
    chains = [Chain([s]) for s in sorted(segments, key=lambda s: -s.y_bottom)]

    def score(a, b):
        if _intervals_overlap(a, b) or _gap_between(a, b) > max_gap:
            return None
        ordered = sorted([(s, 0) for s in a.segments] + [(s, 1) for s in b.segments],
                         key=lambda item: -item[0].y_bottom)
        errors = []
        for (lower, lower_chain), (upper, upper_chain) in zip(ordered, ordered[1:]):
            if lower_chain == upper_chain:
                continue
            error = local_continuation(lower, upper, g.W, g.H)
            if error is None:
                return None
            errors.append(error)
        fit = _joint_fit(a, b, g)
        if not errors or fit is None or fit[0] > rms_tol or fit[1] > max_tol:
            return None
        return fit[0] + max(errors) + .5 * _gap_between(a, b) / max_gap

    pairs = {}
    for i, a in enumerate(chains):
        for j in range(i + 1, len(chains)):
            value = score(a, chains[j])
            if value is not None:
                pairs[i, j] = value
    alive = set(range(len(chains)))
    while pairs:
        (i, j), _ = min(pairs.items(), key=lambda item: item[1])
        chains[i] = Chain(chains[i].segments + chains[j].segments)
        alive.discard(j)
        pairs = {key: value for key, value in pairs.items() if i not in key and j not in key}
        for k in alive - {i}:
            a, b = sorted((i, k))
            value = score(chains[a], chains[b])
            if value is not None:
                pairs[a, b] = value
    return [chains[i] for i in sorted(alive)]


def risk_diagnostics(original):
    """Identify the demonstrated tangentless-speck join failure mechanism.

    A genuine curve with measurable components stays unchanged. A component's
    observed span is recovered from the stored gap boundaries; spans below four
    rows cannot provide the direction required by Segment.slope(). Both road
    sides use the same check. The result is a risk signal, not a certification.
    """
    risky, curved = [], []
    for lane in original.get("lanes", []):
        if lane.get("slot") not in (1, 2, 3, 4):
            continue
        curvature = float(lane.get("curvature", 0.))
        intervals = sorted((min(float(a), float(b)), max(float(a), float(b)))
                           for a, b in lane.get("gaps", []))
        merged = []
        for lo, hi in intervals:
            if merged and lo <= merged[-1][1]:
                merged[-1][1] = max(hi, merged[-1][1])
            else:
                merged.append([lo, hi])
        for index, (lo, hi) in enumerate(merged):
            gap = hi - lo
            if gap <= .08 * original["height"] or curvature <= .04:
                continue
            upper_top = merged[index - 1][1] if index else lane["y_top"]
            lower_bottom = merged[index + 1][0] if index + 1 < len(merged) else lane["y_bottom"]
            item = {"slot": lane["slot"], "gap_px": gap, "curvature": curvature,
                    "upper_component_span": max(0., lo - upper_top),
                    "lower_component_span": max(0., lower_bottom - hi)}
            curved.append(item)
            if min(item["upper_component_span"], item["lower_component_span"]) < 4:
                risky.append(item)
    return {"risky": bool(risky), "long_gap_curves": risky, "curved_long_gaps": curved}


def repair_geometry_v3(original, lane_prob, my_car=None, movable=None):
    """Return a new source-only draft without changing inputs or stored records."""
    probability = np.asarray(lane_prob, dtype=np.float32)
    h, w = probability.shape
    if (h, w) != (original["height"], original["width"]):
        raise ValueError("Source mask dimensions do not match geometry")
    lane = probability >= .5
    if my_car is not None:
        if np.shape(my_car) != lane.shape:
            raise ValueError("Ego-car mask dimensions do not match source paint")
        lane &= np.asarray(my_car) < .5
    count, labels, stats, _ = cv2.connectedComponentsWithStats(lane.astype(np.uint8), connectivity=8)
    small = np.isin(labels, np.nonzero(stats[:, cv2.CC_STAT_AREA] < 6)[0])
    lane &= ~small
    visible_y = np.nonzero(lane)[0]
    by = int(original["valid_bottom"])
    # A row partly covered by the hood can still have visible paint at its side.
    # Extend the scan only as far as actual non-hood source paint, never to a
    # guessed lane endpoint or a model-proposed continuation.
    if len(visible_y):
        by = min(h - 1, max(by, int(visible_y.max())))
    params = GeoParams(horizon_y=original["horizon_y"] / h,
                       max_run_frac=original.get("max_run_frac", .08))
    g = _Geo((h, w), params, by)
    tracks, track_stats = track_segments(lane, g)
    segments, filter_stats = filter_segments(tracks, g)
    chains = strict_group_dashes(segments, g)
    lanes = [_lane_from_chain(chain, g) for chain in chains]
    lanes = merge_double_lines(lanes, g)
    assign_slots(lanes, g)
    tags = lane_checks(lanes, g)
    for item in lanes:
        real_gaps = []
        for y0, y1 in item["gaps"]:
            from .geometry import lane_xs
            ys = np.arange(y0, y1 + 1)
            xs = np.round(lane_xs(item, ys)).astype(int)
            okay = (xs >= 0) & (xs < w)
            painted = [lane[y, max(0, x - 2):x + 3].any() for y, x in zip(ys[okay], xs[okay])]
            if not painted or np.mean(painted) < .7:
                real_gaps.append([y0, y1])
        item["gaps"] = real_gaps
        if item["kind"] == "dashed" and not real_gaps:
            item["kind"] = "solid"
        values = []
        for x, y, seen in item["points"]:
            xi, yi = int(round(x)), int(y)
            if seen and 0 <= xi < w and 0 <= yi < h:
                radius = paint_window(item, yi, g)
                values.append(float(probability[yi, max(0, xi-radius):xi+radius+1].max()))
        item["mean_confidence"] = round(float(np.mean(values)), 3) if values else 0.
        if values and item["mean_confidence"] < .65:
            item["flags"].append("faded_or_uncertain")
        item["flags"] = sorted(set(item["flags"]))
    if track_stats["wide_rows"] > .02 * (g.by - g.hy):
        tags["crosswalk_or_stop_line"] = True
    if any("faded_or_uncertain" in item["flags"] for item in lanes):
        tags["faded_markings"] = True
    lanes.sort(key=lambda item: (item["slot"] is None, item["slot"] or 0, item["x_ref"]))
    for index, item in enumerate(lanes):
        item["id"] = index
    result = deepcopy(original)
    result.update(valid_bottom=by, lanes=lanes, tags={**original.get("tags", {}), **tags,
                  "lane_count": sum(item["slot"] is not None for item in lanes)},
                  stats={**track_stats, **filter_stats, "tracks": len(tracks), "segments": len(segments), "chains": len(chains)},
                  source_converter={"version": VERSION, "local_continuation_required": True,
                                    "per_pixel_hood": True, "previous_valid_bottom": original["valid_bottom"],
                                    "scan_bottom": by, "requires_fresh_human_audit": True,
                                    "original_risk": risk_diagnostics(original)})
    return result
