"""Frozen dataset counts and previews must never claim or rewrite source reviews."""
import hashlib
import json
from pathlib import Path
import sqlite3

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from bcdata.studio_datasets import StudioDatasets, create_studio_datasets_router


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_bytes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def frozen(tmp_path):
    run = tmp_path / "models/ufld_baseline/run-base"
    snapshot = run / "dataset"
    records = []
    cache = {}
    for index, (source, split, has_lane) in enumerate([
            ("a2d2", "train", True), ("a2d2", "train", False),
            ("comma10k", "train", True), ("comma10k", "val", True)]):
        image_id = f"{source}-{index}"
        image = f"images/{split}/{image_id}.jpg"
        path = snapshot / image
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1640, 590), (25, 35, 40)).save(path)
        records.append({"image_id": image_id, "image": image, "source": source,
                        "split": split, "pool": "commercial", "decision": "source_validated"})
        cache[image] = [[[-100000, 500], [-100000, 400], [-100000, 300]] for _ in range(4)]
        if has_lane:
            cache[image][1] = [[140, 500], [240, 400], [-100000, 300]]
    sha = write(snapshot / "manifest.json", records)
    cache_sha = write(snapshot / "culane_anno_cache.json", cache)
    write(snapshot / "hashes.json", {"manifest.json": sha, "culane_anno_cache.json": cache_sha})
    write(snapshot / "reserved_test_ids.json", ["test-1", "test-2"])
    write(snapshot / "export_marker.json", {"errors": [], "validation": {"ok": True}})
    write(run / "status.json", {"best_val_f1": .720802005, "best_short_span_val_f1": .7978})
    write(tmp_path / "reports/ufld_training.json", {"output": str(run), "manifest_sha256": sha})
    experiment = tmp_path / "experiments/flagged_teacher/run-20261021"
    metadata = {"base_run": str(run), "base_manifest_sha256": sha, "phase": "preparing_teacher",
                "flagged_images": 9, "flagged_by_split": {"train": 6, "val": 2, "test": 1},
                "proposals_done": 2, "teacher_progress": {"phase": "checking_known_semantic_pixels", "processed": 1, "total": 3}}
    write(experiment / "experiment.json", metadata)
    # A real source DB is present solely to prove it is never opened or changed.
    con = sqlite3.connect(tmp_path / "metadata.sqlite")
    con.execute("create table reviews(image_id text, status text)")
    con.execute("insert into reviews values('source-1','corrected')")
    con.commit(); con.close()
    preview = tmp_path / "published-previews"
    write(preview / "provenance.json", {"items": [{"filename": "0157.jpg", "dimensions": [1280, 720],
          "source_url": "https://carlanebenchmark.github.io/example.jpg"}]})
    Image.new("RGB", (1280, 720)).save(preview / "0157.jpg")
    store = StudioDatasets(tmp_path, preview_root=preview)
    app = FastAPI()

    def session(request: Request):
        if request.headers.get("x-fixture-session") != "paired":
            raise HTTPException(401, "Pair first")
        return {"reviewer": "Nick"}

    app.include_router(create_studio_datasets_router(tmp_path, session))
    with TestClient(app) as client:
        yield {"root": tmp_path, "run": run, "snapshot": snapshot, "records": records,
               "cache": cache, "sha": sha, "experiment": experiment, "metadata": metadata,
               "store": store, "client": client, "headers": {"x-fixture-session": "paired"}}


def variant(catalog, name):
    return next(v for v in catalog["variants"] if v["id"] == name)


def prepare_extra(frozen):
    run = frozen["experiment"] / "ufld_run"
    extra = {"image_id": "experimental-copy-1", "image": "images/train/copy-1.jpg",
             "source": "a2d2", "split": "train", "label_origin": "teacher_pseudo",
             "decision": "experimental_pseudo", "pool": "commercial"}
    sha = write(run / "dataset/manifest.json", frozen["records"] + [extra])
    write(run / "dataset/hashes.json", {"manifest.json": sha})
    write(run / "dataset/reserved_test_ids.json", ["test-1", "test-2"])
    report = {"format": "ufld_teacher_experiment_v1", "experimental": True, "auto_promote": False,
              "base_manifest_sha256": frozen["sha"], "base_train_images": 3, "base_val_images": 1,
              "extra_train_images": 1, "train_images": 4, "val_images": 1, "test_images_used": 0}
    write(run / "dataset/experiment.json", report)
    write(run / "status.json", {"best_val_f1": .74})
    return run, report


