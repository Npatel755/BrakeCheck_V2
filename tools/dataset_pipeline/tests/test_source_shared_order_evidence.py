"""Shared order needs actual simultaneous source support, not x_ref metadata."""
from copy import deepcopy

import numpy as np
import pytest

from bcdata.source_shared_order_evidence import VERSION, measure
from bcdata.source_target_evidence import measure as complete_evidence


def fixture():
    width, height, top, bottom = 1000, 400, 50, 350
    paint = np.zeros((height, width), np.float32)
    lanes = []
    for slot, x in ((1, 100), (2, 350), (3, 650), (4, 900)):
        ys = list(range(top, bottom + 1))
        paint[top:bottom + 1, x - 2:x + 3] = 1
        lanes.append({"slot": slot, "physical": True, "y_top": top, "y_bottom": bottom,
                      "fit": {"basis": "interp", "ys": ys, "xs": [x] * len(ys)},
                      "points": [[x, y, True] for y in reversed(ys)], "gaps": [],
                      # A deliberately misleading mixed-depth metadata order.
                      "x_ref": {1: 375, 2: 350, 3: 650, 4: 625}[slot]})
    geometry = {"width": width, "height": height, "horizon_y": 0,
                "valid_bottom": height - 1, "lanes": lanes, "source_converter": {"warnings": []}}
    evidence = complete_evidence(geometry, paint)
    assert evidence["eligible_for_fresh_audit"]
    return geometry, paint, evidence


def change_x(lane, xs):
    lane["fit"]["xs"] = list(xs)
    by_y = dict(zip(lane["fit"]["ys"], xs))
    for point in lane["points"]:
        point[0] = by_y[point[1]]


def test_actual_shared_paint_proves_order_without_changing_geometry_or_reference():
    geometry, paint, native = fixture()
    before, pixels, receipt = deepcopy(geometry), paint.copy(), deepcopy(native)
    proof = measure(geometry, paint, native)
    assert proof["version"] == VERSION and proof["eligible"]
    assert proof["verified_slots"] == [1, 2, 3, 4]
    assert [pair["shared_source_rows"] for pair in proof["adjacent_pairs"]] == [301, 301, 301]
    assert all(pair["all_measured_rows_in_order"] for pair in proof["adjacent_pairs"])
    assert proof["geometry_changed"] is False and proof["x_ref_changed"] is False
    assert proof["source_only"] and proof["requires_fresh_human_audit"]
    assert geometry == before and native == receipt
    np.testing.assert_array_equal(paint, pixels)


def test_two_ego_boundaries_are_sufficient_but_both_are_required():
    geometry, paint, native = fixture()
    geometry["lanes"] = geometry["lanes"][1:3]
    assert measure(geometry, paint, native)["verified_slots"] == [2, 3]
    geometry["lanes"].pop()
    assert measure(geometry, paint, native) is None


def test_short_outer_paint_before_global_foreground_still_has_shared_measured_order():
    geometry, paint, native = fixture()
    lane = geometry["lanes"][0]
    lane["y_bottom"] = 90
    lane["fit"]["ys"] = list(range(50, 91))
    lane["fit"]["xs"] = [100] * 41
    lane["points"] = [[100, y, True] for y in range(90, 49, -1)]
    proof = measure(geometry, paint, native)
    assert proof["adjacent_pairs"][0]["shared_source_rows"] == 41
    assert proof["adjacent_pairs"][0]["shared_y"] == [50, 90]


@pytest.mark.parametrize("change", [
    {"eligible_for_fresh_audit": False}, {"version": "other"},
    {"represented_fraction": .979}, {"observed_points_on_paint": .979},
    {"crossings": [[2, 3]]}, {"unrepresented_components": [{}]},
    {"unhandled_converter_warnings": ["ambiguous"]}, {"near_duplicate_slot_pairs": [[1, 2]]},
])
def test_incomplete_or_inconsistent_native_evidence_fails_closed(change):
    geometry, paint, native = fixture()
    assert measure(geometry, paint, {**native, **change}) is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "1.0", None, True, 1.001])
