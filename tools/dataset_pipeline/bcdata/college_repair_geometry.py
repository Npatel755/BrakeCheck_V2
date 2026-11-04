"""Prepare source-only College geometry candidates without publishing decisions.

All coordinates originate on an observed native SCNN edge. Native branches and
horizontal edges remain explicit diagnostic evidence: choosing one branch, adding
missing paint, or treating an annotation as verified visible paint needs a human.
This module never writes source files, clears audit warnings, or approves photos.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from itertools import combinations, permutations
import math

from .college_group_clearance import has_observed_training_span
from .import_college_station import CONVERTER, clean_audit_base, parse_lines

VERSION = "college-observed-repair-candidates-v2"
HARD_WARNINGS = frozenset({"native_coverage_incomplete", "invalid_native_number",
    "invalid_native_polyline", "degenerate_native_polyline",
    "native_polyline_not_single_valued", "native_points_outside_image"})


def _native_traces(native):
    traces = []
    errors = []
    for number, line in enumerate(native.decode("utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            values = [float(value) for value in line.split()]
        except ValueError:
            errors.append({"source_line": number, "reason": "invalid_native_number"})
            continue
        if len(values) < 4 or len(values) % 2 or not all(map(math.isfinite, values)):
            errors.append({"source_line": number, "reason": "invalid_native_polyline"})
            continue
        # Remove consecutive duplicates only; revisiting a point can be evidence
        # of a source branch and must not be silently discarded.
        points = []
        for point in zip(values[::2], values[1::2]):
            if not points or points[-1] != point:
                points.append(point)
        if len(points) < 2:
            errors.append({"source_line": number, "reason": "degenerate_native_polyline"})
            continue
        traces.append({"source_line": number, "points": points})
    return traces, errors


def clip_observed_edge(a, b, width, height):
    """Liang-Barsky intersection with the image, never endpoint clamping.

    Image coordinates are continuous on [0,width) x [0,height), matching the
    existing reviewer. nextafter gives a representable inclusive upper bound.
    Returned t values prove each generated endpoint lies on its native edge.
    """
    if width <= 0 or height <= 0:
        raise ValueError("Image dimensions must be positive")
    xmax = math.nextafter(float(width), -math.inf)
    ymax = math.nextafter(float(height), -math.inf)
    dx, dy = b[0] - a[0], b[1] - a[1]
    low, high = 0.0, 1.0
    for p, q in ((-dx, a[0]), (dx, xmax - a[0]), (-dy, a[1]), (dy, ymax - a[1])):
        if p == 0:
            if q < 0:
                return None
            continue
        value = q / p
        if p < 0:
            low = max(low, value)
        else:
            high = min(high, value)
        if low > high:
            return None
    first = [a[0] + low * dx, a[1] + low * dy]
    last = [a[0] + high * dx, a[1] + high * dy]
    # A few ulps of roundoff at a computed intersection must not leave the
    # documented rectangle. This is numerical correction after intersection.
    for point in (first, last):
        point[0] = min(xmax, max(0.0, point[0]))
        point[1] = min(ymax, max(0.0, point[1]))
    if first == last:
        return None
    return {"points": [first, last], "native_edge_parameters": [low, high]}


def observed_sections(points, width, height):
    """Split at row reversals, horizontal edges, and excursions out of frame."""
    sections, horizontal, edges = [], [], []
    active, direction = [], 0
    for index, (a, b) in enumerate(zip(points, points[1:])):
        clipped = clip_observed_edge(a, b, width, height)
        if clipped is None:
            if len(active) >= 2:
                sections.append(active)
            active, direction = [], 0
            continue
        first, last = clipped["points"]
        edges.append({"native_edge_index": index, **clipped})
        sign = 1 if last[1] > first[1] else -1 if last[1] < first[1] else 0
        if sign == 0:
            if len(active) >= 2:
                sections.append(active)
            horizontal.append(clipped["points"])
            active, direction = [], 0
        elif active and active[-1] == first and sign == direction:
            active.append(last)
        else:
            if len(active) >= 2:
                sections.append(active)
            active, direction = [first, last], sign
    if len(active) >= 2:
        sections.append(active)
    spans = [(min(p[1] for p in part), max(p[1] for p in part)) for part in sections]
    overlap = any(max(a, c) < min(b, d) for (a, b), (c, d) in combinations(spans, 2))
    return {"sections": sections, "horizontal_edges": horizontal,
            "overlapping_row_support": overlap, "observed_edges": edges}


def _x(points, y):
    for a, b in zip(points, points[1:]):
        if min(a[1], b[1]) <= y <= max(a[1], b[1]) and a[1] != b[1]:
            return a[0] + (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1])
    raise ValueError("Reference row is outside observed support")


def observed_slot_assignments(lanes, fixed_slots):
    """Enumerate slots using only pairwise ordering on actual overlapping rows.

    fixed_slots identifies retained source hints. Ordering alone does not establish
    ego identity. A unique result is required before any candidate slot changes.
    Disconnected traces, crossing boundaries, and unanchored ego identity hold.
    """
    if not 1 <= len(lanes) <= 4:
        return {"assignments": [], "comparisons": [], "reason": "unsupported_lane_count"}
    comparisons = []
    for a, b in combinations(lanes, 2):
        pa, pb = a["points"], b["points"]
        low = max(min(p[1] for p in pa), min(p[1] for p in pb))
        high = min(max(p[1] for p in pa), max(p[1] for p in pb))
        if low >= high:
            continue
        rows = sorted({low, high} | {p[1] for p in pa + pb if low < p[1] < high})
        diffs = [_x(pa, y) - _x(pb, y) for y in rows]
        if all(delta < -1e-7 for delta in diffs):
            left, right = a["source_line"], b["source_line"]
        elif all(delta > 1e-7 for delta in diffs):
            left, right = b["source_line"], a["source_line"]
        else:
            return {"assignments": [], "comparisons": comparisons,
                    "reason": "crossing_or_touching_observed_traces"}
        comparisons.append({"left_source_line": left, "right_source_line": right,
                            "overlap_rows": [low, high], "checked_rows": rows})
    ids = [lane["source_line"] for lane in lanes]
    assignments = []
    for slots in permutations((1, 2, 3, 4), len(lanes)):
        assignment = dict(zip(ids, slots))
        if any(assignment.get(key) != value for key, value in fixed_slots.items()):
            continue
        if any(assignment[c["left_source_line"]] >= assignment[c["right_source_line"]]
               for c in comparisons):
            continue
        # No disconnected added trace is assigned a slot from absence alone.
        if any(key not in fixed_slots and not any(key in (c["left_source_line"], c["right_source_line"])
                                                for c in comparisons) for key in ids):
            continue
        assignments.append(assignment)
    return {"assignments": assignments, "comparisons": comparisons,
            "reason": "unique_observed_order" if len(assignments) == 1 else "ego_identity_or_order_ambiguous"}


def propose_candidate(record, native_bytes, source_geometry):
    """Return an unapproved, reviewable candidate; inputs are never mutated."""
    geometry = deepcopy(source_geometry)
    original = sorted(set(record.get("qc_reasons", [])))
    width, height = geometry["width"], geometry["height"]
    native_digest = hashlib.sha256(native_bytes).hexdigest()
    if record.get("source_label_sha256") not in (None, native_digest):
        raise ValueError("Native label digest differs from manifest")
    if record.get("converter", CONVERTER) != CONVERTER:
        raise ValueError("Unsupported native converter")
    replay, replay_warnings = parse_lines(native_bytes, width, height)
    # A preexisting hand edit must never be mistaken for native geometry.
    if any(source_geometry.get(key) != replay.get(key) for key in ("lanes", "omitted_native_source_lines")):
        raise ValueError("Source geometry does not replay from the native annotation")
    traces, errors = _native_traces(native_bytes)
    diagnostics = {"native_parse_errors": errors, "native_traces": [],
                   "native_replayed_warnings": replay_warnings,
                   "source_omitted_lines": list(geometry.get("omitted_native_source_lines", [])),
                   "mechanical_operations": [], "blockers": []}
    usable = []
    for trace in traces:
        parts = observed_sections(trace["points"], width, height)
        outside = any(not 0 <= x < width or not 0 <= y < height for x, y in trace["points"])
        diagnostic = {"source_line": trace["source_line"],
                      "native_points": [list(p) for p in trace["points"]],
                      "had_outside_points": outside, **parts}
        diagnostics["native_traces"].append(diagnostic)
        if parts["overlapping_row_support"] or parts["horizontal_edges"] or len(parts["sections"]) != 1:
            continue
        points = parts["sections"][0]
        usable.append({"source_line": trace["source_line"], "points": points})
    present = {lane["source_line"] for lane in geometry["lanes"]}
    fixed = {lane["source_line"]: lane["slot"] for lane in geometry["lanes"]}
    ordering = observed_slot_assignments(usable, fixed) if len(usable) == len(traces) else {
        "assignments": [], "comparisons": [], "reason": "native_branches_need_visual_selection"}
    diagnostics["observed_slot_evidence"] = ordering
    resolved = []
    # Restore an omitted off-frame annotation only when clipping produces exactly
    # one observed section and retained, overlapping source slots locate it uniquely.
    if "native_points_outside_image" in original and len(ordering["assignments"]) == 1:
        slots = ordering["assignments"][0]
        clipped = [part for part in diagnostics["native_traces"] if part["had_outside_points"]]
        if clipped and all(part["source_line"] not in present for part in clipped):
            for part in clipped:
                line = part["source_line"]
                points = part["sections"][0]
                ordered = sorted(points, key=lambda p: p[1])
                geometry["lanes"].append({"id": line - 1, "source_line": line,
                    "slot": slots[line], "kind": "unknown", "physical": True,
                    "visibility": "unknown", "physical_evidence": "publisher_annotation_only_unverified",
                    "visible_paint_verified": False, "points": [[*p, True] for p in reversed(ordered)],
                    "fit": {"basis": "interp", "xs": [p[0] for p in ordered], "ys": [p[1] for p in ordered]},
                    "y_top": ordered[0][1], "y_bottom": ordered[-1][1], "gaps": [],
                    "flags": ["native_points_outside_image"], "candidate_operation": "clip_observed_native_edges"})
                geometry["omitted_native_source_lines"] = [number for number in
                    geometry.get("omitted_native_source_lines", []) if number != line]
                diagnostics["mechanical_operations"].append({"operation": "clip_observed_native_edges",
                    "source_line": line, "slot": slots[line], "native_warning_preserved": True})
            geometry["lanes"].sort(key=lambda lane: lane["slot"])
            geometry["tags"]["lane_count"] = len(geometry["lanes"])
            resolved.append("native_points_outside_image")
    remaining = [reason for reason in original if reason not in resolved]
    blockers = sorted(set(remaining) & HARD_WARNINGS)
    # Full native traces can exceed the output slot count or include intentionally
    # omitted transverse markings. Check retained geometry separately: an empty
    # full-native assignment set must not hide a contradiction in existing slots.
    retained_order = observed_slot_assignments(geometry["lanes"], {
        lane["source_line"]: lane["slot"] for lane in geometry["lanes"]})
    diagnostics["retained_slot_evidence"] = retained_order
    if len(geometry["lanes"]) >= 2 and not retained_order["assignments"]:
        blockers.append("observed_slot_order_contradiction")
    span = has_observed_training_span(geometry)
    if not span:
        blockers.append("no_observed_training_span")
    diagnostics["blockers"] = blockers
    diagnostics["has_observed_training_span"] = span
    diagnostics["missing_source_paint_inferred"] = False
    diagnostics["extrapolated_points_created"] = 0
    diagnostics["visible_paint_verified"] = False
    try:
        base = clean_audit_base(record)
    except (KeyError, ValueError):
        base = record.get("sequence_id", record.get("group_key", "unknown"))
    disposition = "ready_for_audit" if not blockers else (
        "no_training_span" if blockers == ["no_observed_training_span"] else "held")
    return {"image_id": record["image_id"], "geometry": geometry,
        "candidate_status": disposition, "resolved_warning_reasons": resolved,
        "original_warnings": original, "remaining_warning_reasons": remaining,
        "diagnostics": diagnostics,
        "provenance": {"generator": VERSION, "source_geometry_sha256": record.get("geometry_sha256"),
            "source_sha256": record.get("source_sha256"), "native_sha256": native_digest,
            "source_group_key": record.get("group_key"), "source_member": record.get("source_member"),
            "source_warnings_preserved": True, "human_decisions_created": 0, "auto_promote": False},
        "batch_key": base + ":" + "+".join(remaining or ["mechanical_repair"]) + ":" + disposition,
        "review_required": True}
