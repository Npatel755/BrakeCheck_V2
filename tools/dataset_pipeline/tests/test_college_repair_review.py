"""Separate College repair audits must never confer source-label truth."""
from copy import deepcopy
import json
import shutil

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from bcdata.college_repair_review import API, CollegeRepairReview, create_college_repair_router
from bcdata.simulation_review import SimulationCorrection
from tests.test_real_source_review import decision, fixture, raw_json, repin, sha


def original_fixture(tmp_path, *, held_count=3):
    original_root, original_rows, original_store = fixture(tmp_path)
    held = []
    for index in range(held_count):
        template = original_rows[index % len(original_rows)]
        row = deepcopy(template)
        image_id = f"college-{index + 100:016x}"
        row.update(image_id=image_id, split="hold", intended_split="train",
                   session_id="drive/held", group_key="hold/drive-held/two-lanes",
                   qc_reasons=["short_native_polyline"], source_member=f"drive/frame{index}.jpg")
        for key in ("image", "geometry", "source_label"):
            row[key] = template[key].replace(template["image_id"], image_id)
            (original_root / row[key]).write_bytes((original_root / template[key]).read_bytes())
        geometry = json.loads((original_root / row["geometry"]).read_bytes())
        geometry["image_id"] = image_id
        (original_root / row["geometry"]).write_bytes(raw_json(geometry))
        row["geometry_sha256"] = sha((original_root / row["geometry"]).read_bytes())
        held.append(row)
    audits = json.loads((original_root / "audits.json").read_bytes())
    sample_indices = [0, len(held) // 2, len(held) - 1] if len(held) >= 3 else list(range(len(held)))
    audits["groups"].append({"group_key": held[0]["group_key"], "split": "hold",
                             "count": len(held), "sample_ids": [held[index]["image_id"] for index in sample_indices]})
    (original_root / "audits.json").write_bytes(raw_json(audits))
    repin(original_root, original_rows + held)
    for index, row in enumerate(original_rows):
        original_store.decide(decision(original_store.detail(row["image_id"]), "approve", index), "Original Human")
    return original_root, held, original_store


def repair_fixture(tmp_path, *, origin="source_repair", held_count=3, audit_mode="ready"):
    original_root, held, original_store = original_fixture(tmp_path, held_count=held_count)
    _, _, _, original_manifest_sha, original_audits_sha = original_store.snapshot(fresh=True)
    original_import_sha = sha((original_root / "import.json").read_bytes())
    snapshot = {"manifest_sha256": original_manifest_sha, "audits_sha256": original_audits_sha,
                "import_sha256": original_import_sha}
    root = tmp_path / "experiments" / "college_held_repairs-20261022"
    root.mkdir(parents=True)
    rows = []
    for original in held:
        row = deepcopy(original)
        row.update(split="train", original_split="hold",
                   qc_reasons=[] if audit_mode == "ready" else ["short_native_polyline"],
                   audit_mode=audit_mode,
                   original_qc_reasons=original["qc_reasons"],
                   original_geometry=f"original_geometry/{original['image_id']}.json",
                   original_geometry_sha256=original["geometry_sha256"],
                   candidate_origin=origin, group_key="repairs/drive-held/two-lanes",
                   source_snapshot={**snapshot, "record_fingerprint": original_store.fingerprint(
                       original, original_manifest_sha, original_audits_sha)})
        for key in ("image", "geometry", "source_label"):
            destination = root / row[key]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original_root / row[key], destination)
        original_geometry_path = root / row["original_geometry"]
        original_geometry_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original_root / original["geometry"], original_geometry_path)
        geometry = json.loads((root / row["geometry"]).read_bytes())
        if audit_mode == "needs_repair":
            geometry["lanes"] = [{"slot": 2, "points": [[20, 10], [19, 11]]}]
        geometry.update(origin=origin, version="college-test-repair:v1", candidate_only=True,
                        training_eligible=False)
        (root / row["geometry"]).write_bytes(raw_json(geometry))
        row["geometry_sha256"] = sha((root / row["geometry"]).read_bytes())
        rows.append(row)
    manifest_raw = b"".join(raw_json(row) for row in rows)
    sample_indices = [0, len(rows) // 2, len(rows) - 1] if len(rows) >= 3 else list(range(len(rows)))
    audits_raw = raw_json({"schema": "real_lane_audits:v1", "manifest_sha256": sha(manifest_raw),
        "groups": [{"group_key": rows[0]["group_key"], "split": "train", "count": len(rows),
                    "sample_ids": [rows[index]["image_id"] for index in sample_indices]}]})
    metadata = {"schema": "real_lane_import:v1", "source": "college_station_lane_marking",
                "license": "CC0-1.0", "state": "awaiting_audit", "auto_promote": False,
                "manifest_sha256": sha(manifest_raw), "audits_sha256": sha(audits_raw),
                "source_snapshot": snapshot, "collection": "college_held_repairs-20261022",
                "candidate_only": True, "training_eligible": False}
    (root / "manifest.jsonl").write_bytes(manifest_raw)
    (root / "audits.json").write_bytes(audits_raw)
    (root / "import.json").write_bytes(raw_json(metadata))
    return root, rows, CollegeRepairReview(tmp_path), original_root, original_store


def approve_all(store, rows):
    for index, row in enumerate(rows):
        store.decide(decision(store.detail(row["image_id"]), "approve", index + 100), "Repair Human")


def test_separate_audit_starts_fresh_and_preserves_existing_approvals(tmp_path):
    _, rows, store, original_root, original_store = repair_fixture(tmp_path)
    before = {path: path.read_bytes() for path in original_root.rglob("*") if path.is_file()}
    original_decisions = deepcopy(original_store.decisions())
    assert len(original_decisions) == 3
    assert store.status()["counts"]["pending"] == 3
    assert not store.decisions()
    approve_all(store, rows)
    status = store.status()
    assert status["counts"]["approved"] == 3
    assert status["counts"]["audit_passed_photos"] == 3
    assert status["counts"]["eligible_train_candidates"] == 0
    assert store.eligible_records()["records"] == []
    assert store.eligible_records()["train_images"] == 0
    assert original_store.decisions() == original_decisions
    assert all(path.read_bytes() == raw for path, raw in before.items())


def test_model_proposal_approvals_remain_outside_source_training(tmp_path):
    _, rows, store, _, _ = repair_fixture(tmp_path, origin="model_proposal")
    approve_all(store, rows)
    status = store.status()
    assert status["counts"]["approved"] == 3
    assert status["counts"]["eligible_train_candidates"] == 0
    assert store.eligible_records()["records"] == []
    detail = store.detail(rows[0]["image_id"])
    assert detail["candidate_origin"] == "model_proposal"
    assert detail["auto_promote"] is False


def test_changing_a_sample_blocks_the_whole_repaired_group(tmp_path):
    _, rows, store, _, _ = repair_fixture(tmp_path)
    approve_all(store, rows)
    current = store.detail(rows[0]["image_id"])
    lanes = deepcopy(current["geometry"]["lanes"])
    lanes[0]["points"][0][0] += 1
    result = store.correct(SimulationCorrection(
        image_id=current["image_id"], revision=current["revision"], fingerprint=current["fingerprint"],
        lanes=lanes, approve=True), "Repair Human")
    assert result["decision"]["action"] == "approve"
    status = store.status()
    assert status["groups"][0]["state"] == "needs_repair"
    assert status["groups"][0]["eligible_train_images"] == 0
    assert status["counts"]["audit_passed_photos"] == 0
    assert store.eligible_records()["records"] == []


@pytest.mark.parametrize("action", ["skip", "exclude"])
def test_incomplete_or_excluded_audit_does_not_pass_a_group(tmp_path, action):
    _, rows, store, _, _ = repair_fixture(tmp_path)
    for index, row in enumerate(rows):
        store.decide(decision(store.detail(row["image_id"]), action if index == 2 else "approve", index + 100), "Repair Human")
    status = store.status()
    assert status["counts"]["audit_passed_photos"] == 0
    assert status["groups"][0]["state"] == ("blocked" if action == "exclude" else "awaiting_audit")
    assert store.eligible_records()["records"] == []


@pytest.mark.parametrize("asset", ["geometry", "source_label", "image"])
def test_changed_candidate_bytes_cannot_receive_a_stale_approval(tmp_path, asset):
    root, rows, store, _, _ = repair_fixture(tmp_path)
    current = store.detail(rows[0]["image_id"])
    stale = decision(current, "approve", 200)
    path = root / rows[0][asset]
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(HTTPException) as error:
        store.decide(stale, "Repair Human")
    assert error.value.status_code == 409
    assert not store.decisions()


def test_original_source_replacement_invalidates_repair_binding(tmp_path):
    _, rows, store, original_root, _ = repair_fixture(tmp_path)
    store.detail(rows[0]["image_id"])
    path = original_root / rows[0]["source_label"]
    path.write_bytes(path.read_bytes() + b"changed original label")
    with pytest.raises(HTTPException) as error:
        store.detail(rows[0]["image_id"])
    assert error.value.status_code == 409


def test_stale_fingerprint_cannot_overwrite_a_saved_sample_correction(tmp_path):
    _, rows, store, _, _ = repair_fixture(tmp_path)
    current = store.detail(rows[0]["image_id"])
    stale = decision(current, "approve", 250)
    lanes = deepcopy(current["geometry"]["lanes"])
    lanes[0]["points"][0][0] += 1
    store.correct(SimulationCorrection(image_id=current["image_id"], revision=current["revision"],
        fingerprint=current["fingerprint"], lanes=lanes), "Repair Human")
    with pytest.raises(HTTPException) as error:
        store.decide(stale, "Repair Human")
    assert error.value.status_code == 409


def test_repair_router_requires_a_paired_reviewer(tmp_path):
    app = FastAPI()
    app.include_router(create_college_repair_router(tmp_path, lambda: None))
    with TestClient(app) as client:
        for path in ("", "/queue", "/eligible", "/samples/college-0000000000000064",
                     "/samples/college-0000000000000064/photo"):
            assert client.get(API + path).status_code == 401
        assert client.post(API + "/decision", json={}).status_code == 401
        assert client.post(API + "/correction", json={}).status_code == 401


def test_repair_router_exposes_fresh_candidate_photo_and_no_training_records(tmp_path):
    _, rows, _, _, _ = repair_fixture(tmp_path)
    app = FastAPI()
    app.include_router(create_college_repair_router(tmp_path, lambda: {"reviewer": "Paired Human"}))
    with TestClient(app) as client:
        queued = client.get(API + "/queue").json()["items"][0]
        assert queued["image_id"] in {row["image_id"] for row in rows}
        assert queued["image_url"].startswith(API + "/samples/")
        assert queued["candidate_only"] is True and queued["training_eligible"] is False
        photo = client.get(queued["image_url"])
        assert photo.status_code == 200
        assert photo.headers["cache-control"] == "private, no-store"
        assert client.get(API + "/eligible").json()["records"] == []


def test_repair_group_cannot_exceed_one_hundred_photos(tmp_path):
    _, _, store, _, _ = repair_fixture(tmp_path, held_count=101)
    with pytest.raises(HTTPException) as error:
        store.snapshot(fresh=True)
    assert error.value.status_code == 409


@pytest.mark.parametrize("split", ["val", "test", "hold"])
def test_candidate_audits_reject_other_staged_splits(tmp_path, split):
    root, rows, store, _, _ = repair_fixture(tmp_path)
    for row in rows:
        row["split"] = split
    repin(root, rows)
    with pytest.raises(HTTPException) as error:
        store.snapshot(fresh=True)
    assert error.value.status_code == 409


@pytest.mark.parametrize("field,value", [("intended_split", "val"), ("session_id", "different/drive")])
def test_candidate_must_keep_original_train_intent_and_recording_session(tmp_path, field, value):
    root, rows, store, _, _ = repair_fixture(tmp_path)
    rows[0][field] = value
    repin(root, rows)
    with pytest.raises(HTTPException) as error:
        store.detail(rows[0]["image_id"])
    assert error.value.status_code == 409


@pytest.mark.parametrize("missing", ["copied_geometry", "original_label", "original_import"])
def test_missing_original_evidence_returns_a_review_conflict(tmp_path, missing):
    root, rows, store, original_root, _ = repair_fixture(tmp_path)
    store.detail(rows[0]["image_id"])
    paths = {"copied_geometry": root / rows[0]["original_geometry"],
             "original_label": original_root / rows[0]["source_label"],
             "original_import": original_root / "import.json"}
    paths[missing].unlink()
    with pytest.raises(HTTPException) as error:
        store.detail(rows[0]["image_id"])
    assert error.value.status_code == 409


def test_problem_samples_are_separate_from_the_normal_pending_audit_queue(tmp_path):
    _, rows, store, _, _ = repair_fixture(tmp_path, audit_mode="needs_repair")
    counts = store.status()["counts"]
    assert counts["pending"] == 0 and counts["pending_problem_samples"] == 3
    assert counts["ready_candidate_photos"] == 0 and counts["problem_photos"] == 3
    assert counts["groups_needing_repair"] == 1
    assert store.queue()["items"] == []
    problems = store.queue(status="needs_repair", limit=12)["items"]
    assert {item["image_id"] for item in problems} == {row["image_id"] for row in rows}
    assert all(item["audit_mode"] == "needs_repair" for item in problems)
    assert store.eligible_records()["records"] == []


@pytest.mark.parametrize("action", ["skip", "exclude"])
def test_problem_samples_can_be_triaged_but_cannot_be_approved(tmp_path, action):
    _, rows, store, _, _ = repair_fixture(tmp_path, audit_mode="needs_repair")
    current = store.detail(rows[0]["image_id"])
    with pytest.raises(HTTPException) as error:
        store.decide(decision(current, "approve", 600), "Repair Human")
    assert error.value.status_code == 422
    assert not store.decisions()
    store.decide(decision(current, action, 601), "Repair Human")
    problems = store.queue(status="needs_repair", limit=12)["items"]
    assert len(problems) == 2 and current["image_id"] not in {item["image_id"] for item in problems}
    counts = store.status()["counts"]
    assert counts["pending"] == 0 and counts["pending_problem_samples"] == 2
    assert counts["audit_passed_photos"] == counts["eligible_train_candidates"] == 0
    assert store.eligible_records()["records"] == []


def test_resolving_a_problem_sample_warning_cannot_remove_its_repair_gate(tmp_path):
    _, rows, store, _, _ = repair_fixture(tmp_path, audit_mode="needs_repair")
    current = store.detail(rows[0]["image_id"])
    body = dict(image_id=current["image_id"], revision=current["revision"],
                fingerprint=current["fingerprint"], lanes=[{"slot": 2, "points": [[20, 20], [10, 70]]}],
                resolved_qc_reasons=["short_native_polyline"])
    try:
        saved = store.correct(SimulationCorrection(**body, approve=True), "Repair Human")
    except HTTPException as error:
        assert error.status_code == 422
        saved = store.correct(SimulationCorrection(**body, approve=False), "Repair Human")
    assert saved["decision"] is None, "Saving a diagnostic edit must never create an approval"
    assert "short_native_polyline" in saved["resolved_qc_reasons"]
    assert "short_native_polyline" not in saved["qc_reasons"]
    assert "repair_review_required" in saved["qc_reasons"]
    assert "repair_review_required" in saved["source_qc_reasons"]
    assert saved["audit_mode"] == "needs_repair"
    assert saved["geometry"]["lanes"][0]["points"] == [[20, 20], [10, 70]]
    with pytest.raises(HTTPException) as error:
        store.decide(decision(saved, "approve", 650), "Repair Human")
    assert error.value.status_code == 422
    assert not store.decisions()
    status = store.status()
    assert status["groups"][0]["state"] == "needs_repair"
    assert status["counts"]["audit_passed_photos"] == 0
    assert status["counts"]["eligible_train_candidates"] == 0
    assert store.queue()["items"] == []
    assert saved["image_id"] in {item["image_id"] for item in store.queue(status="needs_repair", limit=12)["items"]}
