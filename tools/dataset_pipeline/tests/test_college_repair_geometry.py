from copy import deepcopy
import hashlib

import pytest

from bcdata.college_repair_geometry import (
    clip_observed_edge, observed_sections, observed_slot_assignments, propose_candidate,
)
from bcdata.import_college_station import CONVERTER, parse_lines


def fixture(native, width=100, height=100):
    geometry, warnings = parse_lines(native, width, height)
    row = {"image_id": "college-0000000000000001", "qc_reasons": warnings,
           "source_label_sha256": hashlib.sha256(native).hexdigest(), "converter": CONVERTER,
           "group_key": "test", "sequence_id": "test"}
    return row, geometry


def test_clipping_intersects_the_native_edge_instead_of_clamping_endpoint():
    a, b = (20.0, 10.0), (120.0, 60.0)
    result = clip_observed_edge(a, b, 100, 100)
    end = result["points"][1]
    assert 99.9 < end[0] < 100
    assert end[1] == pytest.approx(50)
    for point, parameter in zip(result["points"], result["native_edge_parameters"]):
        assert point == pytest.approx([a[i] + parameter * (b[i] - a[i]) for i in (0, 1)])
        assert 0 <= parameter <= 1
    assert clip_observed_edge((-3, 0), (-2, 60), 100, 100) is None


def test_monotonic_sections_keep_both_source_branches_and_reveal_overlap():
    result = observed_sections([(10, 90), (40, 20), (12, 90)], 100, 100)
    assert result["sections"] == [[[10, 90], [40, 20]], [[40, 20], [12, 90]]]
    assert result["overlapping_row_support"] is True
    assert len(result["observed_edges"]) == 2


def test_horizontal_source_edges_remain_diagnostics_without_becoming_longitudinal_paint():
    result = observed_sections([(10, 20), (12, 20), (30, 90)], 100, 100)
    assert result["horizontal_edges"] == [[[10, 20], [12, 20]]]
    assert result["sections"] == [[[12, 20], [30, 90]]]


def test_clipping_does_not_bridge_a_native_excursion_outside_the_photo():
    result = observed_sections([(10, 20), (110, 50), (10, 80)], 100, 100)
    assert len(result["sections"]) == 2
    assert result["sections"][0][-1] != result["sections"][1][0]


def test_slots_cannot_be_inferred_from_disconnected_or_unanchored_traces():
    lanes = [{"source_line": 1, "points": [[10, 10], [20, 20]]},
             {"source_line": 2, "points": [[80, 70], [90, 90]]}]
    assert observed_slot_assignments(lanes, {1: 2})["assignments"] == []
    lanes[1]["points"] = [[80, 10], [90, 20]]
    assignments = observed_slot_assignments(lanes, {1: 2})["assignments"]
    assert len(assignments) == 2  # Right could be the ego or outer boundary.


def test_slots_require_consistent_order_on_every_piecewise_observed_row():
    lanes = [{"source_line": 1, "points": [[10, 10], [80, 50], [10, 90]]},
             {"source_line": 2, "points": [[50, 10], [50, 90]]}]
    result = observed_slot_assignments(lanes, {1: 2})
    assert result["assignments"] == []
    assert result["reason"] == "crossing_or_touching_observed_traces"


def test_short_source_trace_stays_short_and_unverified_in_review_candidate():
    native = b"20 60 15 70\n70 55 85 90\n"
    row, geometry = fixture(native)
    originals = deepcopy((row, geometry))
    result = propose_candidate(row, native, geometry)
    assert result["candidate_status"] == "ready_for_audit"
    assert "short_native_polyline" in result["original_warnings"]
    assert "short_native_polyline" in result["remaining_warning_reasons"]
    assert result["resolved_warning_reasons"] == []
    assert result["geometry"] == geometry
    assert (row, geometry) == originals
    assert "audit_resolved_qc_reasons" not in result["geometry"]
    assert result["diagnostics"]["visible_paint_verified"] is False