def prepare_converted_base(frozen):
    run = frozen["root"] / "models/ufld_baseline/run-converted-cleared"
    snapshot = run / "dataset"
    extra = {"image_id": "college-cleared-1", "image": "images/train/college-cleared-1.jpg",
             "source": "college_station_lane_marking", "split": "train",
             "pool": "commercial", "decision": "corrected"}
    records = frozen["records"] + [extra]
    for record in frozen["records"]:
        target = snapshot / record["image"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((frozen["snapshot"] / record["image"]).read_bytes())
    Image.new("RGB", (1640, 590), (75, 85, 90)).save(snapshot / extra["image"])
    cache = {**frozen["cache"], extra["image"]: [[], [[150, 500], [250, 400]], [], []]}
    sha = write(snapshot / "manifest.json", records)
    cache_sha = write(snapshot / "culane_anno_cache.json", cache)
    reserved_sha = write(snapshot / "reserved_test_ids.json", ["test-1", "test-2"])
    write(snapshot / "hashes.json", {"manifest.json": sha, "culane_anno_cache.json": cache_sha,
                                     "reserved_test_ids.json": reserved_sha})
    write(snapshot / "export_marker.json", {"errors": [], "validation": {"ok": True},
                                            "counts": {"train": 4, "val": 1, "test": 2}})
    write(run / "status.json", {"phase": "training", "best_val_f1": .75})
    pointer = {"schema": "converted_data:v1", "run": str(run), "manifest_sha256": sha}
    write(frozen["root"] / "reports/converted_data.json", pointer)
    return run, snapshot, records, pointer


@pytest.mark.parametrize("relative", [False, True])
def test_canonical_converted_base_updates_collection_and_previews_but_not_frozen_experiments(frozen, relative):
    prepare_extra(frozen)
    overnight_fixture(frozen)
    run, _, _, pointer = prepare_converted_base(frozen)
    if relative:
        pointer["run"] = str(run.relative_to(frozen["root"]))
        write(frozen["root"] / "reports/converted_data.json", pointer)
    before = tree_bytes(frozen["root"])
    catalog = frozen["store"].catalog()
    converted = variant(catalog, "converted_only")
    assert catalog["base"]["run_id"] == run.name
    assert converted["base_ref"] == {"run_id": run.name, "manifest_sha256": pointer["manifest_sha256"]}
    assert converted["training_ready"] and converted["total_train_images"] == 4
    assert converted["base_train_images"] == 4 and converted["val_images"] == 1
    assert converted["reserved_test_images"] == 2 and converted["test_images_used_for_training"] == 0
    assert converted["metrics"]["best_val_f1"] == .75
    assert any(source["source"] == "college_station_lane_marking" for source in converted["sources"])
    for collection in catalog["variants"][1:]:
        assert collection["base_ref"] == {"run_id": frozen["run"].name, "manifest_sha256": frozen["sha"]}
        assert collection["base_train_images"] == collection["total_train_images"] == 3
    assert variant(catalog, "converted_plus_proposals")["provisional_available_extra_train_images"] == 1
    assert catalog["overnight_queue"]["base_ref"]["run_id"] == frozen["run"].name
    response = frozen["client"].get("/api/studio/datasets/base/samples?limit=12", headers=frozen["headers"])
    assert response.status_code == 200
    assert response.json()["base_ref"] == converted["base_ref"]
    assert response.json()["total"] == 4
    assert "college-cleared-1" in {item["image_id"] for item in response.json()["items"]}
    assert frozen["client"].get("/api/studio/datasets/base/photos/college-cleared-1", headers=frozen["headers"]).status_code == 200
    assert tree_bytes(frozen["root"]) == before


@pytest.mark.parametrize("changes", [
    {"schema": "other"}, {"run": None}, {"run": ""}, {"manifest_sha256": None},
    {"manifest_sha256": ""}, {"manifest_sha256": "not-a-sha256"}, {"manifest_sha256": "0" * 64},
    {"run": "../outside"}, {"run": "reports"}, {"run": "models/ufld_baseline"},
    {"run": "experiments/flagged_teacher/run-20261021"},
])
def test_invalid_canonical_pointer_fails_closed_without_repinning_experiment(frozen, changes):
    _, _, _, pointer = prepare_converted_base(frozen)
    write(frozen["root"] / "reports/converted_data.json", {**pointer, **changes})
    catalog = frozen["store"].catalog()
    assert not catalog["base"]["available"]
    assert not variant(catalog, "converted_only")["training_ready"]
    assert variant(catalog, "converted_plus_proposals")["base_ref"]["manifest_sha256"] == frozen["sha"]
    assert catalog["warnings"]
    assert frozen["client"].get("/api/studio/datasets/base/samples", headers=frozen["headers"]).status_code == 409
    assert frozen["client"].get("/api/studio/datasets/base/photos/a2d2-0", headers=frozen["headers"]).status_code == 409


@pytest.mark.parametrize("problem", ["unapproved", "export_failed", "reserved_overlap", "manifest_hash"])
def test_canonical_selection_uses_all_existing_base_integrity_guards(frozen, problem):
    _, snapshot, records, pointer = prepare_converted_base(frozen)
    if problem == "unapproved":
        records[-1]["decision"] = "experimental_pseudo"
        pointer["manifest_sha256"] = write(snapshot / "manifest.json", records)
        hashes = json.loads((snapshot / "hashes.json").read_text())
        write(snapshot / "hashes.json", {**hashes, "manifest.json": pointer["manifest_sha256"]})
        write(frozen["root"] / "reports/converted_data.json", pointer)
    elif problem == "export_failed":
        write(snapshot / "export_marker.json", {"errors": ["invalid lane"], "validation": {"ok": False}})
    elif problem == "reserved_overlap":
        write(snapshot / "reserved_test_ids.json", ["college-cleared-1", "test-2"])
    else:
        hashes = json.loads((snapshot / "hashes.json").read_text())
        write(snapshot / "hashes.json", {**hashes, "manifest.json": "0" * 64})
    assert not variant(frozen["store"].catalog(), "converted_only")["training_ready"]
    assert frozen["client"].get("/api/studio/datasets/base/samples", headers=frozen["headers"]).status_code == 409


def test_canonical_pointer_updates_are_observed_and_malformed_pointer_never_falls_back(frozen):
    assert frozen["store"].catalog()["base"]["run_id"] == frozen["run"].name
    run, _, _, _ = prepare_converted_base(frozen)
    assert frozen["store"].catalog()["base"]["run_id"] == run.name
    pointer = frozen["root"] / "reports/converted_data.json"
    pointer.write_text("{partial", encoding="utf-8")
    assert not frozen["store"].catalog()["base"]["available"]
    pointer.unlink()
    assert frozen["store"].catalog()["base"]["run_id"] == frozen["run"].name


def test_valid_canonical_base_is_independent_of_a_broken_experiment_base(frozen):
    run, _, _, _ = prepare_converted_base(frozen)
    write(frozen["experiment"] / "experiment.json", {**frozen["metadata"], "base_run": "../outside"})
    catalog = frozen["store"].catalog()
    assert catalog["base"]["run_id"] == run.name
    assert variant(catalog, "converted_only")["training_ready"]
    assert variant(catalog, "converted_plus_proposals")["base_ref"]["run_id"] is None


def test_counts_describe_full_same_frozen_base_not_queue(frozen):
    catalog = frozen["store"].catalog()
    assert catalog["schema"] == "studio_datasets:v1" and catalog["read_only"]
    base = catalog["base"]
    assert (base["train_images"], base["val_images"], base["reserved_test_images"]) == (3, 1, 2)
    assert base["metrics"]["best_val_f1"] == .720802005
    assert base["sources"] == [
        {"source": "a2d2", "title": "Audi A2D2", "train_images": 2, "val_images": 0},
        {"source": "comma10k", "title": "comma10k", "train_images": 1, "val_images": 1}]
    assert len({json.dumps(v["base_ref"], sort_keys=True) for v in catalog["variants"]}) == 1
    assert all(v["base_train_images"] == 3 for v in catalog["variants"])
    proposals = variant(catalog, "converted_plus_proposals")
    assert proposals["candidate_train_images"] == 6
    assert proposals["extra_train_images"] == 0 and proposals["total_train_images"] == 3
    assert proposals["provisional_available_extra_train_images"] is None
    assert not proposals["training_ready"]
    assert catalog["active_experiment"]["proposals_done"] == 2
    assert catalog["active_experiment"]["teacher_progress"]["processed"] == 1
    assert variant(catalog, "converted_only")["training_ready"]
    combined = variant(catalog, "converted_plus_proposals_plus_simulated")
    assert len(catalog["variants"]) == 4
    assert not combined["training_ready"] and combined["extra_train_images"] == 0
    assert combined["simulated_extra_train_images"] == 0


def test_simulation_is_preview_only_and_has_no_imported_training_additions(frozen):
    simulation = variant(frozen["store"].catalog(), "converted_plus_simulated")
    assert simulation["state"] == "preview_only" and not simulation["training_ready"]
    assert simulation["preview_images"] == 1 and simulation["extra_train_images"] == 0
    assert simulation["total_train_images"] == 3
    assert simulation["published_simulated_train_images"] == 24000
    assert simulation["imported_simulated_train_images"] == 0
    assert simulation["previews"][0]["image_url"] == "/static/dataset_samples/carlane-tulane/0157.jpg"


def test_verified_frozen_qc_snapshot_is_only_provisional_until_audited(frozen):
    run, report = prepare_extra(frozen)
    catalog = frozen["store"].catalog()
    proposals = variant(catalog, "converted_plus_proposals")
    assert not proposals["training_ready"] and proposals["extra_train_images"] == 0
    assert proposals["provisional_available_extra_train_images"] == 1
    assert proposals["total_train_images"] == 3 and proposals["candidate_train_images"] == 6
    assert proposals["state"] == "awaiting_audit"
    assert proposals["metrics"] == {} and proposals["provisional_metrics"]["best_val_f1"] == .74
    assert proposals["extra_sources"] == [] and proposals["provisional_extra_sources"]
    report["val_images"] = 9
    write(run / "dataset/experiment.json", report)
    invalid = variant(frozen["store"].catalog(), "converted_plus_proposals")
    assert not invalid["training_ready"] and invalid["extra_train_images"] == 0
    assert invalid["provisional_available_extra_train_images"] is None


def test_proposal_snapshot_cannot_omit_base_or_include_holdout_extras(frozen):
    run, _ = prepare_extra(frozen)
    manifest = frozen["records"] + [{"image_id": "holdout-copy", "split": "val"}]
    sha = write(run / "dataset/manifest.json", manifest)
    write(run / "dataset/hashes.json", {"manifest.json": sha})
    assert not variant(frozen["store"].catalog(), "converted_plus_proposals")["training_ready"]


def test_experimental_snapshot_keeps_reserved_test_ids_unchanged(frozen):
    run, _ = prepare_extra(frozen)
    write(run / "dataset/reserved_test_ids.json", [])
    assert not variant(frozen["store"].catalog(), "converted_plus_proposals")["training_ready"]


def test_fallback_production_without_experiment_is_valid(frozen):
    (frozen["experiment"] / "experiment.json").unlink()
    catalog = frozen["store"].catalog()
    assert catalog["base"]["available"] and catalog["active_experiment"] is None
    assert variant(catalog, "converted_plus_proposals")["candidate_train_images"] is None


def test_missing_base_is_graceful_and_creates_nothing(tmp_path):
    before = tree_bytes(tmp_path)
    catalog = StudioDatasets(tmp_path).catalog()
    assert not catalog["base"]["available"]
    assert all(not v["training_ready"] for v in catalog["variants"])
    assert tree_bytes(tmp_path) == before


@pytest.mark.parametrize("unsafe", ["../outside", "reports", "/tmp/outside-baseline"])
def test_active_base_path_fails_closed_outside_baselines(frozen, unsafe):
    metadata = dict(frozen["metadata"], base_run=unsafe)
    write(frozen["experiment"] / "experiment.json", metadata)
    assert not frozen["store"].catalog()["base"]["available"]
    response = frozen["client"].get("/api/studio/datasets/base/samples", headers=frozen["headers"])
    assert response.status_code == 409


def test_base_hash_reserved_overlap_and_bad_clearance_fail_closed(frozen):
    write(frozen["snapshot"] / "reserved_test_ids.json", [frozen["records"][0]["image_id"]])
    assert not frozen["store"].catalog()["base"]["available"]
    write(frozen["snapshot"] / "reserved_test_ids.json", ["test-1"])
    altered = [dict(r) for r in frozen["records"]]
    altered[0]["decision"] = "experimental_pseudo"
    sha = write(frozen["snapshot"] / "manifest.json", altered)
    # Even a refreshed fingerprint cannot make an unapproved base record eligible.
    hashes = json.loads((frozen["snapshot"] / "hashes.json").read_text())
    hashes["manifest.json"] = sha
    write(frozen["snapshot"] / "hashes.json", hashes)
    write(frozen["experiment"] / "experiment.json", dict(frozen["metadata"], base_manifest_sha256=sha))
    assert not frozen["store"].catalog()["base"]["available"]


def test_malformed_progress_and_nonfinite_metrics_never_break_catalog(frozen):
    write(frozen["experiment"] / "experiment.json", dict(frozen["metadata"],
          teacher_progress=["invalid"], flagged_by_split={"train": "21087"}, proposals_done=-3))
    path = frozen["experiment"] / "teacher/status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{partial", encoding="utf-8")
    write(frozen["run"] / "status.json", {"best_val_f1": float("nan")})
    catalog = frozen["store"].catalog()
    assert catalog["base"]["available"] and catalog["base"]["metrics"] == {}
    assert catalog["active_experiment"]["teacher_progress"] is None
    assert catalog["active_experiment"]["proposals_done"] is None
    assert catalog["active_experiment"]["candidate_train_images"] is None
    assert catalog["warnings"]


