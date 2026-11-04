"""Dash association fixes are tested with source-only disposable masks."""
from copy import deepcopy

import cv2
import numpy as np

from bcdata.geometry import Chain, GeoParams, Segment, _Geo, extract_lanes
from bcdata.source_converter_v3 import (local_continuation, repair_geometry_v3,
                                      risk_diagnostics, strict_group_dashes)
from tests.synth import lane_prob_from_classes, scene


def segment(top, bottom, slope=-1.5, intercept=1100.):
    ys = np.arange(top, bottom + 1)
    return Segment(ys, slope * ys + intercept, np.ones_like(ys) * 3)


def test_three_row_speck_cannot_bend_a_foreground_dash_across_long_gap():
    lower = segment(559, 629, slope=-1.66, intercept=1174.)
    speck = segment(401, 403, slope=-1.75, intercept=1140.)
    correct = segment(430, 443, slope=-1.66, intercept=1174.)
    g = _Geo((874, 1164), GeoParams(), 629)
    assert local_continuation(lower, speck, g.W, g.H) is None
    chains = strict_group_dashes([lower, speck, correct], g)
    assert len(chains) == 2
    joined = next(chain for chain in chains if lower in chain.segments)
    assert len(joined.segments) == 2
    assert any(member is correct for member in joined.segments)
    assert not any(member is speck for member in joined.segments)


def test_parallel_neighbor_and_transverse_paint_are_not_continuations():
    lower = segment(500, 560)
    neighbor = segment(440, 465, intercept=1160.)
    transverse = segment(440, 465, slope=-8.)
    assert local_continuation(lower, neighbor, 1164, 874) is None
    assert local_continuation(lower, transverse, 1164, 874) is None


def test_independent_well_observed_dashes_join_without_extending_past_paint():
    lower, upper = segment(500, 560), segment(440, 465)
    g = _Geo((874, 1164), GeoParams(), 629)
    chains = strict_group_dashes([lower, upper], g)
    assert len(chains) == 1
    assert (chains[0].y_top, chains[0].y_bottom) == (440, 560)


def test_hood_is_excluded_per_pixel_without_hiding_visible_side_paint():
    h, w = 400, 600
    paint = np.zeros((h, w), np.float32)
    cv2.line(paint, (320, 190), (500, 360), 1., 5)
    my_car = np.zeros((h, w), np.float32)
    my_car[300:, 0:260] = 1.
    # A source-mask mistake inside the hood must not survive pixel exclusion.
    cv2.line(paint, (30, 310), (80, 370), 1., 5)
    original = extract_lanes(paint, GeoParams(horizon_y=.42), my_car=my_car)
    original["image_id"] = "hood-fixture"
    before = deepcopy(original)
    fixed = repair_geometry_v3(original, paint, my_car=my_car)
    assert original == before
    assert original["valid_bottom"] < 300
    assert fixed["valid_bottom"] <= 363
    right = next(lane for lane in fixed["lanes"] if lane["slot"] == 3)
    assert right["y_bottom"] >= 358
    assert all(not (y >= 300 and x < 260) for lane in fixed["lanes"] for x, y, _ in lane["points"])


def test_reextract_empty_source_keeps_empty_labels_and_does_not_invent_extent():
    original = {"width": 400, "height": 400, "horizon_y": 168,
                "valid_bottom": 300, "max_run_frac": .08, "lanes": [], "tags": {}}
    fixed = repair_geometry_v3(original, np.zeros((400, 400), np.float32))
    assert fixed["lanes"] == []
    assert fixed["valid_bottom"] == 300


def test_risk_gate_is_symmetric_and_does_not_label_every_curve_as_wrong():
    original = {"height": 874, "lanes": [{"slot": 4, "y_top": 398, "y_bottom": 600,
                                          "gaps": [[400, 500]], "curvature": .08}]}
    assert risk_diagnostics(original)["risky"]
    original["lanes"][0]["slot"] = 1
    assert risk_diagnostics(original)["risky"]
    original["lanes"][0]["gaps"] = []
    assert not risk_diagnostics(original)["risky"]


def test_measurable_curve_components_do_not_trigger_speck_repair():
    original = {"height": 874, "lanes": [{"slot": 2, "y_top": 436, "y_bottom": 668,
                                          "gaps": [[492, 591], [445, 480]], "curvature": .045}]}
    result = risk_diagnostics(original)
    assert result["curved_long_gaps"]
    assert not result["risky"]


def test_stop_line_and_arrow_protection_survives_reextract():
    _, classes, _ = scene(stop_line=True, arrow=True)
    mask = lane_prob_from_classes(classes)
    original = extract_lanes(mask, GeoParams(horizon_y=.42), my_car=(classes == 5).astype(np.float32))
    fixed = repair_geometry_v3(original, mask, my_car=(classes == 5).astype(np.float32))
    assert {lane["slot"] for lane in fixed["lanes"] if lane["slot"]} == {1, 2, 3, 4}
    assert fixed["tags"]["crosswalk_or_stop_line"]
