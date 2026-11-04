import copy
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from bcdata.bootstrap import atomic_json
from bcdata.export_ufld import cache_entry, write_sample
from bcdata.geometry import make_crop
from bcdata.quality import read_rgb, write_jpg, write_png
from bcdata.teachers.base import file_sha256
from bcdata.ufld_baseline import (NativeDataset, load_frozen_export, load_prepared_export,
                                  parse_arguments, training_report_path)
from bcdata.ufld_experiment import (PSEUDO_DECISION, PSEUDO_ORIGIN, build_experiment,
                                    load_experiment_export)


def _rgb(seed):
    yy, xx = np.indices((590, 1640))
    return np.stack([((xx * (seed + 1) + yy * 7 + seed * 31) % 256),
                     ((xx * 3 + yy * (seed + 2) + seed * 73) % 256),
                     ((xx * 5 + yy * 2 + seed * 17) % 256)], axis=2).astype(np.uint8)


def _geometry():
    return {"lanes": [{"slot": 2, "physical": True, "points": [[400, 300], [600, 570]],
                        "y_top": 300, "y_bottom": 570,
                        "fit": {"basis": "interp", "xs": [400, 600], "ys": [300, 570]}}]}


@pytest.fixture()
def experiment_fixture(tmp_path):
    base = tmp_path / "base"
    dataset = base / "dataset"
    records, cache, inventory = [], {}, []
    for index, split in enumerate(("train", "train", "val")):
        image_id = "base-" + str(index)
        image = f"images/{split}/{image_id}.jpg"
        segmentation = "laneseg_label_w16/" + image.replace(".jpg", ".png")
        rgb = _rgb(index)
        exists, lanes, _ = write_sample(rgb, _geometry(), make_crop({}, 1640, 590, 1640, 590), 16, 95,
                                         dataset / image, dataset / segmentation)
        (dataset / image).with_suffix(".lines.txt").write_text("".join(
            " ".join(f"{x:.3f} {y}" for x, y in points) + " \n" for points in lanes if points), encoding="utf-8")
        record = {"image": image, "image_id": image_id, "split": split, "source": "fixture",
                  "session_id": f"{split}-session-{index}", "license": "fixture-license", "pool": "commercial",
                  "label_origin": "human", "decision": "approved", "lanes": sum(exists),
                  "source_sha256": file_sha256(dataset / image)}
        records.append(record); inventory.append(copy.deepcopy(record)); cache[image] = cache_entry(lanes)
    atomic_json(dataset / "manifest.json", records)
    atomic_json(dataset / "culane_anno_cache.json", cache)
    atomic_json(dataset / "hashes.json", {p.relative_to(dataset).as_posix(): file_sha256(p)
                                         for p in dataset.rglob("*") if p.is_file()})
    atomic_json(dataset / "export_marker.json", {"revision": 123, "errors": [], "validation": {"ok": True}})
    atomic_json(dataset / "reserved_test_ids.json", ["reserved-test"])
    inventory.append({"image_id": "reserved-test", "source": "fixture", "session_id": "held-test-session",
                      "split": "test", "source_sha256": "f" * 64, "pool": "commercial"})
    teacher = {"name": "test-teacher", "version": "test-v1", "checkpoint_sha256": "a" * 64,
               "training_records": [copy.deepcopy(records[0])]}
    fixture = SimpleNamespace(base=base, dataset=dataset, records=records, inventory=inventory,
                              teacher=teacher, output=tmp_path / "experiment", temp=tmp_path)

    def extra(index=0, **changes):
        image_id = "extra-" + str(index)
        image_path = tmp_path / "input" / (image_id + ".png")
        write_png(image_path, cv2.cvtColor(_rgb(index + 10), cv2.COLOR_RGB2BGR))
        record = {"image_id": image_id, "split": "train", "source": "fixture",
                  "session_id": "extra-session-" + str(index), "pool": "commercial", "license_id": "fixture-license",
                  "source_sha256": file_sha256(image_path), "review_status": "pending", "locked": 0,
                  "width": 1640, "height": 590, **changes}
        fixture.inventory.append(copy.deepcopy(record))
        return {"record": record, "image_path": image_path, "geometry": _geometry()}

    fixture.extra = extra
    fixture.build = lambda extras, **kwargs: build_experiment(
        fixture.base, fixture.output, extras, split_inventory=fixture.inventory,
        teacher=fixture.teacher, **kwargs)
    return fixture


def _rehash(dataset, relative):
    hashes = json.loads((dataset / "hashes.json").read_text())
    hashes[relative] = file_sha256(dataset / relative)
    atomic_json(dataset / "hashes.json", hashes)


