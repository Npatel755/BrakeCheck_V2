"""Explicit repair promotion freezes approved geometry without inventing votes."""
from copy import deepcopy
import json

from fastapi import HTTPException
from PIL import Image
import pytest

from bcdata.bootstrap import atomic_json
from bcdata.college_middle_boundary import save_approved_middle_connections
from bcdata.college_repair_review import CollegeRepairReview
from bcdata.college_repair_training import CollegeRepairTraining, REPAIR_SOURCE
from bcdata.converted_merge import build_converted_merge, load_converted_merge, plan_converted_merge
from bcdata.simulation_review import SimulationCorrection
from bcdata.teachers.base import file_sha256
from bcdata.ufld_baseline import NativeDataset
from tests.test_college_middle_boundary import dashed_geometry
from tests.test_college_repair_review import repair_fixture
from tests.test_real_source_review import decision, raw_json, repin, sha
from tests.test_ufld_experiment import _rgb, experiment_fixture


def prepared(base, *, audit_mode="ready"):
    root, rows, _, original_root, original_store = repair_fixture(
        base.temp, origin="model_proposal", held_count=5, audit_mode=audit_mode)
    # The stock fixture repeats three source photos. Give these five identities
    # distinct real bytes before binding either manifest, so deduplication does
    # not obscure which approvals release which photographs.
    original_rows = [json.loads(line) for line in (original_root / "manifest.jsonl").read_text().splitlines()]
    originals = {row["image_id"]: row for row in original_rows}
    for number, row in enumerate(rows):
        photo = original_root / originals[row["image_id"]]["image"]
        Image.fromarray(_rgb(number + 40)[:72, :128]).save(photo, quality=95)
        (root / row["image"]).write_bytes(photo.read_bytes())
        originals[row["image_id"]]["source_sha256"] = row["source_sha256"] = file_sha256(photo)
        geometry_path = root / row["geometry"]
        geometry = json.loads(geometry_path.read_bytes())
        geometry.update(dashed_geometry())
        geometry_path.write_bytes(raw_json(geometry))
        row["geometry_sha256"] = sha(geometry_path.read_bytes())
    repin(original_root, original_rows)
    _, _, _, manifest_sha, audits_sha = original_store.snapshot(fresh=True)
    snapshot = {"manifest_sha256": manifest_sha, "audits_sha256": audits_sha,
                "import_sha256": file_sha256(original_root / "import.json")}
    for row in rows:
        row["source_snapshot"] = {**snapshot, "record_fingerprint": original_store.fingerprint(
            originals[row["image_id"]], manifest_sha, audits_sha)}
    metadata = json.loads((root / "import.json").read_bytes())
    metadata["source_snapshot"] = snapshot
    atomic_json(root / "import.json", metadata)
    repin(root, rows)
    atomic_json(base.dataset / "split_inventory.json", base.inventory)
    return root, rows, CollegeRepairReview(base.temp), original_root, original_store


def approve_samples(store):
    samples = next(iter(store.snapshot()[2].values()))["sample_ids"]
    for number, image_id in enumerate(samples):
        store.decide(decision(store.detail(image_id), "approve", 1800 + number), "Repair Human")
    return samples


def build(base, **kwargs):
    return build_converted_merge(base.temp, base.base, base.output, sources=[REPAIR_SOURCE], **kwargs)


def test_approved_model_repairs_and_saved_connections_enter_converted_only_with_evidence(experiment_fixture):
    f = experiment_fixture
    root, rows, store, original_root, original_store = prepared(f)
    sample_ids = approve_samples(store)
    save_approved_middle_connections(store)
    votes, original_votes = deepcopy(store.decisions()), deepcopy(original_store.decisions())
    before = {path: path.read_bytes() for folder in (root, original_root, f.dataset)
              for path in folder.rglob("*") if path.is_file()}
    assert CollegeRepairTraining(f.temp).eligible_records()["train_images"] == 5
    # Audit endpoints retain their candidate-only contract. Integration is an
    # explicit converted-builder choice, rather than a side effect of reviewing.
    assert store.eligible_records()["train_images"] == 0
    plan = plan_converted_merge(f.temp, f.base, sources=[REPAIR_SOURCE])
    assert plan["candidate_train_images"] == 5
    assert REPAIR_SOURCE not in plan_converted_merge(f.temp, f.base)["candidate_counts_by_source"]
    report = build(f)
    dataset, records, marker = load_converted_merge(f.output)
    assert report["extra_train_images"] == 5
    assert report["train_images"] == 7 and report["val_images"] == 1
    assert report["test_images_used"] == report["source_val_images_used"] == 0
    assert records[:len(f.records)] == f.records
    assert marker["collection_id"] == "converted_only" and marker["experimental"] is False
    assert len(NativeDataset(dataset, "train")) == 7 and len(NativeDataset(dataset, "val")) == 1
    assert json.loads((dataset / "reserved_test_ids.json").read_text()) == ["reserved-test"]
    cache = json.loads((dataset / "culane_anno_cache.json").read_text())
    additions = records[len(f.records):]
    assert {row["image_id"] for row in additions} == {row["image_id"] for row in rows}
    for record in additions:
        assert record["candidate_origin"] == "model_proposal"
        assert record["label_origin"] != "human"
        assert record["split"] == "train" and record["original_split"] == "hold"
        effective = json.loads((dataset / record["geometry"]).read_bytes())
        middle = next(lane for lane in effective["lanes"] if lane["slot"] == 2)
        assert len(middle["segments"]) == 1
        assert len(middle["observed_paint_segments"]) == 3
        assert all(section["observed_paint"] is False for section in middle["inferred_sections"])
        # Source y=43.93 is a dash gap, so this sampled row proves the saved
        # continuous annotation reached training instead of stale candidate ink.
        by_y = {point[1]: point[0] for point in cache[record["image"]][1]}
        assert by_y[360] == pytest.approx(60 * 1640 / 128, abs=.001)
        proof = record["isolated_source_provenance"]["clearance"]
        decisions = proof["sample_decisions"]
        assert {item["image_id"] for item in decisions} == set(sample_ids)
        assert all(item["action"] == "approve" for item in decisions)
    assert store.decisions() == votes and original_store.decisions() == original_votes
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert not (f.temp / "registry.sqlite").exists()