def test_nonfinite_or_invalid_native_proof_fails_closed(value):
    geometry, paint, native = fixture()
    assert measure(geometry, paint, {**native, "represented_fraction": value}) is None


def test_reversed_slot_pair_cannot_use_complete_source_coverage():
    geometry, paint, native = fixture()
    geometry["lanes"][0]["slot"], geometry["lanes"][1]["slot"] = 2, 1
    assert measure(geometry, paint, native) is None


def test_one_crossing_row_is_not_hidden_by_quantile_order():
    geometry, paint, native = fixture()
    lane = geometry["lanes"][0]
    xs = [100] * len(lane["fit"]["ys"])
    xs[100] = 400
    change_x(lane, xs)
    paint[150, 398:403] = 1
    assert measure(geometry, paint, native) is None


def test_one_near_contact_row_fails_the_all_rows_separation_gate():
    geometry, paint, native = fixture()
    lane = geometry["lanes"][0]
    xs = [100] * len(lane["fit"]["ys"])
    xs[100] = 344
    change_x(lane, xs)
    paint[150, 342:347] = 1
    assert measure(geometry, paint, native) is None


def test_seven_shared_painted_rows_are_insufficient_despite_long_independent_spans():
    geometry, paint, native = fixture()
    paint[57:351, 98:103] = 0
    assert measure(geometry, paint, native) is None


def test_nonoverlapping_paint_ranges_cannot_supply_an_ordering_edge():
    geometry, paint, native = fixture()
    paint[50:201, 98:103] = 0
    paint[201:351, 348:353] = 0
    assert measure(geometry, paint, native) is None


def test_declared_interpolated_gap_cannot_use_an_unrelated_painted_stripe():
    geometry, paint, native = fixture()
    lane = geometry["lanes"][0]
    lane["gaps"] = [[50, 350]]
    lane["points"] = [[100, 350, True], [100, 50, True]]
    # Even completely painted fits cannot make declared inferred gap rows measured.
    assert measure(geometry, paint, native) is None


def test_hidden_points_cannot_supply_measured_rows_even_when_pixels_are_painted():
    geometry, paint, native = fixture()
    for point in geometry["lanes"][0]["points"]:
        point[2] = False
    assert measure(geometry, paint, native) is None


def test_valid_dashed_intervals_are_supported_only_where_both_curves_have_actual_paint():
    geometry, paint, native = fixture()
    lane = geometry["lanes"][2]
    lane["gaps"] = [[100, 200]]
    for point in lane["points"]:
        if 100 < point[1] < 200:
            point[2] = False
    proof = measure(geometry, paint, native)
    assert proof and proof["adjacent_pairs"][1]["shared_source_rows"] < 301
    assert proof["adjacent_pairs"][1]["shared_source_rows"] >= 190


def test_ego_last_measured_rows_must_stay_on_correct_side():
    geometry, paint, native = fixture()
    lane = geometry["lanes"][1]
    xs = [350] * len(lane["fit"]["ys"])
    xs[-1] = 510
    change_x(lane, xs)
    paint[350, 508:513] = 1
    assert measure(geometry, paint, native) is None


def test_ego_tail_cannot_touch_the_centre_margin():
    geometry, paint, native = fixture()
    lane = geometry["lanes"][1]
    xs = [350] * len(lane["fit"]["ys"])
    xs[-1] = 485
    change_x(lane, xs)
    paint[350, 483:488] = 1
    assert measure(geometry, paint, native) is None


def test_ego_source_paint_must_reach_the_global_foreground():
    geometry, paint, native = fixture()
    paint[200:, 348:353] = 0
    assert measure(geometry, paint, native) is None


def test_ego_needs_all_twenty_four_tail_measurements():
    geometry, paint, native = fixture()
    paint[50:328, 348:353] = 0
    assert measure(geometry, paint, native) is None


