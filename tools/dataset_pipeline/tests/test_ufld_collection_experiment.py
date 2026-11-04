import copy
import json

import numpy as np
from PIL import Image
import pytest

from bcdata.bootstrap import atomic_json
from bcdata import ufld_collection_experiment as collection
from bcdata.import_tulane import convert_record, json_bytes, sha256, source_records
from bcdata.teachers.base import file_sha256
from bcdata.ufld_baseline import load_frozen_export, load_prepared_export
from bcdata.ufld_experiment import build_experiment
from tests.test_ufld_experiment import experiment_fixture
from tests.test_import_tulane import native


@pytest.fixture()
def simulation(experiment_fixture, monkeypatch):
    f = experiment_fixture
    root = f.temp / "simulation"
    expected = {}
    for split, count in (("train", 3), ("val", 1)):
        values = [native(split, index) for index in range(count)]
        if split == "train":
            values[1]["lanes"][0][10] = -2  # retained for audit, excluded from private structural QC.
        raw = b"".join(json_bytes(value) for value in values)
        path = root / "upstream" / ("source_" + split + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        expected[split] = (path.name, count, sha256(raw))
    monkeypatch.setattr(collection, "PINNED", expected)
    records = source_records(root / "upstream", expected=expected)
    rows = []
    import io
    for index, record in enumerate(records):
        stream = io.BytesIO()
        Image.new("RGB", (1280, 720), (index * 40 + 5, index * 20 + 6, index * 30 + 7)).save(stream, format="JPEG")
        raw = stream.getvalue()
        rows.append(convert_record(root, record, fetcher=lambda url, raw=raw: raw))
    manifest_raw = b"".join(json_bytes(row) for row in rows)
    (root / "manifest.jsonl").write_bytes(manifest_raw)
    atomic_json(root / "import.json", {"schema": "tulane_simulation_import:v1", "source": "carlane_tulane",
        "domain": "synthetic", "license": "Apache-2.0", "dataset_version": 2,
        "state": "awaiting_audit", "errors": [], "converted": len(rows),
        "manifest_sha256": sha256(manifest_raw), "eligible_train_images": 0})
    atomic_json(root / "plan.json", {"schema": "tulane_simulation_plan:v1", "source": "carlane_tulane",
        "dataset_version": 2, "source_annotations": {split: {"filename": row[0], "count": row[1], "sha256": row[2]}
        for split, row in expected.items()}, "pilot": False, "total_expected": len(rows)})
    provenance = root / "provenance"
    provenance.mkdir()
    (provenance / "LICENSE-Apache-2.0.txt").write_text("Apache License fixture")
    (provenance / "publisher-page.html").write_text("Commercial permission fixture")
    (provenance / "NOTICE.txt").write_text("Synthetic source only; no TuSimple target")
    atomic_json(provenance / "provenance.json", {"source": "carlane_tulane", "domain": "synthetic",
        "license": "Apache-2.0", "dataset_version": 2, "download_scope": ["train/source", "val/source"],
        "license_sha256": file_sha256(provenance / "LICENSE-Apache-2.0.txt"),
        "publisher_page_sha256": file_sha256(provenance / "publisher-page.html")})
    return root, rows


def build_sim(f, simulation, **kwargs):
    return collection.build_collection_experiment(f.base, f.temp / "collection3",
        split_inventory=f.inventory, collection_id="converted_plus_simulated", sim_root=simulation[0], **kwargs)


def rehash(snapshot, relative):
    hashes = json.loads((snapshot / "hashes.json").read_text())
    hashes[relative] = file_sha256(snapshot / relative)
    atomic_json(snapshot / "hashes.json", hashes)


def test_unaudited_simulation_is_private_typed_and_preserves_every_cleared_byte(experiment_fixture, simulation):
    f = experiment_fixture
    before = {p: p.read_bytes() for p in f.dataset.rglob("*") if p.is_file()}
    report = build_sim(f, simulation)
    snapshot, records, marker = collection.load_collection_export(f.temp / "collection3")
    assert records[:3] == f.records
    assert report["train_images"] == 4 and report["val_images"] == 1
    assert report["simulated_train_images"] == 2 and report["model_train_images"] == 0
    assert report["extra_origin_counts"] == {"simulator_projected_gt": 2}
    assert report["auto_promote"] is False and report["provisional"] and report["unaudited"]
    assert report["test_images_used"] == 0 and marker["experimental"]
    assert all(r["decision"] == "experimental_unaudited_simulator" and r["split"] == "train" for r in records[3:])
    assert all(r["audit_status"] == "not_consulted" for r in records[3:])
    assert not (simulation[0] / "review.sqlite").exists()
    assert not (simulation[0] / "audits.json").exists()
    assert json.loads((simulation[0] / "import.json").read_text())["eligible_train_images"] == 0
    assert report["rejection_counts"] == {"native_structural_qc": 1, "official_simulation_validation_is_held_out": 1}
    for path, raw in before.items():
        assert path.read_bytes() == raw
    for record in f.records:
        assert (snapshot / record["image"]).read_bytes() == (f.dataset / record["image"]).read_bytes()
    assert json.loads((snapshot / "reserved_test_ids.json").read_text()) == ["reserved-test"]
    assert load_prepared_export(f.temp / "collection3", experimental=True)[1] == records
    with pytest.raises(ValueError, match="ineligible labels"):
        load_prepared_export(f.temp / "collection3")
    with pytest.raises(ValueError, match="ineligible labels"):
        load_frozen_export(f.temp / "collection3")


def test_combined_uses_identical_model_and_simulation_additions(experiment_fixture, simulation):
    f = experiment_fixture
    extra = f.extra()
    build_experiment(f.base, f.output, [extra], split_inventory=f.inventory, teacher=f.teacher)
    build_sim(f, simulation)
    report = collection.build_collection_experiment(f.base, f.temp / "collection4",
        split_inventory=f.inventory, collection_id=4, model_run=f.output, sim_root=simulation[0])
    snapshot, records, _ = collection.load_collection_export(f.temp / "collection4")
    model = json.loads((f.output / "dataset/manifest.json").read_text())
    sim = json.loads((f.temp / "collection3/dataset/manifest.json").read_text())
    assert records[:4] == model
    assert records[4:] == sim[3:]
    assert report["train_images"] == 5 and report["model_train_images"] == 1 and report["simulated_train_images"] == 2
    assert records[3]["label_origin"] == "teacher_pseudo" and records[4]["label_origin"] == "simulator_projected_gt"
    assert (snapshot / records[3]["image"]).read_bytes() == (f.output / "dataset" / model[3]["image"]).read_bytes()


def test_model_only_collection_keeps_teacher_snapshot_and_remains_unaudited(experiment_fixture):
    f = experiment_fixture
    extra = f.extra()
    build_experiment(f.base, f.output, [extra], split_inventory=f.inventory, teacher=f.teacher)
    report = collection.build_collection_experiment(f.base, f.temp / "collection2",
        split_inventory=f.inventory, collection_id=2, model_run=f.output)
    snapshot, records, marker = load_prepared_export(f.temp / "collection2", experimental=True)
    assert report["unaudited"] is True and marker["unaudited"] is True
    assert report["simulated_train_images"] == 0 and report["model_train_images"] == 1
    assert not (snapshot / "simulation_import").exists()
    assert records == json.loads((f.output / "dataset/manifest.json").read_text())
    with pytest.raises(ValueError, match="ineligible labels"):
        load_frozen_export(f.temp / "collection2")


@pytest.mark.parametrize("kind", ["source_image", "native_label", "native_geometry", "import_hash", "license", "unfinished"])
def test_bad_source_evidence_never_becomes_a_provisional_training_snapshot(experiment_fixture, simulation, kind):
    f = experiment_fixture
    root, rows = simulation
    if kind in {"source_image", "native_label", "native_geometry"}:
        key = {"source_image": "image", "native_label": "source_label", "native_geometry": "geometry"}[kind]
        (root / rows[0][key]).write_bytes(b"tampered")
    elif kind == "import_hash":
        (root / "manifest.jsonl").write_bytes(b"changed")
    elif kind == "license":
        (root / "provenance/LICENSE-Apache-2.0.txt").write_text("different")
    else:
        metadata = json.loads((root / "import.json").read_text())
        metadata["state"] = "downloading"
        atomic_json(root / "import.json", metadata)
    with pytest.raises(ValueError):
        build_sim(f, simulation)
    assert not (f.temp / "collection3").exists()


@pytest.mark.parametrize("kind", ["no_provisional", "promote", "typed_origin", "lane_extrapolation", "base", "heldout", "missing_hash", "native_bytes"])
def test_loader_rejects_semantic_tampering_even_with_updated_file_hash(experiment_fixture, simulation, kind):
    f = experiment_fixture
    build_sim(f, simulation)
    snapshot = f.temp / "collection3/dataset"
    if kind in {"no_provisional", "promote"}:
        report = json.loads((snapshot / "experiment.json").read_text())
        report["provisional" if kind == "no_provisional" else "auto_promote"] = kind == "promote"
        atomic_json(snapshot / "experiment.json", report)
        rehash(snapshot, "experiment.json")
    elif kind in {"typed_origin", "base", "heldout"}:
        records = json.loads((snapshot / "manifest.json").read_text())
        if kind == "typed_origin": records[-1]["label_origin"] = "teacher_pseudo"
        elif kind == "base": records[0]["lanes"] = 0
        else: records[-1]["split"] = "val"
        atomic_json(snapshot / "manifest.json", records)
        rehash(snapshot, "manifest.json")
    elif kind == "lane_extrapolation":
        cache = json.loads((snapshot / "culane_anno_cache.json").read_text())
        image = json.loads((snapshot / "manifest.json").read_text())[-1]["image"]
        cache[image][1][-2][0] += 10
        atomic_json(snapshot / "culane_anno_cache.json", cache)
        rehash(snapshot, "culane_anno_cache.json")
    elif kind == "missing_hash":
        hashes = json.loads((snapshot / "hashes.json").read_text())
        del hashes["base_anno_cache.json"]
        atomic_json(snapshot / "hashes.json", hashes)
    else:
        records = json.loads((snapshot / "manifest.json").read_text())
        relative = records[-1]["native_evidence"]["source_label"]
        (snapshot / relative).write_bytes(b"tampered")
        rehash(snapshot, relative)
    with pytest.raises(ValueError):
        collection.load_collection_export(f.temp / "collection3")


def test_simulation_train_session_cannot_overlap_reserved_test(experiment_fixture, simulation):
    f = experiment_fixture
    f.inventory[-1]["source"] = "carlane_tulane"
    f.inventory[-1]["session_id"] = simulation[1][0]["session_id"]
    with pytest.raises(ValueError, match="overlaps"):
        build_sim(f, simulation)


def update_sim_manifest(root, rows):
    raw = b"".join(json_bytes(row) for row in rows)
    (root / "manifest.jsonl").write_bytes(raw)
    metadata = json.loads((root / "import.json").read_text())
    metadata["manifest_sha256"] = sha256(raw)
    atomic_json(root / "import.json", metadata)
    return sha256(raw)


@pytest.mark.parametrize("target", ["train", "val"])
def test_duplicate_native_frames_are_excluded_without_aborting_other_training_images(experiment_fixture, simulation, target):
    f = experiment_fixture
    root, original_rows = simulation
    rows = copy.deepcopy(original_rows)
    first = rows[0]
    destination = rows[2] if target == "train" else rows[3]
    (root / destination["image"]).write_bytes((root / first["image"]).read_bytes())
    destination["source_sha256"] = first["source_sha256"]
    update_sim_manifest(root, rows)
    report = build_sim(f, (root, rows))
    assert report["simulated_train_images"] == 1
    assert report["rejection_counts"]["duplicate_or_held_out_source_fingerprint"] == 1
    records = collection.load_collection_export(f.temp / "collection3")[1]
    expected = rows[0]["image_id"] if target == "train" else rows[2]["image_id"]
    assert records[-1]["image_id"] == expected


def test_native_validation_cannot_be_reclassified_by_conversion_manifest(experiment_fixture, simulation):
    f = experiment_fixture
    root, original_rows = simulation
    rows = copy.deepcopy(original_rows)
    rows[-1]["split"] = "train"
    rows[-1]["session_id"] = rows[0]["session_id"]
    update_sim_manifest(root, rows)
    with pytest.raises(ValueError, match="differs from pinned native"):
        build_sim(f, (root, rows))


def test_loader_rejects_train_simulation_fingerprint_shared_with_native_validation(experiment_fixture, simulation):
    f = experiment_fixture
    build_sim(f, simulation)
    snapshot = f.temp / "collection3/dataset"
    records = json.loads((snapshot / "manifest.json").read_text())
    rows = [json.loads(line) for line in (snapshot / "simulation_import/manifest.jsonl").read_bytes().splitlines()]
    rows[-1]["source_sha256"] = records[-1]["source_sha256"]
    digest = update_sim_manifest(snapshot / "simulation_import", rows)
    for name in ("simulation_import/manifest.jsonl", "simulation_import/import.json"):
        rehash(snapshot, name)
    report = json.loads((snapshot / "experiment.json").read_text())
    report["simulated_manifest_sha256"] = digest
    atomic_json(snapshot / "experiment.json", report)
    rehash(snapshot, "experiment.json")
    with pytest.raises(ValueError, match="held-out"):
        collection.load_collection_export(f.temp / "collection3")
