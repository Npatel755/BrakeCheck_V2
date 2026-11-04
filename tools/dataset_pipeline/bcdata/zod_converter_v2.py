"""Native paint sections associated by a joint curve, with evidence-only output."""
from collections import Counter

import numpy as np

from .geometry import GeoParams, Segment, _Geo, _lane_from_chain, group_dashes, assign_slots
from .zod_mirror import (CLASSES, CONVERTER, HEIGHT, ID, NATIVE_HEIGHT, NATIVE_WIDTH,
                         WIDTH, centerline, rings)


def convert_sections(native, image_id):
    if not ID.fullmatch(image_id):
        raise ValueError("Invalid ZOD frame ID")
    warnings, polygons, sections, counts = set(), [], [], Counter()
    if not isinstance(native, list):
        native = []
        warnings.add("missing_or_invalid_native_lane_annotations")
    for item in native:
        if not isinstance(item, dict):
            warnings.add("invalid_native_paint_shape")
            continue
        props = item.get("properties") or {}
        cls = props.get("class", "road_paint_other")
        counts[cls] += 1
        for ring in rings(item):
            try:
                trace, polygon = centerline(ring, every_row=True)
            except (ValueError, TypeError):
                warnings.add("invalid_or_out_of_bounds_native_polygon")
                continue
            polygons.append({"class": cls, "points": polygon, "annotation_uuid": props.get("annotation_uuid")})
            if cls not in CLASSES:
                continue
            if len(trace) < 2:
                warnings.add("paint_polygon_needs_manual_trace")
                continue
            arr = np.asarray(trace)
            if np.ptp(arr[:, 0]) > 3 * np.ptp(arr[:, 1]):
                warnings.add("near_transverse_paint_needs_review")
                continue
            # Sub-pixel stubs cannot occupy one of the four training slots.
            if len(trace) < 4:
                continue
            sections.append(Segment(arr[:, 1], arr[:, 0], np.ones(len(arr))))
    params = GeoParams(horizon_y=0, dash_max_dx_px=32, dash_max_dslope=.9)
    geo = _Geo((HEIGHT, WIDTH), params, HEIGHT - 1)
    chains = group_dashes(sections, geo)
    supported = []
    for chain in chains:
        # A wide y-span alone can be created by linking a few distant specks to
        # a disconnected foreground marking. Require actual observed rows,
        # rather than letting empty space provide the slot-selection support.
        if len(np.unique(chain.ys)) < max(8, int(.05 * HEIGHT)):
            warnings.add("sparse_native_paint_needs_review")
        else:
            supported.append(chain)
    chains = supported
    associations = [_lane_from_chain(chain, geo) for chain in chains]
    assign_slots(associations, geo)
    lanes, omitted_long = [], 0
    for chain, association in zip(chains, associations):
        if association["slot"] not in (1, 2, 3, 4):
            omitted_long += int(chain.y_bottom - chain.y_top >= .05 * HEIGHT)
            continue
        # Fits are ONLY association/ordering evidence. No fit coordinates,
        # extrapolation, bridges or road-edge proposals become native labels.
        segments = [[[float(x), float(y)] for y, x in sorted(zip(segment.ys, segment.xs))]
                    for segment in sorted(chain.segments, key=lambda segment: segment.y_top)]
        # Split even touching/overlapping source pieces instead of drawing across
        # different observations. Each exported point came from a polygon row.
        points = [point for segment in segments for point in segment]
        if len(points) > 2048:
            warnings.add("boundary_exceeds_editor_point_limit")
            continue
        lanes.append({"slot": association["slot"], "points": points, "segments": segments,
                      "physical": True, "kind": "lane_marking", "origin": "source_polygon_conversion",
                      "association_x_ref": association["x_ref"]})
        if chain.y_bottom < .65 * HEIGHT:
            warnings.add("far_field_only_boundary_needs_review")
    if omitted_long:
        warnings.add("unassigned_long_marking_candidate")
    lanes.sort(key=lambda lane: lane["slot"])
    # Ordering is checked on actual common rows, not polynomial extrapolations.
    for i, left in enumerate(lanes):
        first = {point[1]: point[0] for point in left["points"]}
        for right in lanes[i + 1:]:
            second = {point[1]: point[0] for point in right["points"]}
            if any(first[y] >= second[y] for y in first.keys() & second.keys()):
                warnings.add("observed_slot_order_needs_review")
    if not lanes:
        warnings.add("no_converted_lane_requires_whole_photo_check")
    return {"image_id": image_id, "width": WIDTH, "height": HEIGHT, "version": CONVERTER,
            "lanes": lanes, "source_polygons": polygons, "source_class_counts": dict(counts),
            "coordinate_transform": {"native_width": NATIVE_WIDTH, "native_height": NATIVE_HEIGHT,
                                     "status": "provisional_official_sensor_dimensions_check_overlay"},
            "annotation_policy": "observed_polygon_rows_no_gap_fill_no_endpoint_extension",
            "conversion_stats": {"source_sections": len(sections), "associated_boundaries": len(chains),
                                 "unassigned_long_boundaries": omitted_long}}, sorted(warnings)
