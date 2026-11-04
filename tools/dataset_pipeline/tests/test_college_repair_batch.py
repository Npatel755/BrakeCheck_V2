"""A repair batch freezes fresh representative audits without rewriting history."""
from copy import deepcopy
import json

from fastapi import HTTPException
import pytest

from bcdata.college_group_clearance import FILENAME, prepare
from bcdata.college_repair_batch import observed_slot_order_ok, stage_batch
from bcdata.college_repair_review import COLLECTION, CollegeRepairReview
from bcdata.simulation_review import SimulationCorrection
from tests.test_college_repair_review import original_fixture
from tests.test_real_source_review import decision, raw_json


def candidates_for(root, held, *, origin="source_repair"):
    candidates = []
    for row in held:
        geometry = json.loads((root / row["geometry"]).read_bytes())
        geometry.update(origin=origin, version="test-source-repair:v1")
        candidates.append({"image_id": row["image_id"], "geometry": geometry,
                           "candidate_origin": origin, "repair_family": "test-visible-span",
                           "repair_description": "Check the separately converted visible span.",
                           "provenance": {"recipe": "test-repair:v1"}})
    return candidates


def test_reversed_observed_slots_are_rejected_before_publication(tmp_path):
    original_root, held, _ = original_fixture(tmp_path)
    candidates = candidates_for(original_root, held)
    candidates[0]["geometry"]["lanes"].append({"slot": 3, "points": [[5, 20], [5, 70]]})
    with pytest.raises(ValueError, match="slots cross or reverse"):
        stage_batch(tmp_path, candidates, original_held_count=3)
    assert not (tmp_path / "experiments" / COLLECTION / "manifest.jsonl").exists()


def test_slot_order_gate_does_not_connect_unobserved_gaps():
    geometry = {"lanes": [
        {"slot": 2, "points": [[70, 20], [65, 30], [20, 60], [15, 70]],
         "segments": [[[70, 20], [65, 30]], [[20, 60], [15, 70]]]},
        {"slot": 3, "points": [[50, 40], [45, 50]]},
    ]}
    assert observed_slot_order_ok(geometry)


def test_staging_freezes_fresh_audits_and_preserves_original_votes_and_assets(tmp_path):
    original_root, held, original = original_fixture(tmp_path)
    before = {path: path.read_bytes() for path in original_root.rglob("*") if path.is_file()}
    decisions_before, corrections_before = deepcopy(original.decisions()), deepcopy(original.corrections())
    receipt = stage_batch(tmp_path, candidates_for(original_root, held), original_held_count=3)
    store = CollegeRepairReview(tmp_path)
    status = store.status()
    assert receipt["staged_photos"] == 3 and receipt["groups"] == 1 and receipt["samples"] == 3
    assert receipt["human_decisions_created"] == 0 and receipt["training_started"] is False
    assert receipt["training_held"] is True and receipt["original_reviews_unchanged"] is True
    assert status["counts"]["pending"] == 3 and status["counts"]["approved"] == 0
    assert status["counts"]["eligible_train_candidates"] == 0
    assert not store.decisions() and not store.corrections()
    assert not (store.root / "review.sqlite").exists()
    _, rows, _, _, _ = store.snapshot(fresh=True)
    for held_row in held:
        row = rows[held_row["image_id"]]
        assert row["original_split"] == "hold" and row["split"] == "train"
        assert row["original_qc_reasons"] == held_row["qc_reasons"]
        assert row["original_geometry_sha256"] == held_row["geometry_sha256"]
        detail = store.detail(row["image_id"])
        assert detail["candidate_only"] is True and detail["training_eligible"] is False
        assert detail["original_geometry"]["image_id"] == row["image_id"]
    assert original.decisions() == decisions_before and original.corrections() == corrections_before
    assert all(path.read_bytes() == raw for path, raw in before.items())


def test_staging_keeps_model_proposal_provenance_and_never_claims_human_annotation(tmp_path):
    original_root, held, _ = original_fixture(tmp_path)
    candidates = candidates_for(original_root, held, origin="model_proposal")
    candidates[0]["geometry"].update(human_complete_annotation=True,
                                      audit_resolved_qc_reasons=["short_native_polyline"])
    receipt = stage_batch(tmp_path, candidates, original_held_count=3)
    assert receipt["candidate_origins"] == {"model_proposal": 3}
    store = CollegeRepairReview(tmp_path)
    detail = store.detail(held[0]["image_id"])
    assert detail["candidate_origin"] == detail["label_origin"] == "model_proposal"
    assert detail["geometry"]["human_complete_annotation"] is False
    assert "audit_resolved_qc_reasons" not in detail["geometry"]
    assert store.eligible_records()["records"] == []


