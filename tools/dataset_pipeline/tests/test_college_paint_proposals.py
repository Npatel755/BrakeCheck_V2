"""Observed proposal boundaries cannot silently become filled training truth."""
from copy import deepcopy

import numpy as np
import pytest

from bcdata.college_paint_proposals import (
    EXTRACTION, _inside, observed_geometry, select_pilot,
    audit_disposition, _write_compact_evidence,
    single_valued_sections,
)


def _params():
    params = deepcopy(EXTRACTION)
    params.horizon_y = .05
    params.min_segment_rows = 3
    return params


def test_keeps_actual_mask_centres_and_explicit_dash_gaps():
    probability = np.zeros((240, 320), np.float32)
    probability[70:140, 95:100] = .81
    probability[155:230, 95:100] = .81
    proposal = observed_geometry(probability, params=_params())
    assert proposal["candidate_only"] is True
    assert proposal["training_eligible"] is False
    assert proposal["human_complete_annotation"] is False
    assert len(proposal["lanes"]) == 1
    lane = proposal["lanes"][0]
    assert len(lane["segments"]) == 2
    assert "fit" not in lane
    assert lane["points"][0][1] == 70
    assert lane["points"][-1][1] == 229
    assert all(probability[y, round(x)] >= .35 for x, y, _ in lane["points"])
    assert not any(140 <= y < 155 for _, y, _ in lane["points"])
    assert {tuple(p) for s in lane["segments"] for p in s} == {tuple(p) for p in lane["points"]}


def test_vehicle_exclusion_splits_observations_without_filling_behind_car():
    probability = np.zeros((240, 320), np.float32)
    probability[40:230, 220:225] = .9
    vehicles = np.zeros_like(probability)
    vehicles[110:140, 210:235] = .9
    proposal = observed_geometry(probability, occlusion_probability=vehicles, params=_params())
    assert proposal["lanes"]
    points = [p for lane in proposal["lanes"] for p in lane["points"]]
    assert not any(110 <= y < 140 for _, y, _ in points)
    assert all(vehicles[y, round(x)] < .35 for x, y, _ in points)


def test_no_road_or_no_mask_is_not_a_human_no_paint_annotation():
    probability = np.ones((100, 200), np.float32)
    proposal = observed_geometry(probability, road_probability=np.zeros_like(probability), params=_params())
    assert proposal["lanes"] == []
    assert proposal["human_complete_annotation"] is False
    assert "no_visible_lane_paint" not in proposal
    assert proposal["origin"] == "model_proposal"


def test_rejects_nonfinite_and_mismatched_evidence():
    with pytest.raises(ValueError):
        observed_geometry(np.full((20, 20), np.nan))
    with pytest.raises(ValueError):
        observed_geometry(np.zeros((20, 20)), road_probability=np.zeros((10, 20)))
    with pytest.raises(ValueError):
        observed_geometry(np.zeros((20, 20)), occlusion_probability=np.full((20, 20), np.inf))


def test_pilot_preserves_completed_diagnostics_and_samples_temporal_range():
    rows = [{"image_id": str(i), "source_member": f"sequence/frame_{i}.jpg"} for i in range(101)]
    diagnostics = [{"image_id": "done", "source_member": "sequence/frame_999.jpg"}]
    selected = select_pilot(rows[::-1], diagnostics, limit=4)
    assert selected[0] == diagnostics[0]
    assert [row["image_id"] for row in selected[1:]] == ["0", "50", "100"]
    assert len({row["image_id"] for row in selected}) == len(selected)
    assert len(select_pilot(rows, [], limit=500)) == len(rows)
    with pytest.raises(ValueError):
        select_pilot(rows, diagnostics, limit=0)


def test_evidence_paths_cannot_leave_batch(tmp_path):
    assert _inside(tmp_path, "evidence/one.npz").is_relative_to(tmp_path)
    with pytest.raises(ValueError):
        _inside(tmp_path, "../elsewhere.npz")


def test_observed_slot_reversal_excludes_branch_from_ready_cohort():
    geometry = {"width":320,"lanes":[
        {"slot":2,"association_x_ref":60,"points":[[60,100,True],[70,120,True]]},
        {"slot":3,"association_x_ref":200,"points":[[200,100,True],[230,120,True]]},
        {"slot":4,"association_x_ref":290,"points":[[190,100,True],[240,120,True]]}],
        "stats":{"long_unassigned_chains":0}}
    result = audit_disposition(geometry)
    assert result["repair_disposition"] == "held"
    assert "observed_slot_order_reversal_or_crossing" in result["repair_qc_reasons"]


def test_compact_evidence_preserves_exact_threshold_mask(tmp_path):
    params = _params()
    lane = np.array([[.34999,.35001,.9],[.8,.7,.6]],np.float32)
    road = np.ones_like(lane)
    occlusion = np.zeros_like(lane)
    occlusion[1,1] = .9
    path = tmp_path/"evidence.npz"
    receipt = _write_compact_evidence(path,lane,road,occlusion,params)
    with np.load(path) as evidence:
        actual = np.unpackbits(evidence["accepted_mask_bits"],axis=1)[:,:lane.shape[1]].astype(bool)
        np.testing.assert_array_equal(actual,(lane>=params.threshold)&(occlusion<params.threshold))
        assert abs(evidence["lane_u8"]/255-lane).max() <= .5/255+1e-7
    assert "quantization error" in receipt["probability_storage"]


def test_overlapping_sections_have_one_observed_point_per_row_and_stay_held():
    probability = np.zeros((20,30),np.float32)
    probability[:,10]=.8
    probability[:,15]=.9
    first=[[10,y,True] for y in range(2,10)]
    second=[[15,y,True] for y in range(8,15)]
    sections, conflicts = single_valued_sections([first,second],probability)
    points=[p for section in sections for p in section]
    assert conflicts==2
    assert len({p[1] for p in points})==len(points)
    assert all(a[1]<b[1] for a,b in zip(points,points[1:]))
    assert sections[0][-1]==[10,7,True]
    assert sections[1][0]==[15,8,True]
    geometry={"width":30,"lanes":[{"slot":2,"association_x_ref":10,"points":points,
               "overlapping_section_rows":conflicts}],"stats":{}}
    assert "overlapping_observed_section_association" in audit_disposition(geometry)["repair_qc_reasons"]
    geometry["lanes"] = []
    geometry["representation_repair"] = {"overlapping_rows_per_slot": {"2": conflicts}}
    assert "overlapping_observed_section_association" in audit_disposition(geometry)["repair_qc_reasons"]
