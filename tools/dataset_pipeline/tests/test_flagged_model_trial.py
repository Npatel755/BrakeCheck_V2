"""Isolation, replica provenance and train-only proposal selection."""
import json
import hashlib
from pathlib import Path
import sqlite3

import cv2
import numpy as np
import pytest

from bcdata.bootstrap import atomic_json
from bcdata.flagged_model_trial import (ReadOnlyRegistry, materialize_copies, proposal_quality,
                                      selected_extras, recover_partial_teacher, assigned_split_inventory)
from bcdata.local import local_settings
from bcdata.teachers.base import file_sha256


def test_readonly_registry_cannot_update_original(tmp_path):
    path = tmp_path / "original.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE images(image_id TEXT,split TEXT)")
        connection.execute("INSERT INTO images VALUES('original','train')")
    before = file_sha256(path)
    registry = ReadOnlyRegistry(path)
    assert registry.images()[0]["image_id"] == "original"
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        registry.conn.execute("UPDATE images SET split='test'")
    registry.close()
    assert file_sha256(path) == before


def test_replicas_preserve_originals_dimensions_and_frozen_geometry(tmp_path):
    run = tmp_path / "experiment"
    run.mkdir()
    source = tmp_path / "original.png"
    pixels = np.arange(96 * 160 * 3, dtype=np.uint8).reshape(96, 160, 3)
    source.write_bytes(cv2.imencode(".png", pixels)[1].tobytes())
    original_sha = file_sha256(source)
    geometry_bytes = b'{"lanes": [], "width": 160, "height": 96}\n'
    atomic_json(run / "inventory.json", {"items": [{
        "record": {"image_id": "original-1", "path": str(source), "sha256": original_sha,
                   "source": "a2d2", "session_id": "train-drive", "split": "train"},
        "original_geometry_sha256": hashlib.sha256(geometry_bytes).hexdigest(),
        "original_geometry_bytes_hex": geometry_bytes.hex()}]})
    items = materialize_copies(run, lambda **value: None)
    assert file_sha256(source) == original_sha
    item = items[0]
    assert item["copy_sha256"] != original_sha
    assert item["copy_provenance"]["transform"] == "full_resolution_jpeg_q97"
    assert cv2.imdecode(np.frombuffer((run / item["image_path"]).read_bytes(), np.uint8), 1).shape == (96, 160, 3)
    assert (run / item["original_geometry_path"]).read_bytes() == geometry_bytes
    # Resume verifies the independent file; it never rewrites the original.
    assert materialize_copies(run, lambda **value: None) == items
    (run / item["image_path"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="copy changed"):
        materialize_copies(run, lambda **value: None)


def test_teacher_empty_uncertain_or_flip_unstable_not_training_truth():
    paint = np.zeros((100, 100), np.float32)
    paint[20:90, 44:48] = .95
    lane = {"slot": 2, "points": [[46, 89, True], [46, 20, True]],
            "mean_confidence": .95, "y_top": 20, "y_bottom": 89, "flags": []}
    assert proposal_quality({"lanes": [lane]}, paint, paint)["eligible_for_experiment"]
    assert not proposal_quality({"lanes": []}, paint * 0, paint * 0)["eligible_for_experiment"]
    assert not proposal_quality({"lanes": [lane]}, paint, paint * 0)["eligible_for_experiment"]
    assert not proposal_quality({"lanes": [{**lane, "mean_confidence": .6}]}, paint, paint)["eligible_for_experiment"]
    assert not proposal_quality({"lanes": [{**lane, "points": [[-2, 89], [46, 20]]}]}, paint, paint)["eligible_for_experiment"]


def test_only_train_quality_pass_unskipped_copies_are_selected(tmp_path):
    run = tmp_path / "experiment"
    run.mkdir()
    items = []
    for index, (split, good) in enumerate([("train", True), ("val", True), ("test", True), ("train", False), ("train", True)]):
        copy_id = f"copy-{index}"
        item = {"copy_id": copy_id, "split": split, "source": "comma10k",
                "width": 160, "height": 100,
                "record": {"image_id": str(index)}, "image_path": "copies/images/" + copy_id + ".jpg",
                "copy_sha256": "c" * 64, "original_sha256": "a" * 64,
                "copy_provenance": {"transform": "full_resolution_jpeg_q97"}}
        items.append(item)
        atomic_json(run / "proposals" / (copy_id + ".json"), {
            "geometry": {"width": 160, "height": 100,
                         "lanes": [{"slot": 2, "physical": True, "points": [[40, 80], [50, 20]]}]},
            "quality": {"eligible_for_experiment": good,
                        "reasons": [] if good else ["low_paint_confidence"]}})
    atomic_json(run / "copies.json", {"items": items})
    with sqlite3.connect(run / "review.sqlite") as con:
        con.execute("CREATE TABLE decisions(copy_id TEXT,action TEXT)")
        con.execute("INSERT INTO decisions VALUES('copy-4','skip')")
    extras, report = selected_extras(local_settings(tmp_path / "dataset"), run)
    assert [d["record"]["image_id"] for d in extras] == ["0"]
    assert extras[0]["crop_config"]["y0"] == 240
    assert report["excluded"] == {"held_out_comparison_only": 2, "proposal_quality_failed": 1,
                                  "experimental_reviewer_excluded_or_skipped": 1}
    assert report["manual_approval_required"] is False
    assert report["automatically_selected"] == 1
    assert report["human_override_selected"] == 0


def test_manual_soft_qc_override_matches_the_current_proposal_and_cannot_admit_empty_predictions(tmp_path):
    run = tmp_path / "experiment"
    run.mkdir()
    items = []
    for index in range(3):
        copy_id = f"copy-{index}"
        item = {"copy_id": copy_id, "split": "train", "source": "comma10k", "width": 160, "height": 100,
                "record": {"image_id": str(index)}, "image_path": f"copies/{copy_id}.jpg",
                "copy_sha256": "a" * 64, "original_sha256": "a" * 64}
        items.append(item)
        atomic_json(run / "proposals" / f"{copy_id}.json", {
            "geometry": {"width": 160, "height": 100,
                         "lanes": [{"slot": 2, "points": [[40, 80], [50, 20]]}] if index < 2 else []},
            "quality": {"eligible_for_experiment": False, "reasons": ["low_paint_confidence"]}})
    atomic_json(run / "copies.json", {"items": items})
    with sqlite3.connect(run / "review.sqlite") as con:
        con.execute("CREATE TABLE decisions(copy_id TEXT, action TEXT, proposal_sha256 TEXT)")
        for index in range(3):
            digest = file_sha256(run / "proposals" / f"copy-{index}.json") if index != 1 else "f" * 64
            con.execute("INSERT INTO decisions VALUES(?,?,?)", (f"copy-{index}", "approve", digest))
    extras, report = selected_extras(local_settings(tmp_path / "dataset"), run)
    assert [d["record"]["image_id"] for d in extras] == ["0"]
    assert report["human_override_selected"] == 1
    assert report["excluded"] == {"proposal_quality_failed": 2}


def test_duplicate_boundary_fragment_slots_stay_out_of_automatic_future_selection(tmp_path):
    run = tmp_path / "experiment"
    run.mkdir()
    item = {"copy_id": "copy-0", "split": "train", "source": "comma10k", "width": 64, "height": 32,
            "record": {"image_id": "0"}, "image_path": "copies/copy-0.jpg",
            "copy_sha256": "a" * 64, "original_sha256": "a" * 64}
    atomic_json(run / "copies.json", {"items": [item]})
    path = run / "proposals/copy-0.json"
    atomic_json(path, {"geometry": {"width": 64, "height": 32, "lanes": [
        {"slot": 1, "points": [[10, 10, True], [12, 15, True]]},
        {"slot": 2, "points": [[14, 20, True], [18, 30, True]]}]},
        "quality": {"eligible_for_experiment": True, "reasons": []}})
    original_bytes = path.read_bytes()
    settings = local_settings(tmp_path / "dataset")
    extras, report = selected_extras(settings, run)
    assert extras == []
    assert report["excluded"] == {"proposal_geometry_ambiguous": 1}
    assert report["manual_approval_required"] is False
    assert path.read_bytes() == original_bytes
    with sqlite3.connect(run / "review.sqlite") as con:
        con.execute("CREATE TABLE decisions(copy_id TEXT, action TEXT, proposal_sha256 TEXT)")
        con.execute("INSERT INTO decisions VALUES(?,?,?)", ("copy-0", "approve", file_sha256(path)))
    extras, report = selected_extras(settings, run)
    assert len(extras) == 1 and report["human_override_selected"] == 1
    assert path.read_bytes() == original_bytes


def test_partial_teacher_recovery_preserves_uncommitted_files_only(tmp_path):
    teacher = tmp_path / "teacher"
    partial = teacher / "source_cohort/training_cohort.json"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"frozen original-source manifest")
    archived = Path(recover_partial_teacher(tmp_path))
    assert archived.parent == tmp_path
    assert (archived / "source_cohort/training_cohort.json").read_bytes() == b"frozen original-source manifest"
    assert not teacher.exists()
    atomic_json(teacher / "experiment.json", {"state": "prepared"})
    assert recover_partial_teacher(tmp_path) is None


def test_unsplit_rejected_inputs_do_not_block_the_experiment_or_change_frozen_inventory():
    records = [{"image_id": "good", "split": "train", "status": "curated"},
               {"image_id": "held", "split": "test", "status": "rejected"},
               {"image_id": "black-frame", "split": None, "status": "rejected"}]
    before = json.dumps(records, sort_keys=True)
    assigned, excluded = assigned_split_inventory(records)
    assert [r["image_id"] for r in assigned] == ["good", "held"]
    assert excluded == [{"image_id": "black-frame", "status": "rejected",
                         "reason": "rejected_before_split_assignment"}]
    assert json.dumps(records, sort_keys=True) == before


@pytest.mark.parametrize("status,split", [("curated", None), ("downloaded", None),
                                          (None, None), ("rejected", "trian")])
def test_unsplit_eligible_or_corrupted_inventory_still_fails(status, split):
    with pytest.raises(ValueError, match="eligible inventory image"):
        assigned_split_inventory([{"image_id": "must-not-disappear", "status": status, "split": split}])