def test_one_approval_in_partial_repair_group_only_converts_that_photo(experiment_fixture):
    f = experiment_fixture
    _, rows, store, _, _ = prepared(f)
    current = store.detail(rows[0]["image_id"])
    store.decide(decision(current, "approve", 1900), "Repair Human")
    save_approved_middle_connections(store)
    assert CollegeRepairTraining(f.temp).eligible_records()["train_images"] == 1
    report = build(f)
    _, records, _ = load_converted_merge(f.output)
    assert report["extra_train_images"] == 1
    assert [row["image_id"] for row in records[len(f.records):]] == [current["image_id"]]
    assert len(store.decisions()) == 1


def test_corrected_and_approved_sample_does_not_release_unreviewed_neighbors(experiment_fixture):
    f = experiment_fixture
    root, rows, store, _, _ = prepared(f)
    candidate_bytes = (root / rows[0]["geometry"]).read_bytes()
    current = store.detail(rows[0]["image_id"])
    lanes = deepcopy(current["geometry"]["lanes"])
    lanes[0]["points"][0][0] += 1
    saved = store.correct(SimulationCorrection(image_id=current["image_id"], revision=current["revision"],
        fingerprint=current["fingerprint"], lanes=lanes, approve=True), "Repair Human")
    assert saved["decision"]["action"] == "approve"
    assert store.status()["groups"][0]["state"] == "needs_repair"
    report = build(f)
    dataset, records, _ = load_converted_merge(f.output)
    assert report["extra_train_images"] == 1
    assert records[-1]["image_id"] == current["image_id"]
    effective = json.loads((dataset / records[-1]["geometry"]).read_bytes())
    assert effective["lanes"][0]["points"][0][0] == lanes[0]["points"][0][0]
    assert (root / rows[0]["geometry"]).read_bytes() == candidate_bytes


def test_problem_repair_group_cannot_enter_training_through_manual_approval(experiment_fixture):
    f = experiment_fixture
    _, rows, store, _, _ = prepared(f, audit_mode="needs_repair")
    current = store.detail(rows[0]["image_id"])
    with pytest.raises(HTTPException) as error:
        store.decide(decision(current, "approve", 1950), "Repair Human")
    assert error.value.status_code == 422
    saved = store.correct(SimulationCorrection(image_id=current["image_id"], revision=current["revision"],
        fingerprint=current["fingerprint"], lanes=current["geometry"]["lanes"],
        resolved_qc_reasons=["short_native_polyline"], approve=True), "Repair Human")
    assert saved["decision"] is None
    assert CollegeRepairTraining(f.temp).eligible_records()["train_images"] == 0
    report = build(f)
    assert report["extra_train_images"] == 0
    assert load_converted_merge(f.output)[1] == f.records


def test_revoked_repair_approval_during_merge_aborts_publication(experiment_fixture):
    f = experiment_fixture
    _, _, store, _, _ = prepared(f)
    samples = approve_samples(store)
    save_approved_middle_connections(store)
    changed = False

    def progress(value):
        nonlocal changed
        if value["phase"] == "verifying_source_clearance" and not changed:
            changed = True
            store.decide(decision(store.detail(samples[0]), "skip", 1990), "Repair Human")

    with pytest.raises(ValueError, match="changed|clearance|approval"):
        build(f, progress=progress)
    assert changed and not f.output.exists()
    assert list(f.temp.glob("experiment.preparing-*/FAILED.json"))


def test_new_unrelated_approval_during_build_does_not_change_frozen_selection(experiment_fixture):
    f = experiment_fixture
    _, rows, store, _, _ = prepared(f)
    first = store.detail(rows[0]["image_id"])
    store.decide(decision(first, "approve", 2000), "Repair Human")
    second_id = rows[2]["image_id"]
    second_vote = None

    def progress(value):
        nonlocal second_vote
        if value["phase"] == "copying_cleared_base" and second_vote is None:
            store.decide(decision(store.detail(second_id), "approve", 2001), "Repair Human")
            second_vote = deepcopy(store.decisions()[second_id])

    report = build(f, progress=progress)
    _, records, _ = load_converted_merge(f.output)
    assert report["extra_train_images"] == 1
    assert [row["image_id"] for row in records[len(f.records):]] == [first["image_id"]]
    assert second_vote is not None
    assert store.decisions()[second_id] == second_vote
    assert len(store.decisions()) == 2
