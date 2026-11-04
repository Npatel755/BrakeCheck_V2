from copy import deepcopy

import numpy as np

from bcdata.source_tip_evidence import measure
from bcdata.source_target_evidence import measure as complete_evidence


def fixture():
    width, height, top, bottom = 1920, 400, 50, 350
    ys = np.arange(top, bottom + 1)
    xs = 600 + 7 * np.minimum(ys - top, 4) + np.maximum(ys - top - 4, 0)
    paint = np.zeros((height, width), np.float32)
    for y, x in zip(ys, xs):
        paint[y, x - 5:x + 6] = 1
    lane = {"slot": 2, "physical": True, "y_top": top, "y_bottom": bottom,
            "fit": {"basis": "interp", "ys": ys.tolist(), "xs": xs.tolist()},
            "points": [[int(xs[index]), int(y), True] for index, y in enumerate(ys)][::-1]}
    geometry = {"width": width, "height": height, "horizon_y": 0,
                "valid_bottom": height - 1, "lanes": [lane], "source_converter": {"warnings": []}}
    evidence = complete_evidence(geometry, paint)
    assert evidence["eligible_for_fresh_audit"]
    return geometry, paint, evidence


def test_complete_source_supported_distal_tip_is_evidence_without_geometry_changes():
    geometry, paint, evidence = fixture()
    before, pixels = deepcopy(geometry), paint.copy()
    result = measure(geometry, paint, evidence)
    assert result["eligible"] and result["verified_slots"] == [2]
    assert result["geometry_changed"] is False and result["requires_fresh_human_audit"]
    assert result["evidence"][0]["whole_component_single_run"]
    assert geometry == before
    np.testing.assert_array_equal(paint, pixels)


def test_incomplete_native_evidence_cannot_use_tip_exception():
    geometry, paint, evidence = fixture()
    for key, value in (("eligible_for_fresh_audit", False), ("represented_fraction", .97),
                       ("observed_points_on_paint", .97), ("crossings", [[2, 3]]),
                       ("unhandled_converter_warnings", ["ambiguous_slot"]),
                       ("version", "unverified-other-evidence")):
        changed = {**evidence, key: value}
        assert measure(geometry, paint, changed) is None


def test_no_transverse_pairs_means_no_exception_evidence():
    geometry, paint, evidence = fixture()
    geometry["lanes"][0]["points"] = [[600, 350, True], [600, 50, True]]
    assert measure(geometry, paint, evidence) is None


def test_foreground_horizontal_error_remains_blocked():
    geometry, paint, evidence = fixture()
    geometry["lanes"][0]["points"].insert(50, [1200, 300, True])
    assert measure(geometry, paint, evidence) is None


def test_one_valid_tip_does_not_excuse_another_invalid_boundary():
    geometry, paint, evidence = fixture()
    second = deepcopy(geometry["lanes"][0])
    second["slot"] = 3
    second["points"] = [[1000, 200, True], [1100, 201, True]]
    geometry["lanes"].append(second)
    assert measure(geometry, paint, evidence) is None


def test_hidden_or_inferred_tip_point_cannot_supply_evidence():
    geometry, paint, evidence = fixture()
    geometry["lanes"][0]["points"][-1][2] = False
    assert measure(geometry, paint, evidence) is None


def test_source_gap_cannot_be_bridged_by_nearby_paint():
    geometry, paint, evidence = fixture()
    paint[60] = 0
    assert measure(geometry, paint, evidence) is None


def test_detached_tip_component_is_not_longitudinal_continuation():
    geometry, paint, evidence = fixture()
    paint[55:58] = 0
    assert measure(geometry, paint, evidence) is None


def test_a_boundary_ending_inside_an_existing_component_is_not_a_distal_tip():
    geometry, paint, evidence = fixture()
    paint[40:50, 595:606] = 1
    assert measure(geometry, paint, evidence) is None


def test_connected_fork_with_two_runs_on_a_row_remains_blocked():
    geometry, paint, evidence = fixture()
    # A side branch is connected at row 100 and continues parallel farther down.
    centre = int(geometry["lanes"][0]["fit"]["xs"][50])
    paint[100, centre:centre + 80] = 1
    paint[101:150, centre + 74:centre + 80] = 1
    assert measure(geometry, paint, evidence) is None


def test_attached_wide_stop_bar_at_the_endpoint_remains_blocked():
    geometry, paint, evidence = fixture()
    paint[50:53, 550:700] = 1
    assert measure(geometry, paint, evidence) is None


def test_narrow_attached_t_bar_rejected_even_below_absolute_width_cap():
    geometry, paint, evidence = fixture()
    # Keep the upper slope supported, but join a 31px T head to a 5px stem.
    paint[:] = 0
    lane = geometry["lanes"][0]
    for y, x in zip(lane["fit"]["ys"], lane["fit"]["xs"]):
        half = 15 if y <= 54 else 2
        paint[y, x - half:x + half + 1] = 1
    assert measure(geometry, paint, evidence) is None


def test_points_along_the_edge_of_a_wide_source_stroke_are_not_centreline_evidence():
    geometry, paint, evidence = fixture()
    paint[:] = 0
    lane = geometry["lanes"][0]
    for y, x in zip(lane["fit"]["ys"], lane["fit"]["xs"]):
        paint[y, x:x + 20] = 1
    assert measure(geometry, paint, evidence) is None


def test_slope_violation_beyond_eight_native_rows_is_not_a_tip():
    geometry, paint, evidence = fixture()
    geometry["lanes"][0]["points"].insert(-15, [900, 64, True])
    assert measure(geometry, paint, evidence) is None


def test_malformed_geometry_fails_closed():
    geometry, paint, evidence = fixture()
    geometry["lanes"][0]["fit"] = {"basis": "unknown"}
    assert measure(geometry, paint, evidence) is None


def test_malformed_or_nonfinite_evidence_fails_closed():
    geometry, paint, evidence = fixture()
    for value in (float("nan"), float("inf"), "1.0", True, None, 1.01):
        assert measure(geometry, paint, {**evidence, "represented_fraction": value}) is None
    assert measure(geometry, paint, {**evidence, "crossings": np.array([1, 2])}) is None


def test_actual_tip_points_cannot_use_a_different_correct_fit_as_support():
    geometry, paint, evidence = fixture()
    geometry["lanes"][0]["points"][-1][0] += 100
    assert measure(geometry, paint, evidence) is None


def test_boolean_slot_is_not_a_lane_identifier():
    geometry, paint, evidence = fixture()
    geometry["lanes"][0]["slot"] = True
    assert measure(geometry, paint, evidence) is None


def test_rounded_border_coordinate_fails_closed_without_indexing_exception():
    geometry, paint, evidence = fixture()
    geometry["lanes"][0]["fit"]["xs"] = [geometry["width"] - .1] * len(geometry["lanes"][0]["fit"]["xs"])
    assert measure(geometry, paint, evidence) is None
