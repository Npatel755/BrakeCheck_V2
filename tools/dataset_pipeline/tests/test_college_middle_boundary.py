"""Saved dash connections preserve candidate evidence and real review votes."""
from copy import deepcopy
import json

from fastapi import HTTPException
import numpy as np
import pytest

from bcdata.college_middle_boundary import INDEX, connect_middle, save_approved_middle_connections
from bcdata.college_repair_review import CollegeRepairReview
from bcdata.simulation_review import SimulationCorrection
from tests.test_college_repair_review import repair_fixture
from tests.test_real_source_review import decision, raw_json, repin, sha


def dashed_geometry(slots=(1, 2, 3)):
    sections = [[[60, 10], [60, 20]], [[60, 30], [60, 40]], [[60, 50], [60, 60]]]
    return {"width": 128, "height": 72, "lanes": [
        {"slot": slots[0], "points": [[20, 10], [20, 60]]},
        {"slot": slots[1], "points": [p for section in sections for p in section], "segments": sections},
        {"slot": slots[2], "points": [[100, 10], [100, 60]]}]}


@pytest.mark.parametrize("slots", [(1, 2, 3), (2, 3, 4)])
def test_connections_keep_paint_and_outer_lines_without_extending_endpoints(slots):
    original = dashed_geometry(slots)
    before = deepcopy(original)
    connected, info = connect_middle(original)
    assert original == before
    assert connected["lanes"][0] == original["lanes"][0]
    assert connected["lanes"][2] == original["lanes"][2]
    middle = connected["lanes"][1]
    assert middle["observed_paint_segments"] == original["lanes"][1]["segments"]
    assert len(middle["segments"]) == 1
    assert middle["points"][0] == [60, 10] and middle["points"][-1] == [60, 60]
    assert [60, 25] in middle["points"] and [60, 45] in middle["points"]
    assert info["middle_slot"] == slots[1] and info["interpolated_gaps"] == 2
    assert all(section["observed_paint"] is False for section in middle["inferred_sections"])


def test_ambiguous_middle_and_overlapping_sections_are_not_connected():
    ambiguous = dashed_geometry()
    ambiguous["lanes"].append({"slot": 4, "points": [[120, 10], [120, 60]]})
    assert connect_middle(ambiguous)[1]["reason"] == "middle_boundary_identity_ambiguous"
    overlap = dashed_geometry()
    overlap["lanes"][1]["segments"][1][0][1] = 19
    assert connect_middle(overlap)[1]["reason"] == "overlapping_middle_sections"


def test_vehicle_gap_stays_open_and_other_dash_gap_is_connected():
    occlusion = np.zeros((72, 128), dtype=np.uint8)
    occlusion[25, 60] = 255
    connected, info = connect_middle(dashed_geometry(), occlusion)
    assert len(connected["lanes"][1]["segments"]) == 2
    assert [60, 25] not in connected["lanes"][1]["points"]
    assert [60, 45] in connected["lanes"][1]["points"]
    assert info["interpolated_gaps"] == 1 and len(info["blocked_gaps"]) == 1


def test_interpolation_cannot_cross_an_outer_boundary():
    geometry = dashed_geometry()
    geometry["lanes"][0]["points"] = [[20, 10], [70, 25], [20, 60]]
    assert connect_middle(geometry)[1]["reason"] == "interpolation_crosses_another_boundary"


def prepared(tmp_path, **kwargs):
    root, rows, store, original_root, original_store = repair_fixture(
        tmp_path, origin="model_proposal", held_count=5, **kwargs)
    for row in rows:
        path = root / row["geometry"]
        geometry = json.loads(path.read_bytes())
        geometry.update(dashed_geometry())
        path.write_bytes(raw_json(geometry))
        row["geometry_sha256"] = sha(path.read_bytes())
    repin(root, rows)
    return root, rows, store, original_root, original_store


def approve_samples(store):
    for number, image_id in enumerate(store.snapshot()[2]["repairs/drive-held/two-lanes"]["sample_ids"]):
        store.decide(decision(store.detail(image_id), "approve", 800 + number), "Human")


def test_saved_connections_survive_reload_without_changing_candidates_or_votes(tmp_path):
    root, rows, store, original_root, original_store = prepared(tmp_path)
    approve_samples(store)
    decisions_before = deepcopy(store.decisions())
    original_votes = deepcopy(original_store.decisions())
    before = {path: path.read_bytes() for path in [*root.rglob("*"), *original_root.rglob("*")]
              if path.is_file()}
    baseline = store.detail(rows[0]["image_id"])
    receipt = save_approved_middle_connections(store)
    assert receipt["saved_annotations"] == 5 and receipt["approved_samples_preserved"] == 3
    assert receipt["human_decisions_created"] == 0
    assert store.decisions() == decisions_before and original_store.decisions() == original_votes
    assert all(path.read_bytes() == raw for path, raw in before.items())
    reopened = CollegeRepairReview(tmp_path)
    connected = reopened.detail(rows[0]["image_id"])
    assert connected["fingerprint"] == baseline["fingerprint"]
    assert connected["decision"] == baseline["decision"]
    assert connected["boundary_completion"]["interpolated_gaps"] == 2
    assert len(connected["geometry"]["lanes"][1]["segments"]) == 1
    assert reopened.status()["counts"]["audit_passed_photos"] == 5
    assert reopened.eligible_records()["train_images"] == 0
    noop = SimulationCorrection(image_id=connected["image_id"], revision=connected["revision"],
        fingerprint=connected["fingerprint"], lanes=connected["geometry"]["lanes"], approve=True)
    reopened.correct(noop, "Human")
    assert all(path.read_bytes() == raw for path, raw in before.items())


