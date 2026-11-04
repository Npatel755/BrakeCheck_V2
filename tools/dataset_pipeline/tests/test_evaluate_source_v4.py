"""Report metrics reject missing identities and unsupported human extensions."""
import numpy as np

from tools.evaluate_source_v4 import (crossing_checks, known_failure_check,
                                      duplicate_slots, lane_rows, no_overlap_control_check,
                                      paint_supported_identity, source_boundary_retention,
                                      source_fragment_overlap, unsupported_inward_promotions)


def lane(slot, x, top, bottom):
    return {"slot": slot, "points": [[x, top, True], [x, bottom, True]]}


def geometry(*lanes):
    return {"height": 100, "width": 100, "lanes": list(lanes)}


def test_identity_scores_only_source_supported_human_rows_without_endpoint_extrapolation():
    source = np.zeros((100, 100), np.float32)
    source[20:41, 50] = 1.
    human = geometry(lane(2, 50, 20, 90))
    candidate = geometry(lane(2, 50, 20, 40))
    measured = paint_supported_identity(candidate, human, source)["2"]
    assert measured["source_supported_human_rows"] == 21
    assert measured["unsupported_human_rows"] == 50
    assert measured["source_supported_row_recall"] == 1.
    shorter = geometry(lane(2, 50, 30, 40))
    assert paint_supported_identity(shorter, human, source)["2"]["matched_rows_within_12px"] == 11
    assert np.isnan(lane_rows(shorter["lanes"][0], 100)[20])


def test_geometrically_matching_paint_in_the_wrong_slot_is_missing():
    source = np.zeros((100, 100), np.float32)
    source[20:41, 50] = 1.
    human = geometry(lane(2, 50, 20, 40))
    candidate = geometry(lane(1, 50, 20, 40))
    measured = paint_supported_identity(candidate, human, source)["2"]
    assert measured["source_supported_row_recall"] == 0.
    assert measured["sufficient_source_rows"]


def test_adjacent_boundaries_cannot_cross_just_because_both_hit_source_paint():
    candidate = geometry(lane(2, 70, 20, 90), lane(3, 30, 20, 90))
    assert crossing_checks(candidate) == [{"slots": [2, 3], "crossing_rows": 71, "y": [20, 90]}]
    assert not crossing_checks(geometry(lane(2, 30, 20, 40), lane(3, 70, 50, 90)))


def failure_record(image_id="comma10k-cf86b1a810395c5c"):
    return {"image_id": image_id, "human_slots": [1, 2, 3], "v4_slots": [1, 2, 3],
            "paint_supported_identity": {"v4": {str(slot): {
                "sufficient_source_rows": True, "source_supported_row_recall": 1.,
                "source_supported_human_y": [20, 40]} for slot in (1, 2, 3)}},
            "candidate_extents": {"1": [20., 40.], "2": [20., 40.], "3": [20., 40.]},
            "source_boundary_retention": {"2": {"1": {"observed_points": 2, "retained_fraction": 1.}}},
            "crossings": {"v4": []}, "warnings": [], "v4": {"tp": 2}}


def test_cf86_acceptance_does_not_require_an_unpainted_human_seam_extension():
    record = failure_record()
    result = known_failure_check(record)
    assert result["status"] == "passed"
    assert result["unsupported_human_extent_is_not_a_target"]
    record["candidate_extents"]["2"][1] = 90
    result = known_failure_check(record)
    assert result["status"] == "unresolved"
    assert "central_boundary_extends_beyond_verified_paint" in result["criteria_failures"]


def test_unresolved_failure_requires_specific_converter_mechanism_warning():
    record = failure_record("comma10k-bdbb9db5235de33d")
    record["warnings"] = ["low_confidence"]
    assert known_failure_check(record)["status"] == "unresolved"
    record["warnings"] = ["steep_source_continuation_uncertain"]
    result = known_failure_check(record)
    assert result["status"] == "flagged"
    assert result["flagged"]
    assert result["warnings"] == record["warnings"]


def test_matching_raster_does_not_excuse_duplicate_extra_lane_slot():
    record = failure_record("comma10k-bdbb9db5235de33d")
    record["v4"]["tp"] = 3
    record["v4_slots"] = [1, 2, 3, 4]
    result = known_failure_check(record)
    assert result["status"] == "unresolved"
    assert "candidate_slot_set_differs_from_corrected_boundary_identities" in result["criteria_failures"]


def test_physical_source_boundary_can_be_retained_while_its_lane_slot_changes():
    source = np.zeros((100, 100), np.float32)
    source[20:41, 50] = 1.
    original = geometry(lane(2, 50, 20, 40))
    candidate = geometry(lane(1, 50, 20, 40))
    retention = source_boundary_retention(original, candidate, source)
    assert retention["2"]["1"]["retained_fraction"] == 1.
    record = failure_record()
    record["source_boundary_retention"]["2"]["1"]["retained_fraction"] = .5
    assert "old_far_left_source_boundary_not_retained_in_slot_1" in known_failure_check(record)["criteria_failures"]


def test_repeated_assignment_of_one_slot_is_not_hidden_by_raster_dictionary():
    candidate = geometry(lane(2, 30, 20, 40), lane(2, 70, 20, 40))
    assert duplicate_slots(candidate) == [2]
    record = failure_record()
    record["candidate_duplicate_slots"] = [2]
    assert "candidate_assigns_multiple_boundaries_to_one_slot" in known_failure_check(record)["criteria_failures"]


def test_distant_nonoverlapping_paint_cannot_establish_a_distinct_inner_boundary():
    source = np.zeros((100, 100), np.float32)
    source[40:81, 20] = 1.
    source[10:31, 50] = 1.
    original = geometry(lane(2, 20, 40, 80), lane(None, 50, 10, 30))
    evidence = source_fragment_overlap(original, source)
    assert evidence[0]["source_painted_overlap_rows"] == 0
    assert not evidence[0]["distinct_inward_boundary_supported"]
    record = {"image_id": "comma10k-6fd08fc7c32913a8", "old_slots": [2, 3], "v4_slots": [2, 3],
              "source_fragment_overlap": evidence,
              "source_boundary_retention": {"2": {"2": {"retained_fraction": 1.}}},
              "warnings": ["ambiguous_inward_source_continuation"]}
    assert no_overlap_control_check(record)["status"] == "flagged"
    assert no_overlap_control_check(record)["human_truth_asserted"] is False
    record["v4_slots"] = [1, 2, 3]
    record["source_boundary_retention"] = {"2": {"1": {"retained_fraction": 1.}}}
    assert unsupported_inward_promotions(record)
    assert no_overlap_control_check(record)["status"] == "unresolved"


def test_distinct_parallel_paint_requires_simultaneous_source_rows_and_lateral_separation():
    source = np.zeros((100, 100), np.float32)
    source[10:81, 20] = 1.
    source[20:41, 50] = 1.
    original = geometry(lane(2, 20, 10, 80), lane(None, 50, 20, 40))
    evidence = source_fragment_overlap(original, source)[0]
    assert evidence["distinct_inward_paint_rows"] == 21
    assert evidence["distinct_inward_boundary_supported"]
    # A nearby duplicate stripe does not establish a different lane slot.
    source[:, :] = 0
    source[10:81, 20] = 1.
    source[20:41, 25] = 1.
    original["lanes"][1] = lane(None, 25, 20, 40)
    assert not source_fragment_overlap(original, source)[0]["distinct_inward_boundary_supported"]
