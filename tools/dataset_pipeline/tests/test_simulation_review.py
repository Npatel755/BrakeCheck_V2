"""Simulation audits stay isolated and never promote unverified projections."""
import hashlib
import json
from pathlib import Path
import sqlite3

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from bcdata.simulation_review import SimulationDecision, SimulationReview, create_simulation_router


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def manifest(fixture):
    path = fixture["collection"] / "manifest.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in fixture["rows"]), encoding="utf-8")
    return sha(path)


def files(root):
    return {str(p.relative_to(root)): sha(p) for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def data(tmp_path):
    collection = tmp_path / "experiments/simulated_tulane"
    collection.mkdir(parents=True)
    rows, groups = [], []
    for group_index, (split, count) in enumerate([("train", 4), ("val", 3)]):
        group = {"group_key": f"{split}:scenario-{group_index}", "split": split, "count": count, "sample_ids": []}
        for frame in range(count):
            path = collection / f"images/{split}/{group_index}-{frame}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (1280, 720), (group_index * 80 + frame * 9, 30, 40)).save(path)
            image_sha = sha(path)
            image_id = "tulane-" + image_sha[:16]
            geometry = {"image_id": image_id, "width": 1280, "height": 720,
                        "version": "tulane-converter:v1", "origin": "simulator_projected_gt",
                        "lanes": [{"slot": 2, "points": [[450, 700], [550, 400]],
                                   "fit": {"basis": "interp", "ys": [400, 700], "xs": [550, 450]},
                                   "y_top": 400, "y_bottom": 700, "physical": True}]}
            geo_path = collection / f"geometry/{image_id}.json"
            write(geo_path, geometry)
            source_path = collection / f"labels/records/{image_id}.json"
            write(source_path, {"lanes": [[450, 550]], "h_samples": [700, 400], "raw_file": str(path.relative_to(collection))})
            row = {"image_id": image_id, "split": split, "session_id": f"{split}:scenario-{group_index}",
                   "source": "carlane_tulane", "synthetic": True, "pool": "commercial",
                   "image": path.relative_to(collection).as_posix(),
                   "geometry": geo_path.relative_to(collection).as_posix(),
                   "source_label": source_path.relative_to(collection).as_posix(),
                   "source_sha256": image_sha, "geometry_sha256": sha(geo_path),
                   "source_label_sha256": sha(source_path), "group_key": group["group_key"], "qc_reasons": [],
                   "annotation_policy_risks": ["projected_geometry", "occlusion"]}
            rows.append(row)
            if frame < 3:
                group["sample_ids"].append(image_id)
        groups.append(group)
    write(collection / "import.json", {"schema": "tulane_simulation_import:v1", "source": "carlane_tulane",
                                       "domain": "synthetic", "license": "Apache-2.0", "state": "awaiting_audit"})
    fixture = {"root": tmp_path, "collection": collection, "rows": rows, "groups": groups}
    manifest(fixture)
    write(collection / "audits.json", {"schema": "tulane_simulation_audits:v1", "groups": groups})
    # An existing production review is present to prove every route ignores it.
    con = sqlite3.connect(tmp_path / "metadata.sqlite")
    con.execute("create table reviews(image_id text,decision text)")
    con.execute("insert into reviews values('cleared-original','corrected')")
    con.commit(); con.close()
    write(tmp_path / "exports/ufld_v2/BrakeCheck_Reviewed/READY.json", {"train_images": 13086})
    store = SimulationReview(tmp_path)
    fixture["store"] = store
    app = FastAPI()

    def session(request: Request):
        if request.headers.get("x-paired") != "yes":
            raise HTTPException(401, "Pair first")
        return {"reviewer": "Paired reviewer"}

    app.include_router(create_simulation_router(tmp_path, session))
    with TestClient(app) as client:
        fixture.update(client=client, headers={"x-paired": "yes"})
        yield fixture


def body(data, image_id, action="approve", *, event=None, revision=None):
    detail = data["store"].detail(image_id)
    return SimulationDecision(image_id=image_id, action=action,
                              event_id=event or f"event-{image_id}-{detail['revision']}",
                              revision=detail["revision"] if revision is None else revision,
                              fingerprint=detail["fingerprint"])


def decide(data, image_id, action="approve", **kwargs):
    return data["store"].decide(body(data, image_id, action, **kwargs), "Human reviewer")


def test_empty_status_does_not_create_a_registry(tmp_path):
    before = files(tmp_path)
    status = SimulationReview(tmp_path).status()
    assert not status["audits_ready"] and status["counts"]["imported"] == 0
    assert not status["auto_promote"]
    assert files(tmp_path) == before


def test_native_splits_and_three_samples_per_group(data):
    before = files(data["root"])
    result = data["store"].status()
    assert result["counts"]["imported"] == 7
    assert result["counts"]["train"] == 4 and result["counts"]["val"] == 3
    assert result["counts"]["samples"] == result["counts"]["pending"] == 6
    assert result["counts"]["eligible_train_candidates"] == 0
    assert all(group["state"] == "awaiting_audit" for group in result["groups"])
    assert files(data["root"]) == before
    sample = data["store"].queue(limit=12)["items"][0]
    assert sample["label_origin"] == "simulator_projected_gt" and sample["annotation_policy_risks"]


def test_every_api_requires_existing_authenticated_session(data):
    image_id = data["groups"][0]["sample_ids"][0]
    for path in ("", "/queue", f"/samples/{image_id}", f"/samples/{image_id}/photo"):
        assert data["client"].get("/api/studio/simulation" + path).status_code == 401
    payload = body(data, image_id).model_dump()
    assert data["client"].post("/api/studio/simulation/decision", json=payload).status_code == 401
    response = data["client"].get("/api/studio/simulation", headers=data["headers"])
    assert response.status_code == 200 and response.json()["audits_ready"]
    response = data["client"].get(f"/api/studio/simulation/samples/{image_id}/photo", headers=data["headers"])
    assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
    assert response.content == (data["collection"] / data["rows"][0]["image"]).read_bytes()


def test_audits_never_expose_or_accept_an_unsampled_manifest_row(data):
    image_id = data["rows"][3]["image_id"]
    for method in (data["store"].detail, data["store"].photo):
        with pytest.raises(HTTPException) as error:
            method(image_id)
        assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        data["store"].decide(SimulationDecision(image_id=image_id, action="approve", event_id="unlisted-event",
                                               revision=0, fingerprint="a" * 64), "Reviewer")
    assert error.value.status_code == 404
    assert not (data["collection"] / "review.sqlite").exists()


@pytest.mark.parametrize("field", ["image", "geometry", "source_label"])
@pytest.mark.parametrize("unsafe", ["../metadata.sqlite", "C:/Users/Nick/private.json", "images\\secret.jpg", "/tmp/secret.jpg"])
def test_manifest_assets_are_confined(data, field, unsafe):
    data["rows"][0][field] = unsafe
    manifest(data)
    with pytest.raises(HTTPException) as error:
        data["store"].status()
    assert error.value.status_code == 409


def test_preparation_status_cannot_offer_partial_audits(data):
    write(data["collection"] / "import.json", {"schema": "tulane_simulation_import:v1", "source": "carlane_tulane",
          "domain": "synthetic", "license": "Apache-2.0", "state": "converting"})
    assert not data["store"].status()["audits_ready"]
    assert data["store"].queue()["items"] == []


@pytest.mark.parametrize("wrong", ["missing_samples", "wrong_split", "wrong_count", "unlisted_sample"])
def test_audit_manifest_requires_complete_cohort_membership(data, wrong):
    group = data["groups"][0]
    if wrong == "missing_samples": group["sample_ids"].pop()
    elif wrong == "wrong_split": group["split"] = "val"
    elif wrong == "wrong_count": group["count"] = 6
    else: group["sample_ids"][0] = "tulane-" + "0" * 16
    write(data["collection"] / "audits.json", {"schema": "tulane_simulation_audits:v1", "groups": data["groups"]})
    with pytest.raises(HTTPException) as error:
        data["store"].status()
    assert error.value.status_code == 409


def test_group_is_eligible_only_after_every_sample_and_val_never_enters_train(data):
    production = files(data["root"] / "exports")
    production_db = sha(data["root"] / "metadata.sqlite")
    group = data["groups"][0]
    for image_id in group["sample_ids"][:2]: decide(data, image_id)
    assert data["store"].eligible_records()["train_images"] == 0
    decide(data, group["sample_ids"][2])
    result = data["store"].eligible_records()
    assert result["train_images"] == 4 and result["val_images_used"] == 0
    assert {row["image_id"] for row in result["records"]} == {row["image_id"] for row in data["rows"] if row["split"] == "train"}
    assert all(row["geometry_sha256"] and row["manifest_sha256"] and row["source_label_sha256"] for row in result["records"])
    for image_id in data["groups"][1]["sample_ids"]: decide(data, image_id)
    assert data["store"].eligible_records()["train_images"] == 4
    assert data["store"].status()["counts"]["groups_eligible"] == 2
    assert sha(data["root"] / "metadata.sqlite") == production_db
    assert files(data["root"] / "exports") == production


def test_excluding_one_sample_blocks_cohort_and_skip_holds_it(data):
    samples = data["groups"][0]["sample_ids"]
    decide(data, samples[0]); decide(data, samples[1], "exclude"); decide(data, samples[2])
    result = data["store"].status()
    assert result["counts"]["groups_blocked"] == 1 and data["store"].eligible_records()["train_images"] == 0
    decide(data, samples[1], "skip")
    assert data["store"].status()["counts"]["groups_blocked"] == 0
    assert data["store"].eligible_records()["train_images"] == 0
    queue = data["store"].queue(status="skip")
    assert queue["items"][0]["image_id"] == samples[1]
    decide(data, samples[1])
    assert data["store"].eligible_records()["train_images"] == 4


def test_idempotent_events_and_optimistic_revision(data):
    image_id = data["groups"][0]["sample_ids"][0]
    original = body(data, image_id, event="a-stable-event")
    result = data["store"].decide(original, "First reviewer")
    assert result["revision"] == 1 and not result["idempotent"]
    assert data["store"].decide(original, "First reviewer")["idempotent"]
    with pytest.raises(HTTPException) as error:
        data["store"].decide(original, "Different reviewer")
    assert error.value.status_code == 409
    stale = body(data, image_id, "exclude", event="another-event", revision=0)
    with pytest.raises(HTTPException) as error:
        data["store"].decide(stale, "Second reviewer")
    assert error.value.status_code == 409
    assert data["store"].detail(image_id)["decision"]["action"] == "approve"


@pytest.mark.parametrize("field", ["image", "geometry", "source_label"])
def test_changed_source_bytes_reject_decision_before_database_write(data, field):
    row = data["rows"][0]
    payload = body(data, row["image_id"])
    path = data["collection"] / row[field]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(HTTPException) as error:
        data["store"].decide(payload, "Reviewer")
    assert error.value.status_code == 409
    assert not (data["collection"] / "review.sqlite").exists()


def test_replaced_manifest_does_not_reuse_previous_approvals(data):
    image_id = data["groups"][0]["sample_ids"][0]
    payload = body(data, image_id)
    data["rows"][0]["session_id"] = "changed-session"
    manifest(data)
    with pytest.raises(HTTPException) as error:
        data["store"].decide(payload, "Reviewer")
    assert error.value.status_code == 409
    decide(data, image_id)
    data["rows"][0]["session_id"] = "changed-again"
    manifest(data)
    detail = data["store"].detail(image_id)
    assert detail["previous_decision_stale"] and detail["revision"] == 1
    assert detail["decision"] is None
    assert data["store"].status()["counts"]["approved"] == 0


def test_changed_approved_sample_bytes_hold_previously_eligible_group(data):
    for image_id in data["groups"][0]["sample_ids"]: decide(data, image_id)
    assert data["store"].eligible_records()["train_images"] == 4
    path = data["collection"] / data["rows"][0]["source_label"]
    path.write_bytes(b"changed original annotation")
    assert data["store"].status()["counts"]["eligible_train_candidates"] == 0
    assert data["store"].eligible_records()["train_images"] == 0


def test_unsampled_training_assets_are_reverified_before_build(data):
    for image_id in data["groups"][0]["sample_ids"]: decide(data, image_id)
    path = data["collection"] / data["rows"][3]["image"]
    path.write_bytes(b"changed unsampled source")
    with pytest.raises(HTTPException) as error:
        data["store"].eligible_records()
    assert error.value.status_code == 409


def test_qc_flagged_sample_cannot_be_approved_and_flagged_extras_stay_out(data):
    sample = data["rows"][0]
    sample["qc_reasons"] = ["invalid_slot_order"]
    manifest(data)
    with pytest.raises(HTTPException) as error:
        decide(data, sample["image_id"])
    assert error.value.status_code == 422
    sample["qc_reasons"] = []
    data["rows"][3]["qc_reasons"] = ["invalid_coordinates"]
    manifest(data)
    for image_id in data["groups"][0]["sample_ids"]: decide(data, image_id)
    result = data["store"].eligible_records()
    assert result["train_images"] == 3
    assert data["rows"][3]["image_id"] not in {row["image_id"] for row in result["records"]}


def test_api_trusts_authenticated_reviewer_and_has_no_training_mutation_route(data):
    image_id = data["groups"][0]["sample_ids"][0]
    payload = body(data, image_id).model_dump()
    payload["reviewer"] = "Untrusted body name"
    response = data["client"].post("/api/studio/simulation/decision", json=payload, headers=data["headers"])
    assert response.status_code == 200
    assert data["store"].detail(image_id)["decision"]["reviewer"] == "Paired reviewer"
    assert data["client"].post("/api/studio/simulation/train", headers=data["headers"]).status_code == 404
    assert data["client"].get("/api/studio/simulation/queue?split=test", headers=data["headers"]).status_code == 400


def test_sample_correction_isolated_pending_then_exported_after_approval(data):
    samples = data["groups"][0]["sample_ids"]
    for image_id in samples:
        decide(data, image_id)
    item = data["store"].detail(samples[0])
    original = data["collection"] / data["rows"][0]["geometry"]
    original_sha = sha(original)
    payload = {"image_id": samples[0], "revision": item["revision"],
               "fingerprint": item["fingerprint"],
               "lanes": [{"slot": 2, "points": [[425, 700], [525, 400]]}]}
    response = data["client"].post("/api/studio/simulation/correction", json=payload, headers=data["headers"])
    assert response.status_code == 200, response.text
    corrected = response.json()
    assert corrected["geometry"]["lanes"][0]["points"][0] == [425, 700]
    assert corrected["source_geometry"]["lanes"][0]["points"][0] == [450, 700]
    assert corrected["correction"]["reviewer"] == "Paired reviewer"
    assert corrected["decision"] is None and corrected["previous_decision_stale"]
    assert data["store"].status()["counts"]["groups_eligible"] == 0
    assert data["store"].queue(status="pending")["items"][0]["image_id"] == samples[0]
    assert data["store"].eligible_records()["train_images"] == 0
    decide(data, samples[0])
    exported = {row["image_id"]: row for row in data["store"].eligible_records()["records"]}
    row = exported[samples[0]]
    assert row["geometry_origin"] == "human_audit_correction"
    assert row["geometry_sha256"] == sha(data["collection"] / row["geometry"])
    assert row["geometry"] != data["rows"][0]["geometry"]
    assert exported[samples[1]]["geometry"] == data["rows"][1]["geometry"]
    assert sha(original) == original_sha


def test_sample_correction_rejects_out_of_image_and_stale_fingerprint(data):
    image_id = data["groups"][0]["sample_ids"][0]
    item = data["store"].detail(image_id)
    payload = {"image_id": image_id, "revision": item["revision"],
               "fingerprint": item["fingerprint"],
               "lanes": [{"slot": 2, "points": [[450, 700], [1280, 400]]}]}
    response = data["client"].post("/api/studio/simulation/correction", json=payload, headers=data["headers"])
    assert response.status_code == 422
    assert data["client"].post("/api/studio/simulation/correction", json=payload).status_code == 401
    payload["lanes"][0]["points"][1] = [525, 400]
    assert data["client"].post("/api/studio/simulation/correction", json=payload, headers=data["headers"]).status_code == 200
    assert data["client"].post("/api/studio/simulation/correction", json=payload, headers=data["headers"]).status_code == 409


@pytest.mark.parametrize("segments", [None, [None], [[]], [[[1280, 400]]], [[[525, 400, True]]], [[[float("nan"), 400]]]])
def test_sample_correction_rejects_invalid_visible_sections(data, segments):
    image_id = data["groups"][0]["sample_ids"][0]
    item = data["store"].detail(image_id)
    from bcdata.simulation_review import SimulationCorrection
    payload = SimulationCorrection(image_id=image_id, revision=0, fingerprint=item["fingerprint"],
        lanes=[{"slot": 2, "points": [[425, 700], [525, 400]], "segments": segments}])
    with pytest.raises(HTTPException) as error:
        data["store"].correct(payload, "Reviewer")
    assert error.value.status_code == 422
    assert not (data["collection"] / "review.sqlite").exists()


def test_correction_can_atomically_approve_a_skipped_sample(data):
    image_id = data["groups"][0]["sample_ids"][0]
    decide(data, image_id, "skip")
    item = data["store"].detail(image_id)
    response = data["client"].post("/api/studio/simulation/correction", headers=data["headers"], json={
        "image_id": image_id, "revision": item["revision"], "fingerprint": item["fingerprint"],
        "lanes": [{"slot": 2, "points": [[425, 700], [525, 400]]}], "approve": True})
    assert response.status_code == 200, response.text
    corrected = response.json()
    assert corrected["decision"]["action"] == "approve"
    assert corrected["decision"]["fingerprint"] == corrected["fingerprint"]
    assert corrected["revision"] == 2
    assert corrected["decision"]["reviewer"] == "Paired reviewer"
    assert not data["store"].queue(status="skip")["items"]
    assert image_id not in {item["image_id"] for item in data["store"].queue(limit=12)["items"]}


def test_failed_autoapproval_rolls_back_the_saved_correction(data, monkeypatch):
    from bcdata.simulation_review import SimulationCorrection
    store = data["store"]
    image_id = data["groups"][0]["sample_ids"][0]
    item = store.detail(image_id)
    def fail(*args):
        raise RuntimeError("fixture approval write failed")
    monkeypatch.setattr(store, "_record_decision", fail)
    with pytest.raises(RuntimeError, match="fixture approval"):
        store.correct(SimulationCorrection(image_id=image_id, revision=0, fingerprint=item["fingerprint"],
            lanes=[{"slot": 2, "points": [[425, 700], [525, 400]]}], approve=True), "Reviewer")
    assert store.corrections() == {} and store.decisions() == {}
    assert store.detail(image_id)["fingerprint"] == item["fingerprint"]


def test_unchanged_geometry_can_be_saved_and_approved(data):
    image_id = data["groups"][0]["sample_ids"][0]
    item = data["store"].detail(image_id)
    payload = {"image_id": image_id, "revision": 0, "fingerprint": item["fingerprint"],
               "lanes": item["geometry"]["lanes"], "approve": True}
    response = data["client"].post("/api/studio/simulation/correction", json=payload, headers=data["headers"])
    assert response.status_code == 200, response.text
    corrected = response.json()
    assert corrected["decision"]["action"] == "approve" and corrected["revision"] == 1
    assert corrected["geometry"]["lanes"] == item["geometry"]["lanes"]
    assert data["client"].post("/api/studio/simulation/correction", json=payload, headers=data["headers"]).status_code == 409
    assert data["store"].detail(image_id)["revision"] == 1