def test_partial_group_only_connects_the_actual_approved_sample(tmp_path):
    _, rows, store, _, _ = prepared(tmp_path)
    first = store.detail(rows[0]["image_id"])
    store.decide(decision(first, "approve", 900), "Human")
    receipt = save_approved_middle_connections(store)
    assert receipt["saved_annotations"] == receipt["selected_photos"] == 1
    assert store.detail(first["image_id"]).get("boundary_completion")
    assert not store.detail(rows[2]["image_id"]).get("boundary_completion")


def test_revoking_a_group_sample_withdraws_its_group_connections(tmp_path):
    _, rows, store, _, _ = prepared(tmp_path)
    approve_samples(store)
    save_approved_middle_connections(store)
    current = store.detail(rows[0]["image_id"])
    store.decide(decision(current, "skip", 950), "Human")
    samples = store.snapshot()[2]["repairs/drive-held/two-lanes"]["sample_ids"]
    assert all(not store.detail(image_id).get("boundary_completion") for image_id in samples)


def test_edit_without_reapproval_withdraws_sibling_connections(tmp_path):
    _, rows, store, _, _ = prepared(tmp_path)
    approve_samples(store)
    save_approved_middle_connections(store)
    current = store.detail(rows[0]["image_id"])
    lanes = deepcopy(current["geometry"]["lanes"])
    lanes[0]["points"][0][0] += 1
    saved = store.correct(SimulationCorrection(image_id=current["image_id"], revision=current["revision"],
        fingerprint=current["fingerprint"], lanes=lanes, approve=False), "Human")
    assert "boundary_completion" not in saved["geometry"]
    assert saved["geometry"]["boundary_completion_history"][0]["superseded_by_manual_edit"] is True
    assert not store.detail(rows[2]["image_id"]).get("boundary_completion")


def test_redrawing_connected_middle_does_not_keep_outdated_completion_claim(tmp_path):
    root, rows, store, _, _ = prepared(tmp_path)
    approve_samples(store)
    save_approved_middle_connections(store)
    current = store.detail(rows[0]["image_id"])
    lanes = deepcopy(current["geometry"]["lanes"])
    lanes[1] = {"slot": 2, "points": [[61, 10], [62, 60]]}
    saved = store.correct(SimulationCorrection(image_id=current["image_id"], revision=current["revision"],
        fingerprint=current["fingerprint"], lanes=lanes, approve=True), "Human")
    assert "boundary_completion" not in saved["geometry"]
    assert "inferred_sections" not in saved["geometry"]["lanes"][1]
    assert saved["geometry"]["boundary_completion_history"][0]["superseded_by_manual_edit"] is True
    correction = store.corrections()[current["image_id"]]
    persisted = json.loads((root / correction["geometry_path"]).read_bytes())
    assert "boundary_completion" not in persisted


def test_changed_saved_annotation_is_a_conflict(tmp_path):
    root, rows, store, _, _ = prepared(tmp_path)
    approve_samples(store)
    save_approved_middle_connections(store)
    index = json.loads((root / INDEX).read_bytes())
    path = root / index["records"][rows[0]["image_id"]]["geometry_path"]
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(HTTPException) as error:
        store.detail(rows[0]["image_id"])
    assert error.value.status_code == 409


def test_already_open_editor_can_save_two_point_segmented_replacement(tmp_path):
    root, rows, store, _, _ = prepared(tmp_path)
    current = store.detail(rows[0]["image_id"])
    original = (root / rows[0]["geometry"]).read_bytes()
    lanes = deepcopy(current["geometry"]["lanes"])
    lanes[1] = {"slot": 2, "points": [[61, 10], [62, 60]]}
    body = SimulationCorrection(image_id=current["image_id"], revision=current["revision"],
        fingerprint=current["fingerprint"], lanes=lanes, approve=True)
    saved = store.correct(body, "Human")
    assert saved["geometry"]["lanes"][1]["segments"] == [[[61, 10], [62, 60]]]
    assert saved["decision"]["action"] == "approve"
    assert (root / rows[0]["geometry"]).read_bytes() == original
    with pytest.raises(HTTPException) as error:
        store.correct(body, "Human")
    assert error.value.status_code == 409


def test_segmented_redraw_keeps_problem_group_gate(tmp_path):
    _, rows, store, _, _ = prepared(tmp_path, audit_mode="needs_repair")
    current = store.detail(rows[0]["image_id"])
    lanes = deepcopy(current["geometry"]["lanes"])
    lanes[1] = {"slot": 2, "points": [[61, 10], [62, 60]]}
    body = SimulationCorrection(image_id=current["image_id"], revision=current["revision"],
        fingerprint=current["fingerprint"], lanes=lanes, resolved_qc_reasons=["short_native_polyline"], approve=True)
    saved = store.correct(body, "Human")
    assert saved["decision"] is None
    assert "repair_review_required" in saved["qc_reasons"]
    assert not store.decisions()