def test_experiment_keeps_every_cleared_train_and_val_and_preserves_original_bytes(experiment_fixture):
    f = experiment_fixture
    before = {p: p.read_bytes() for p in f.dataset.rglob("*") if p.is_file()}
    extra = f.extra()
    report = f.build([extra])
    dataset, records, marker = load_experiment_export(f.output)
    assert report["base_train_images"] == 2 and report["extra_train_images"] == 1
    assert report["train_images"] == 3 and report["val_images"] == 1
    assert report["test_images_used"] == 0 and not report["auto_promote"]
    assert records[:3] == f.records
    assert records[-1]["label_origin"] == PSEUDO_ORIGIN and records[-1]["decision"] == PSEUDO_DECISION
    assert records[-1]["original_metadata"] == extra["record"]
    assert len(NativeDataset(dataset, "train")) == 3 and len(NativeDataset(dataset, "val")) == 1
    for path, contents in before.items():
        assert path.read_bytes() == contents
        if path.name not in {"hashes.json", "manifest.json", "culane_anno_cache.json", "export_marker.json"}:
            assert (dataset / path.relative_to(f.dataset)).read_bytes() == contents
    val = f.records[-1]["image"]
    assert (dataset / val).read_bytes() == (f.dataset / val).read_bytes()
    assert json.loads((dataset / "reserved_test_ids.json").read_text()) == ["reserved-test"]
    assert not (dataset / "images/test").exists()
    assert (dataset / "list/test.txt").read_text() == ""
    assert marker["experimental"]
    with pytest.raises(ValueError, match="ineligible labels"):
        load_frozen_export(f.output)
    assert load_prepared_export(f.output, experimental=True)[1] == records
    with pytest.raises(ValueError, match="ineligible labels"):
        load_prepared_export(f.output)


@pytest.mark.parametrize("kind", ["val", "test", "held_session", "rejected", "corrected", "noncommercial", "empty", "changed_bytes", "bad_slot", "unsafe_id"])
def test_rejected_extras_are_separate_and_cannot_displace_cleared_truth(experiment_fixture, kind):
    f = experiment_fixture
    good, bad = f.extra(0), f.extra(1)
    registered = f.inventory[-1]
    if kind in {"val", "test"}:
        bad["record"]["split"] = registered["split"] = kind
    elif kind == "held_session":
        bad["record"]["session_id"] = registered["session_id"] = "held-test-session"
    elif kind in {"rejected", "corrected"}:
        # The descriptor cannot hide an authoritative human decision.
        registered["review_status"] = kind
    elif kind == "noncommercial":
        registered["pool"] = "research"
    elif kind == "empty":
        bad["geometry"] = {"lanes": []}
    elif kind == "changed_bytes":
        Path(bad["image_path"]).write_bytes(b"changed after proposal")
    elif kind == "bad_slot":
        bad["geometry"]["lanes"][0]["slot"] = 5
    elif kind == "unsafe_id":
        bad["record"]["image_id"] = "../../outside"
        # Do not poison the authoritative inventory itself.
    report = f.build([good, bad])
    assert report["extra_train_images"] == 1 and report["rejected_extras"] == 1
    assert load_experiment_export(f.output)[1][:3] == f.records
    rejected = json.loads((f.output / "dataset/rejected_extras.json").read_text())
    assert rejected[0]["reason"]


def test_exact_duplicates_are_not_added_and_cap_is_relative_to_original_cleared_train(experiment_fixture):
    f = experiment_fixture
    extras = [f.extra(index) for index in range(4)]
    extras[1]["image_path"] = extras[0]["image_path"]
    extras[1]["record"]["source_sha256"] = extras[0]["record"]["source_sha256"]
    f.inventory[-3]["source_sha256"] = extras[0]["record"]["source_sha256"]
    report = f.build(extras, max_extra_ratio=.5)
    assert report["extra_train_images"] == 1 and report["base_train_images"] == 2
    assert report["rejected_extras"] == 3
    reasons = report["rejection_counts"]
    assert "duplicate_or_held_out_source_fingerprint" in reasons
    assert reasons["extra_training_ratio_cap"] == 2


@pytest.mark.parametrize("kind", ["id", "session", "fingerprint"])
def test_teacher_training_ancestry_cannot_contain_frozen_validation_or_test(experiment_fixture, kind):
    f = experiment_fixture
    extra = f.extra()
    held = f.records[-1]
    ancestry = dict(f.records[0])
    if kind == "id":
        ancestry["image_id"] = held["image_id"]
    elif kind == "session":
        ancestry["session_id"] = "held-test-session"
    else:
        ancestry["source_sha256"] = held["source_sha256"]
    f.teacher["training_records"].append(ancestry)
    with pytest.raises(ValueError, match="Teacher training"):
        f.build([extra])
    assert not f.output.exists()


def test_recording_sessions_are_source_aware(experiment_fixture):
    f = experiment_fixture
    extra = f.extra(source="another-source", session_id="held-test-session")
    assert f.build([extra])["extra_train_images"] == 1


def test_inventory_must_cover_every_reserved_test_session(experiment_fixture):
    f = experiment_fixture
    extra = f.extra()
    f.inventory = [r for r in f.inventory if r["image_id"] != "reserved-test"]
    with pytest.raises(ValueError, match="every frozen reserved test session"):
        f.build([extra])


