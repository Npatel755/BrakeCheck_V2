"""Audi stroke width must not hide missing boundaries, off-paint or slot risks."""
from copy import deepcopy

import numpy as np
import pytest

from bcdata.qc import a2d2_source_coverage, check, mask_polyline_consistency


def line(slot, x, top=70, bottom=199):
    return {"id": slot, "slot": slot, "x_ref": float(x), "y_top": top, "y_bottom": bottom,
            "flags": [], "points": [[float(x), y, True] for y in range(bottom, top-1, -1)]}


def scene(lines=None):
    return {"width": 400, "height": 200, "horizon_y": 0, "valid_bottom": 199,
            "max_run_frac": .3, "lanes": lines if lines is not None else [line(2,100),line(3,300)]}


def paint():
    mask = np.zeros((200,400),np.float32)
    mask[70:,80:121] = 1
    mask[70:,280:321] = 1
    return mask


def codes(geometry, mask, settings, **kwargs):
    return {issue["code"] for issue in check(geometry, {"mode":"source_label"}, mask,None,settings,**kwargs)[1]}


def test_centered_broad_audi_strokes_are_not_missing_paint(settings):
    geometry, mask = scene(),paint()
    area,on = mask_polyline_consistency(geometry,mask)
    assert area < .6 and on == 1
    assert a2d2_source_coverage(geometry,mask)["coverage"] == 1
    assert "low_mask_coverage" in codes(geometry,mask,settings)
    assert codes(geometry,mask,settings,source="a2d2") == set()


@pytest.mark.parametrize("source",[None,"comma10k","comma2k19","carla","other"])
def test_non_audi_behavior_is_identical(settings,source):
    geometry,mask = scene(),paint()
    assert check(geometry,{"mode":"source_label"},mask,None,settings,source=source) == (.75,[{"code":"low_mask_coverage","detail":round(15/41,3)}])


def test_audi_model_output_does_not_receive_source_exception(settings):
    args = (scene(),{"mode":"teachers","metrics":{"teachers":["a","b"]}},paint(),None,settings)
    assert check(*args,source="a2d2") == check(*args)


def test_one_missing_boundary_stays_flagged(settings):
    geometry,mask = scene([line(2,100)]),paint()
    agreement = a2d2_source_coverage(geometry,mask)
    assert agreement["coverage"] == .5
    assert agreement["uncovered_components"]
    assert {"low_mask_coverage","source_boundary_uncovered"} <= codes(geometry,mask,settings,source="a2d2")


def test_missing_short_boundary_is_not_hidden_by_two_long_ones(settings):
    geometry,mask = scene(),paint()
    mask[150:175,190:211] = 1
    agreement = a2d2_source_coverage(geometry,mask)
    assert agreement["coverage"] > .9  # aggregate coverage alone would pass
    assert len(agreement["uncovered_components"]) == 1
    assert "source_boundary_uncovered" in codes(geometry,mask,settings,source="a2d2")


def test_line_at_edge_of_wide_paint_is_not_treated_as_centered(settings):
    geometry,mask = scene([line(2,83),line(3,283)]),paint()
    assert mask_polyline_consistency(geometry,mask)[1] == 1
    assert a2d2_source_coverage(geometry,mask)["coverage"] == 0
    assert "low_mask_coverage" in codes(geometry,mask,settings,source="a2d2")


def test_off_paint_observed_points_keep_existing_failure(settings):
    geometry,mask = scene([line(2,45),line(3,345)]),paint()
    assert {"low_mask_coverage","polyline_off_mask"} <= codes(geometry,mask,settings,source="a2d2")


def test_missing_curved_second_boundary_is_not_hidden_by_perfect_first_boundary(settings):
    import cv2
    geometry = scene([line(2,100)])
    mask = np.zeros((200,400),np.float32)
    mask[70:,80:121] = 1
    ys = np.arange(70,200)
    xs = 250 + ((ys-120)**2 / 140).astype(int)
    cv2.polylines(mask,[np.column_stack([xs,ys]).astype(np.int32)],False,1,21)
    assert mask_polyline_consistency(geometry,mask)[1] == 1
    assert {"source_boundary_uncovered","low_mask_coverage"} <= codes(geometry,mask,settings,source="a2d2")