def test_far_field_native_traces_do_not_gain_training_span():
    native = b"20 20 10 30\n70 20 90 30\n"
    row, geometry = fixture(native)
    result = propose_candidate(row, native, geometry)
    assert result["candidate_status"] == "no_training_span"
    assert result["geometry"] == geometry
    assert result["diagnostics"]["extrapolated_points_created"] == 0


def test_ambiguous_native_branch_is_not_silently_selected_or_resolved():
    native = b"10 90 30 20 12 90\n70 20 90 90\n"
    row, geometry = fixture(native)
    result = propose_candidate(row, native, geometry)
    assert result["candidate_status"] == "held"
    assert "native_polyline_not_single_valued" in result["remaining_warning_reasons"]
    assert result["diagnostics"]["native_traces"][0]["overlapping_row_support"] is True
    assert result["geometry"] == geometry


def test_clipped_omitted_boundary_requires_unique_slot_from_actual_overlap():
    native = b"15 20 10 90\n30 20 25 90\n70 20 75 90\n90 20 120 90\n"
    row, geometry = fixture(native)
    assert geometry["omitted_native_source_lines"] == [4]
    before = deepcopy(geometry)
    result = propose_candidate(row, native, geometry)
    assert result["resolved_warning_reasons"] == ["native_points_outside_image"]
    restored = next(lane for lane in result["geometry"]["lanes"] if lane["source_line"] == 4)
    assert restored["slot"] == 4
    assert "native_points_outside_image" in restored["flags"]
    assert result["original_warnings"] == row["qc_reasons"]
    assert result["geometry"]["omitted_native_source_lines"] == []
    assert geometry == before
    assert result["candidate_status"] == "ready_for_audit"


def test_unanchored_clipped_boundary_stays_held_with_source_evidence():
    native = b"20 20 10 40\n70 20 120 90\n"
    row, geometry = fixture(native)
    result = propose_candidate(row, native, geometry)
    assert result["resolved_warning_reasons"] == []
    assert "native_points_outside_image" in result["diagnostics"]["blockers"]
    assert result["geometry"] == geometry
    assert result["diagnostics"]["native_traces"][1]["had_outside_points"] is True


def test_native_coverage_gaps_are_never_synthesized_or_cleared():
    native = b"20 20 10 90\n70 20 90 90\n"
    row, geometry = fixture(native)
    row["qc_reasons"].append("native_coverage_incomplete")
    result = propose_candidate(row, native, geometry)
    assert result["candidate_status"] == "held"
    assert "native_coverage_incomplete" in result["remaining_warning_reasons"]
    assert result["geometry"] == geometry
    assert result["diagnostics"]["missing_source_paint_inferred"] is False


def test_repair_refuses_mismatched_native_bytes_and_source_geometry():
    native = b"20 20 10 90\n70 20 90 90\n"
    row, geometry = fixture(native)
    with pytest.raises(ValueError, match="digest"):
        propose_candidate(row, b"20 20 10 80\n", geometry)
    geometry["lanes"][0]["points"][0][0] += 1
    with pytest.raises(ValueError, match="replay"):
        propose_candidate(row, native, geometry)


def test_existing_extrapolated_slot_contradiction_holds_without_forcing_a_fix():
    # Like College TX21 college-c88b56067bdd4c46: the short trace extrapolates
    # left of the long ego-left boundary, but is actually to its right over their
    # shared observed rows. Neither paint identity nor a slot swap is inferred.
    native = b"65 30 75 90\n85 30 95 90\n60 30 55 35\n40 32 20 90\n"
    row, geometry = fixture(native)
    slots = {lane["source_line"]: lane["slot"] for lane in geometry["lanes"]}
    assert slots[3] == 1 and slots[4] == 2
    result = propose_candidate(row, native, geometry)
    assert result["diagnostics"]["has_observed_training_span"] is True
    assert result["candidate_status"] == "held"
    assert "observed_slot_order_contradiction" in result["diagnostics"]["blockers"]
    assert result["diagnostics"]["retained_slot_evidence"]["assignments"] == []
    assert result["resolved_warning_reasons"] == []
    assert result["geometry"] == geometry