def test_new_experiment_cannot_overwrite_existing_output_or_original_run(experiment_fixture):
    f = experiment_fixture
    extra = f.extra()
    f.build([extra])
    fingerprint = file_sha256(f.output / "dataset/manifest.json")
    with pytest.raises(ValueError, match="new directory"):
        f.build([extra])
    with pytest.raises(ValueError, match="new directory"):
        build_experiment(f.base, f.base / "inside", [extra], split_inventory=f.inventory, teacher=f.teacher)
    assert file_sha256(f.output / "dataset/manifest.json") == fingerprint


def test_changed_experiment_bytes_are_rejected_and_base_truth_cannot_be_omitted(experiment_fixture):
    f = experiment_fixture
    f.build([f.extra()])
    dataset = f.output / "dataset"
    records = json.loads((dataset / "manifest.json").read_text())
    (dataset / records[-1]["image"]).write_bytes(b"bad training image")
    with pytest.raises(ValueError, match="Frozen experiment file changed"):
        load_experiment_export(f.output)
    # Even after re-fingerprinting a modified manifest, the immutable base
    # comparison prevents silently replacing/removing cleared train examples.
    records.pop(0)
    atomic_json(dataset / "manifest.json", records)
    _rehash(dataset, "manifest.json")
    hashes = json.loads((dataset / "hashes.json").read_text())
    hashes[json.loads((dataset / "base_manifest.json").read_text())[0]["image"]] = file_sha256(
        dataset / json.loads((dataset / "base_manifest.json").read_text())[0]["image"])
    # Repair only the extra's byte fingerprint, allowing semantic checks to run.
    hashes[records[-1]["image"]] = file_sha256(dataset / records[-1]["image"])
    atomic_json(dataset / "hashes.json", hashes)
    with pytest.raises(ValueError, match="omitted original cleared base"):
        load_experiment_export(f.output)


def test_full_resolution_lossy_copy_requires_exact_explicit_original_and_copy_provenance(experiment_fixture):
    f = experiment_fixture
    extra = f.extra()
    original = Path(extra["image_path"])
    copied = original.with_suffix(".jpg")
    write_jpg(copied, read_rgb(original), 97)
    copy_hash = file_sha256(copied)
    extra.update(image_path=copied, image_sha256=copy_hash, copy_provenance={
        "original_sha256": extra["record"]["source_sha256"], "copy_sha256": copy_hash,
        "transform": "full_resolution_jpeg_q97", "width": 1640, "height": 590})
    f.build([extra])
    record = load_experiment_export(f.output)[1][-1]
    assert record["source_sha256"] == file_sha256(original)
    assert record["input_image_sha256"] == copy_hash
    assert record["copy_provenance"] == extra["copy_provenance"]


@pytest.mark.parametrize("kind", ["missing", "wrong_original", "wrong_copy", "resize", "wrong_transform"])
def test_derived_copy_cannot_hide_an_arbitrary_image_transform(experiment_fixture, kind):
    f = experiment_fixture
    good, bad = f.extra(), f.extra(1)
    copied = Path(bad["image_path"]).with_suffix(".jpg")
    rgb = read_rgb(bad["image_path"])
    if kind == "resize":
        rgb = cv2.resize(rgb, (820, 295))
    write_jpg(copied, rgb, 97)
    copy_hash = file_sha256(copied)
    provenance = {"original_sha256": bad["record"]["source_sha256"], "copy_sha256": copy_hash,
                  "transform": "full_resolution_jpeg_q97", "width": 1640, "height": 590}
    if kind == "wrong_original":
        provenance["original_sha256"] = "b" * 64
    elif kind == "wrong_copy":
        provenance["copy_sha256"] = "b" * 64
    elif kind == "wrong_transform":
        provenance["transform"] = "resized_crop"
    bad.update(image_path=copied, image_sha256=copy_hash)
    if kind != "missing":
        bad["copy_provenance"] = provenance
    assert f.build([good, bad])["extra_train_images"] == 1


def test_experimental_opt_in_and_report_are_explicit_and_production_defaults_are_unchanged(settings, tmp_path):
    normal = parse_arguments([])
    production = settings.path("reports", "ufld_training.json").resolve()
    assert training_report_path(normal, settings, tmp_path / "normal") == production
    with pytest.raises(SystemExit):
        parse_arguments(["--experimental-data"])
    args = parse_arguments(["--experimental-data", "--prepared-run", str(tmp_path / "experiment")])
    assert training_report_path(args, settings, args.prepared_run) == (args.prepared_run / "experiment_training.json").resolve()
    args.status_report = production
    with pytest.raises(ValueError, match="production status"):
        training_report_path(args, settings, args.prepared_run)
    args.status_report = args.prepared_run / "dataset/manifest.json"
    with pytest.raises(ValueError, match="frozen dataset"):
        training_report_path(args, settings, args.prepared_run)
    args.status_report = tmp_path / "isolated-report.json"
    assert training_report_path(args, settings, args.prepared_run) == args.status_report.resolve()