def test_auth_required_for_catalog_samples_and_photos(frozen):
    for endpoint in ("", "/base/samples", "/base/photos/a2d2-0"):
        assert frozen["client"].get("/api/studio/datasets" + endpoint).status_code == 401


def test_read_endpoints_never_open_db_claim_or_write_and_preview_observed_geometry(frozen, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Dataset catalog must never open SQLite")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    before = tree_bytes(frozen["root"])
    client, headers = frozen["client"], frozen["headers"]
    assert client.get("/api/studio/datasets", headers=headers).status_code == 200
    response = client.get("/api/studio/datasets/base/samples?limit=3", headers=headers)
    assert response.status_code == 200
    result = response.json()
    assert result["total"] == 3 and result["next_offset"] is None
    assert [item["source"] for item in result["items"]] == ["a2d2", "comma10k", "a2d2"]
    assert result["items"][0]["geometry"]["lanes"] == [{"slot": 2, "points": [[140, 500], [240, 400]]}]
    assert result["items"][2]["geometry"]["lanes"] == []
    for item in result["items"]:
        photo = client.get(item["image_url"], headers=headers)
        assert photo.status_code == 200 and photo.headers["content-type"] == "image/jpeg"
    assert tree_bytes(frozen["root"]) == before


def test_pagination_and_invalid_photo_identifiers(frozen):
    first = frozen["store"].samples(0, 1)
    second = frozen["store"].samples(1, 1)
    assert first["next_offset"] == 1 and first["items"][0] != second["items"][0]
    assert frozen["store"].samples(100, 3)["items"] == []
    with pytest.raises(HTTPException) as error:
        frozen["store"].photo("..%2Foutside")
    assert error.value.status_code == 400
    for image_id in ("comma10k-3", "not-in-manifest"):
        assert frozen["client"].get("/api/studio/datasets/base/photos/" + image_id,
                                    headers=frozen["headers"]).status_code == 404


def test_photo_symlink_or_traversal_manifest_cannot_escape_snapshot(frozen):
    records = [dict(r) for r in frozen["records"]]
    records[0]["image"] = "../../../../metadata.sqlite"
    sha = write(frozen["snapshot"] / "manifest.json", records)
    hashes = json.loads((frozen["snapshot"] / "hashes.json").read_text())
    hashes["manifest.json"] = sha
    write(frozen["snapshot"] / "hashes.json", hashes)
    write(frozen["experiment"] / "experiment.json", dict(frozen["metadata"], base_manifest_sha256=sha))
    assert not frozen["store"].catalog()["base"]["available"]


def test_cached_manifest_invalidates_when_file_changes(frozen):
    first = frozen["store"].catalog()
    assert first["base"]["available"]
    (frozen["snapshot"] / "manifest.json").write_text("[]", encoding="utf-8")
    assert not frozen["store"].catalog()["base"]["available"]


@pytest.mark.parametrize("state", ["downloading", "converting", "awaiting_audit", "ready", "error"])
def test_simulation_import_progress_is_truthful_but_never_a_training_manifest(frozen, state):
    path = frozen["root"] / "experiments/simulated_tulane/import.json"
    write(path, {"schema": "tulane_simulation_import:v1", "source": "carlane_tulane",
                 "domain": "synthetic", "license": "Apache-2.0", "state": state,
                 "total_expected": 26400, "counts": {"downloaded": 100, "converted": 80,
                    "train_images": 70, "val_images": 10, "audit_samples": 6,
                    "audits_remaining": 3, "eligible_train_images": 70}})
    before = tree_bytes(frozen["root"])
    c = frozen["store"].catalog()
    status = c["simulation_import"]
    assert status["state"] == state and status["counts"]["downloaded"] == 100
    assert status["downloaded"] == 100 and status["audits_remaining"] == 3
    assert status["reported_eligible_train_images"] == 70
    assert not status["training_manifest_verified"] and status["verified_eligible_train_images"] == 0
    for name in ("converted_plus_simulated", "converted_plus_proposals_plus_simulated"):
        v = variant(c, name)
        assert v["imported_simulated_train_images"] == 70
        assert v["simulated_extra_train_images"] == 0 and not v["training_ready"]
        assert v["simulation_import"] == status
    assert variant(c, "converted_plus_simulated")["state"] == state
    assert tree_bytes(frozen["root"]) == before


def test_fourth_variant_keeps_selected_model_additions_separate_from_uncleared_simulation(frozen):
    prepare_extra(frozen)
    c = frozen["store"].catalog()
    combined = variant(c, "converted_plus_proposals_plus_simulated")
    assert combined["base_train_images"] == 3 and combined["model_proposal_train_images"] == 0
    assert combined["model_extra_train_images"] == 0 and combined["model_proposal_candidate_images"] == 1
    assert combined["extra_train_images"] == 0 and combined["total_train_images"] == 3
    assert not combined["model_proposals_training_ready"] and not combined["simulation_training_ready"]
    assert not combined["training_ready"]
    assert combined["base_ref"] == variant(c, "converted_only")["base_ref"]


@pytest.mark.parametrize("altered", [{"domain": "real"}, {"license": "unknown"},
                                       {"source": "tusimple"}, {"schema": "other"}])
def test_simulated_source_contract_rejects_real_or_unknown_license_metadata(frozen, altered):
    meta = {"schema": "tulane_simulation_import:v1", "source": "carlane_tulane",
            "domain": "synthetic", "license": "Apache-2.0", "state": "ready", "train_images": 24000}
    write(frozen["root"] / "experiments/simulated_tulane/import.json", {**meta, **altered})
    c = frozen["store"].catalog()
    assert c["simulation_import"]["state"] == "error"
    assert variant(c, "converted_plus_simulated")["extra_train_images"] == 0
    assert c["warnings"]


def overnight_fixture(frozen):
    output = frozen["root"] / "experiments/overnight_collections/run-night/collection_3"
    write(output / "status.json", {"phase": "training", "epoch": 2, "epochs": 20,
                                   "train_images": 8, "best_val_f1": .75})
    write(output / "dataset/experiment.json", {"train_images": 8, "extra_train_images": 5})
    metadata = {"schema": "overnight_collection_queue:v1", "state": "running", "pid": 123,
                "base_run": str(frozen["run"]), "base_manifest_sha256": frozen["sha"],
                "provisional": True, "unaudited_simulation": True, "auto_promote": False,
                "approved_collections_unchanged": True,
                "jobs": [{"id": "collection_2", "collection_id": "converted_plus_proposals", "state": "complete",
                          "output_run": str(frozen["experiment"] / "ufld_run"), "extra_images": 1,
                          "train_images": 4, "metrics": {"best_val_f1": .74}},
                         {"id": "collection_3", "collection_id": "converted_plus_simulated", "state": "running",
                          "output_run": str(output)},
                         {"id": "collection_4", "collection_id": "converted_plus_proposals_plus_simulated",
                          "state": "waiting_proposals"}]}
    path = frozen["root"] / "experiments/overnight_collections/active.json"
    write(path, metadata)
    return path, metadata, output


def test_overnight_queue_progress_is_private_and_never_changes_approved_counts(frozen, monkeypatch):
    prepare_extra(frozen)
    overnight_fixture(frozen)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("No SQLite access allowed"))
    before = tree_bytes(frozen["root"])
    c = frozen["store"].catalog()
    queue = c["overnight_queue"]
    assert queue["state"] == "running" and queue["provisional"] and queue["unaudited_simulation"]
    assert not queue["auto_promote"] and queue["approved_collections_unchanged"]
    assert [job["id"] for job in queue["jobs"]] == ["collection_2", "collection_3", "collection_4"]
    job = queue["jobs"][1]
    assert job["progress"]["epoch"] == 2 and job["metrics"]["best_val_f1"] == .75
    assert job["train_images"] == 8 and job["extra_images"] == 5
    for v in c["variants"][1:]:
        assert v["extra_train_images"] == 0 and v["total_train_images"] == 3 and not v["training_ready"]
        assert v["provisional_experiment"]["queue_state"] == "running"
    assert "provisional_experiment" not in c["variants"][0]
    assert tree_bytes(frozen["root"]) == before