def test_staging_requires_the_current_held_count(tmp_path):
    original_root, held, _ = original_fixture(tmp_path)
    with pytest.raises(ValueError, match="held count changed"):
        stage_batch(tmp_path, candidates_for(original_root, held), original_held_count=4)
    assert not (tmp_path / "experiments" / COLLECTION / "manifest.jsonl").exists()


@pytest.mark.parametrize("protected", ["manifest.jsonl", "review.sqlite"])
def test_staging_never_overwrites_an_existing_audit_or_review_store(tmp_path, protected):
    original_root, held, _ = original_fixture(tmp_path)
    root = tmp_path / "experiments" / COLLECTION
    root.mkdir(parents=True)
    path = root / protected
    path.write_bytes(b"existing audit history must remain unchanged")
    with pytest.raises(ValueError, match="already published"):
        stage_batch(tmp_path, candidates_for(original_root, held), original_held_count=3)
    assert path.read_bytes() == b"existing audit history must remain unchanged"
    assert set(root.iterdir()) == {path}


@pytest.mark.parametrize("history", ["skip", "correction"])
def test_candidates_with_personal_history_cannot_be_staged_again(tmp_path, history):
    original_root, held, original = original_fixture(tmp_path)
    current = original.detail(held[0]["image_id"])
    if history == "skip":
        original.decide(decision(current, "skip", 500), "Original Human")
    else:
        lanes = deepcopy(current["geometry"]["lanes"])
        lanes[0]["points"][0][0] += 1
        original.correct(SimulationCorrection(image_id=current["image_id"], revision=current["revision"],
            fingerprint=current["fingerprint"], lanes=lanes), "Original Human")
    before = deepcopy(original.decisions()), deepcopy(original.corrections())
    with pytest.raises(ValueError, match="personal review history"):
        stage_batch(tmp_path, candidates_for(original_root, held), original_held_count=3)
    assert (original.decisions(), original.corrections()) == before
    assert not (tmp_path / "experiments" / COLLECTION / "manifest.jsonl").exists()