def test_two_disconnected_slots_on_one_boundary_need_review(settings):
    geometry = scene([line(1,100,150,199),line(2,100,70,110)])
    mask = np.zeros((200,400),np.float32)
    mask[150:,80:121] = 1
    mask[70:111,80:121] = 1
    assert a2d2_source_coverage(geometry,mask)["coverage"] == 1
    assert "source_slot_disjoint" in codes(geometry,mask,settings,source="a2d2")
    assert "low_mask_coverage" in codes(geometry,mask,settings,source="a2d2")


def test_transverse_stopbar_is_not_an_uncovered_longitudinal_boundary(settings):
    geometry,mask = scene(),paint()
    mask[125:132,:] = 1
    agreement = a2d2_source_coverage(geometry,mask)
    assert agreement["coverage"] == 1
    assert not agreement["uncovered_components"]
    assert codes(geometry,mask,settings,source="a2d2") == set()


def test_transverse_only_paint_cannot_establish_complete_source_coverage():
    geometry = scene([line(2,100)])
    mask = np.zeros((200,400),np.float32)
    mask[150:154,100:180] = 1
    result = a2d2_source_coverage(geometry,mask)
    assert result["coverage"] is None
    assert result["source_centres"] == 0


def test_converter_warning_is_not_cleared_by_geometric_agreement(settings):
    geometry,mask = scene(),paint()
    geometry["source_converter"] = {"warnings":["unassigned_source_fragment_requires_review"]}
    assert {"source_conversion_uncertain","low_mask_coverage"} <= codes(geometry,mask,settings,source="a2d2")


def test_qc_does_not_mutate_geometry_or_source_pixels(settings):
    geometry,mask = scene(),paint()
    previous, pixels = deepcopy(geometry),mask.copy()
    check(geometry,{"mode":"source_label"},mask,None,settings,source="a2d2")
    assert geometry == previous
    np.testing.assert_array_equal(mask,pixels)


def test_complete_short_source_span_uses_sample_review_instead_of_length_warning(settings):
    geometry = scene([line(3,300,180,190)])
    mask = np.zeros((200,400),np.float32)
    mask[180:191,280:321] = 1
    assert "short_lane" in codes(geometry,mask,settings)
    assert codes(geometry,mask,settings,source="a2d2") == set()


def test_missing_short_continuation_cannot_clear_length_warning(settings):
    geometry = scene([line(3,300,180,190)])
    mask = np.zeros((200,400),np.float32)
    mask[70:191,280:321] = 1
    assert {"short_lane", "source_boundary_uncovered"} <= codes(geometry,mask,settings,source="a2d2")


def four_lane_scene():
    geometry = scene([line(1,60),line(2,150),line(3,250),line(4,340)])
    mask = np.zeros((200,400),np.float32)
    for x in (60,150,250,340):
        mask[70:,x-10:x+11] = 1
    return geometry,mask


def test_fifth_lane_outside_four_slots_is_not_required_as_a_missing_boundary(settings):
    geometry,mask = four_lane_scene()
    mask[70:,5:16] = 1
    agreement = a2d2_source_coverage(geometry,mask)
    assert agreement["coverage"] == 1 and len(agreement["beyond_four_components"]) == 1
    assert not agreement["uncovered_components"]
    assert codes(geometry,mask,settings,source="a2d2") == set()


@pytest.mark.parametrize("fault",["inside", "no_inner", "unsupported_extent", "off_paint_outer"])
def test_outside_slot_exception_requires_all_observed_rows_and_both_retained_boundaries(settings,fault):
    geometry,mask = four_lane_scene()
    if fault == "inside":
        mask[70:,95:106] = 1
    else:
        mask[50 if fault == "unsupported_extent" else 70:,5:16] = 1
    if fault == "no_inner":
        geometry["lanes"] = [lane for lane in geometry["lanes"] if lane["slot"] != 2]
    if fault == "off_paint_outer":
        geometry["lanes"][0] = line(1,90)
    agreement = a2d2_source_coverage(geometry,mask)
    assert not agreement["beyond_four_components"]
    assert agreement["uncovered_components"]
    assert "source_boundary_uncovered" in codes(geometry,mask,settings,source="a2d2")