@pytest.mark.parametrize("changes", [{"provisional": False}, {"auto_promote": True},
        {"approved_collections_unchanged": False}, {"unaudited_simulation": False},
        {"state": "approved"}, {"base_manifest_sha256": "0" * 64},
        {"base_run": "../outside"}])
def test_overnight_queue_invalid_contract_fails_closed(frozen, changes):
    path, metadata, _ = overnight_fixture(frozen)
    write(path, {**metadata, **changes})
    c = frozen["store"].catalog()
    assert c["overnight_queue"]["state"] == "error"
    assert c["overnight_queue"]["jobs"] == [] and c["warnings"]
    assert all("provisional_experiment" not in v for v in c["variants"])


@pytest.mark.parametrize("output", ["../outside", "models/ufld_baseline/run-base", "reports"])
def test_overnight_queue_output_paths_cannot_escape_isolated_roots(frozen, output):
    path, metadata, _ = overnight_fixture(frozen)
    metadata["jobs"][1]["output_run"] = output
    write(path, metadata)
    assert frozen["store"].catalog()["overnight_queue"]["jobs"] == []


def test_overnight_queue_malformed_atomic_updates_and_metrics_are_safe(frozen):
    path, metadata, output = overnight_fixture(frozen)
    (output / "status.json").write_text("{partial", encoding="utf-8")
    metadata["jobs"][1]["metrics"] = {"best_val_f1": float("nan")}
    write(path, metadata)
    c = frozen["store"].catalog()
    assert c["overnight_queue"]["jobs"][1]["metrics"] == {}
    assert c["overnight_queue"]["jobs"][1]["train_images"] == 8
    path.write_text("{partial", encoding="utf-8")
    assert frozen["store"].catalog()["overnight_queue"]["state"] == "error"