def test_temporal_groups_have_at_most_one_hundred_and_three_spread_out_samples(tmp_path):
    original_root, held, _ = original_fixture(tmp_path, held_count=203)
    candidates = list(reversed(candidates_for(original_root, held)))
    receipt = stage_batch(tmp_path, candidates, original_held_count=203)
    assert receipt["staged_photos"] == 203 and receipt["groups"] == 3 and receipt["samples"] == 9
    _, rows, groups, _, _ = CollegeRepairReview(tmp_path).snapshot(fresh=True)
    ordered = sorted(groups.values(), key=lambda group: group["group_key"])
    assert [group["count"] for group in ordered] == [100, 100, 3]
    for group, offset, size in zip(ordered, [0, 100, 200], [100, 100, 3]):
        expected = [held[offset]["image_id"], held[offset + size // 2]["image_id"],
                    held[offset + size - 1]["image_id"]]
        assert group["sample_ids"] == expected
        assert sum(row["group_key"] == group["group_key"] for row in rows.values()) == size


def test_source_repairs_and_model_proposals_get_separate_audit_groups(tmp_path):
    original_root, held, _ = original_fixture(tmp_path, held_count=6)
    candidates = candidates_for(original_root, held[:3]) + candidates_for(original_root, held[3:], origin="model_proposal")
    receipt = stage_batch(tmp_path, candidates, original_held_count=6)
    assert receipt["groups"] == 2 and receipt["samples"] == 6
    _, rows, groups, _, _ = CollegeRepairReview(tmp_path).snapshot(fresh=True)
    assert {group["candidate_origin"] for group in groups.values()} == {"source_repair", "model_proposal"}
    for group in groups.values():
        assert {rows[image_id]["candidate_origin"] for image_id in group["sample_ids"]} == {group["candidate_origin"]}


def test_already_cleared_unchanged_warning_targets_cannot_be_staged_again(tmp_path):
    original_root, held, original = original_fixture(tmp_path, held_count=6)
    _, _, groups, _, _ = original.snapshot()
    for image_id in groups[held[0]["group_key"]]["sample_ids"]:
        current = original.detail(image_id)
        original.correct(SimulationCorrection(image_id=image_id, revision=current["revision"],
            fingerprint=current["fingerprint"], lanes=current["geometry"]["lanes"],
            resolved_qc_reasons=["short_native_polyline"], approve=True), "Original Human")
    (original_root / FILENAME).write_bytes(raw_json(prepare(original)))
    assert original.status()["counts"]["held_uncleared"] == 0
    untouched_target = held[1]
    assert untouched_target["image_id"] not in original.decisions()
    with pytest.raises(ValueError, match="cleared"):
        stage_batch(tmp_path, candidates_for(original_root, [untouched_target]), original_held_count=0)
    assert not (tmp_path / "experiments" / COLLECTION / "manifest.jsonl").exists()


@pytest.mark.parametrize("bad", ["empty_lanes", "wrong_dimensions", "duplicate", "wrong_photo"])
def test_unusable_or_misbound_candidates_do_not_publish_a_manifest(tmp_path, bad):
    original_root, held, _ = original_fixture(tmp_path)
    candidates = candidates_for(original_root, held)
    if bad == "empty_lanes":
        candidates[0]["geometry"]["lanes"] = []
    elif bad == "wrong_dimensions":
        candidates[0]["geometry"]["width"] += 1
    elif bad == "duplicate":
        candidates.append(deepcopy(candidates[0]))
    else:
        candidates[0]["geometry"]["image_id"] = held[1]["image_id"]
    with pytest.raises(ValueError):
        stage_batch(tmp_path, candidates, original_held_count=3)
    assert not (tmp_path / "experiments" / COLLECTION / "manifest.jsonl").exists()


@pytest.mark.parametrize("problem", ["no_training_span", "slot_order"])
def test_ready_and_problem_candidates_stage_as_separate_approval_and_diagnostic_groups(tmp_path, problem):
    original_root, held, _ = original_fixture(tmp_path, held_count=6)
    candidates = candidates_for(original_root, held)
    for candidate in candidates[3:]:
        candidate.update(audit_mode="needs_repair", blockers=["short_native_polyline"])
        candidate["geometry"]["lanes"] = (
            [{"slot": 2, "points": [[20, 10], [19, 11]]}] if problem == "no_training_span" else [
                {"slot": 2, "points": [[90, 20], [90, 70]]},
                {"slot": 3, "points": [[30, 20], [30, 70]]}])
    receipt = stage_batch(tmp_path, candidates, original_held_count=6)
    assert receipt["staged_photos"] == 6 and receipt["groups"] == 2 and receipt["samples"] == 6
    store = CollegeRepairReview(tmp_path)
    counts = store.status()["counts"]
    assert counts["pending"] == counts["pending_problem_samples"] == 3
    assert counts["ready_candidate_photos"] == counts["problem_photos"] == 3
    assert counts["groups_needing_repair"] == 1
    normal = store.queue(limit=12)["items"]
    diagnostic = store.queue(status="needs_repair", limit=12)["items"]
    assert len(normal) == len(diagnostic) == 3
    assert {item["image_id"] for item in normal} == {row["image_id"] for row in held[:3]}
    assert {item["image_id"] for item in diagnostic} == {row["image_id"] for row in held[3:]}
    assert all(item["audit_mode"] == "ready" and not item["qc_reasons"] for item in normal)
    assert all(item["audit_mode"] == "needs_repair" and item["qc_reasons"] for item in diagnostic)
    assert len({item["group_key"] for item in normal + diagnostic}) == 2
    for index, item in enumerate(normal):
        store.decide(decision(item, "approve", index + 700), "Repair Human")
    with pytest.raises(HTTPException) as error:
        store.decide(decision(diagnostic[0], "approve", 750), "Repair Human")
    assert error.value.status_code == 422
    store.decide(decision(diagnostic[0], "exclude", 751), "Repair Human")
    assert store.queue()["items"] == []
    assert len(store.queue(status="needs_repair", limit=12)["items"]) == 2
    status = store.status()
    assert status["counts"]["audit_passed_photos"] == 3
    assert status["counts"]["pending"] == 0 and status["counts"]["pending_problem_samples"] == 2
    assert status["counts"]["eligible_train_candidates"] == 0
    assert store.eligible_records()["records"] == []