def test_source_observed_row_metadata_is_never_trusted():
    geometry, paint, native = fixture()
    for lane in geometry["lanes"]:
        lane["source_observed_rows"] = list(range(400))
    paint[:, 98:103] = 0
    assert measure(geometry, paint, native) is None
    geometry, paint, native = fixture()
    for lane in geometry["lanes"]:
        lane["source_observed_rows"] = []
    assert measure(geometry, paint, native)


def test_actual_points_cannot_use_a_different_correct_fit():
    geometry, paint, native = fixture()
    geometry["lanes"][0]["points"][100][0] += 10
    assert measure(geometry, paint, native) is None


def test_drawn_observed_point_needs_its_own_pixel_support_even_within_fit_tolerance():
    geometry, paint, native = fixture()
    paint[150, 98:103] = 0
    paint[150, 100] = 1
    geometry["lanes"][0]["points"][200][0] = 102
    assert measure(geometry, paint, native) is None


def test_nonfinite_reference_metadata_is_malformed_even_though_not_used_as_evidence():
    geometry, paint, native = fixture()
    geometry["lanes"][0]["x_ref"] = float("nan")
    assert measure(geometry, paint, native) is None


def test_points_cannot_skip_the_declared_span_endpoints():
    geometry, paint, native = fixture()
    geometry["lanes"][0]["points"] = geometry["lanes"][0]["points"][1:-1]
    assert measure(geometry, paint, native) is None


def test_interp_fit_cannot_extrapolate_beyond_its_measured_knots():
    geometry, paint, native = fixture()
    geometry["lanes"][0]["fit"]["ys"] = [100, 300]
    geometry["lanes"][0]["fit"]["xs"] = [100, 100]
    assert measure(geometry, paint, native) is None


@pytest.mark.parametrize("slot", [True, 0, 5, "2"])
def test_invalid_slot_identifiers_fail_closed(slot):
    geometry, paint, native = fixture()
    geometry["lanes"][0]["slot"] = slot
    assert measure(geometry, paint, native) is None


def test_duplicate_slots_fail_closed():
    geometry, paint, native = fixture()
    geometry["lanes"][0]["slot"] = 2
    assert measure(geometry, paint, native) is None


@pytest.mark.parametrize("field,value", [("width", True), ("height", -1), ("horizon_y", -1), ("valid_bottom", 400)])
def test_invalid_native_image_bounds_fail_closed(field, value):
    geometry, paint, native = fixture()
    geometry[field] = value
    assert measure(geometry, paint, native) is None


@pytest.mark.parametrize("change", [
    {"fit": {"basis": "unknown"}},
    {"fit": {"basis": "interp", "ys": [50, 350], "xs": [100, float("nan")]}},
    {"fit": {"basis": "poly", "coef": [100], "s": 0}},
    {"fit": {"basis": "interp", "ys": [350, 50], "xs": [100, 100]}},
    {"physical": False}, {"points": []}, {"gaps": [[250, 100]]},
    {"y_top": 49.5}, {"y_bottom": 401},
])
def test_malformed_lane_data_fails_closed(change):
    geometry, paint, native = fixture()
    geometry["lanes"][0].update(change)
    assert measure(geometry, paint, native) is None


@pytest.mark.parametrize("pixels", [np.full((400, 1000), np.nan), np.ones((100, 100)), np.ones((400, 1000), complex)])
def test_nonfinite_wrong_size_or_complex_source_pixels_fail_closed(pixels):
    geometry, _, native = fixture()
    assert measure(geometry, pixels, native) is None


def test_rounded_border_coordinate_is_rejected_without_indexing_error():
    geometry, paint, native = fixture()
    lane = geometry["lanes"][3]
    change_x(lane, [999.9] * len(lane["fit"]["ys"]))
    assert measure(geometry, paint, native) is None