def test_overnight_queue_wrong_order_or_duplicate_job_is_rejected(frozen):
    path, metadata, _ = overnight_fixture(frozen)
    metadata["jobs"] = list(reversed(metadata["jobs"]))
    write(path, metadata)
    assert frozen["store"].catalog()["overnight_queue"]["jobs"] == []
    metadata["jobs"] = [metadata["jobs"][0], metadata["jobs"][0]]
    write(path, metadata)
    assert frozen["store"].catalog()["overnight_queue"]["jobs"] == []


def test_overnight_queue_requires_pinned_base_manifest_hash(frozen):
    path, metadata, _ = overnight_fixture(frozen)
    del metadata["base_manifest_sha256"]
    write(path, metadata)
    assert frozen["store"].catalog()["overnight_queue"]["jobs"] == []


@pytest.mark.parametrize("state", ["building", "waiting_space", "waiting_import"])
def test_overnight_building_and_wait_states_never_add_approved_training_rows(frozen, state):
    path, metadata, _ = overnight_fixture(frozen)
    metadata["state"] = state
    metadata["jobs"][1]["state"] = state
    write(path, metadata)
    c = frozen["store"].catalog()
    assert c["overnight_queue"]["state"] == state
    assert c["overnight_queue"]["jobs"][1]["state"] == state
    assert all(v["extra_train_images"] == 0 and v["total_train_images"] == 3
               and not v["training_ready"] for v in c["variants"][1:])
