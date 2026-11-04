"""Isolated semantic teacher uses trusted BASE TRAIN only and never writes DB labels."""
import json
from pathlib import Path

import numpy as np
import pytest

from bcdata import annotation_training as training
from bcdata import flagged_teacher_experiment as experiment
from bcdata.bootstrap import atomic_json
from bcdata.quality import write_png
from bcdata.source_mask_training import freeze_source_cohort
from bcdata.teachers.base import file_sha256
from tests.test_pipeline import build


def row(identifier, split="train", session="drive-a", source="a2d2"):
    return {"image_id": identifier, "split": split, "session_id": session, "source": source}


def test_parent_ids_and_drives_cannot_enter_either_heldout_split():
    base = [row("base", session="train-drive"), row("val", "val", "val-drive")]
    assert experiment.validate_training_ancestry([base[0]], base)["test_images_used"] == 0
    for bad in ([row("val", session="unrelated")], [row("different", session="val-drive")],
                [row("test", session="train-drive")]):
        with pytest.raises(ValueError, match="held-out"):
            experiment.validate_training_ancestry(bad, base, reserved_test_ids=["test"])
    with pytest.raises(ValueError, match="held-out"):
        experiment.validate_training_ancestry([row("different", session="test-drive")], base,
            test_records=[row("test", "test", "test-drive")])


def test_parent_flagged_train_ancestry_is_retained_explicitly():
    base = [row("new-base", session="train"), row("val", "val", "heldout")]
    old = row("prior-flagged", session="other-train")
    result = experiment.validate_training_ancestry([old, base[0]], base)
    assert result["training_image_ids"] == ["new-base", "prior-flagged"]
    assert result["training_session_ids"] == ["other-train", "train"]
    assert result["validation_training_overlap"] == 0


@pytest.fixture
def world(settings, registry, licenses, tmp_path):
    build(settings, registry, licenses, n=6)
    originals = registry.images()
    for index, raw in enumerate(originals):
        registry.update_image(raw["image_id"], status="curated", split="test" if index == 5 else "val" if index == 4 else "train")
    validation = originals[4]
    ann = registry.annotation(validation["image_id"], "source_label")
    registry.update_image(validation["image_id"], source="a2d2", license_id="a2d2")
    registry.add_annotation(validation["image_id"], "source_label", "a2d2", "original", ann["path"], ["a2d2"],
                            params={"human": True, "format": "class_map"})
    settings.path("licenses").mkdir(exist_ok=True)
    settings.path("licenses", "comma10k-LICENSE.txt").write_text("Synthetic MIT fixture")
    settings.path("licenses", "a2d2-LICENSE.txt").write_text("Synthetic Audi licence fixture")
    settings.path("licenses", "a2d2-NOTICE.txt").write_text("Synthetic Audi attribution")
    all_rows = [dict(r) for r in registry.images()]
    parent = tmp_path / "parent"
    freeze_source_cohort(settings, registry, parent)
    config = {"cohort_sha256": file_sha256(parent / "training_cohort.json"),
              "model_provenance": {"hub_id": training.MODEL, "revision": training.REVISION}}
    atomic_json(parent / "config.json", config)
    checkpoint = parent / "best.pt"
    checkpoint.write_bytes(b"trusted synthetic parent bytes")
    selected = [all_rows[0], all_rows[1], next(r for r in all_rows if r["source"] == "a2d2")]
    records = [{"image_id": r["image_id"], "source": r["source"], "session_id": r["session_id"],
                "split": r["split"], "source_sha256": r["sha256"], "decision": "approved", "pool": "commercial"}
               for r in selected]
    base = tmp_path / "base-run"
    snap = base / "dataset"
    snap.mkdir(parents=True)
    atomic_json(snap / "manifest.json", records)
    atomic_json(snap / "culane_anno_cache.json", {})
    atomic_json(snap / "hashes.json", {name: file_sha256(snap / name)
                                        for name in ("manifest.json", "culane_anno_cache.json")})
    atomic_json(snap / "export_marker.json", {"revision": 123, "errors": [], "validation": {"ok": True}})
    atomic_json(snap / "reserved_test_ids.json", [r["image_id"] for r in all_rows if r["split"] == "test"])
    return settings, registry, base, checkpoint, all_rows


def test_prepare_pins_only_base_and_preserves_every_registry_row(world, tmp_path):
    settings, registry, base, checkpoint, all_rows = world
    before = list(registry.conn.iterdump())
    changes = registry.conn.total_changes
    progress = []
    out = tmp_path / "teacher"
    prepared = experiment.prepare_teacher(settings, registry, base, out, checkpoint, progress=progress.append)
    assert registry.conn.total_changes == changes
    assert list(registry.conn.iterdump()) == before
    assert prepared["coverage"]["teacher_train"] == 2
    assert prepared["coverage"]["teacher_val"] == 1
    assert prepared["local_parent_outside_new_base_training"] == 2
    assert (out / "parent_initial.pt").read_bytes() == checkpoint.read_bytes()
    assert prepared["original_registry_writes"] is False and prepared["test_images_used"] == 0
    assert progress[0]["phase"] == "validating_parent" and progress[-1]["phase"] == "prepared"
    _, cohort = experiment.load_prepared(out)
    assert len(cohort["cohorts"]["train"]) == 2
    assert {r["image_id"] for r in cohort["cohorts"]["train"]} == set(prepared["new_training_image_ids"])
    assert all(r["split"] != "test" for rows in cohort["cohorts"].values() for r in rows)
    assert not list(out.rglob("*.sqlite"))
    with pytest.raises(FileExistsError):
        experiment.prepare_teacher(settings, registry, base, out, checkpoint)


def test_preparation_rejects_changed_base_source_identity(world, tmp_path):
    settings, registry, base, checkpoint, rows = world
    frozen = json.loads((base / "dataset/manifest.json").read_text())[0]
    registry.update_image(frozen["image_id"], session_id="changed-drive")
    with pytest.raises(ValueError, match="identity/split/session"):
        experiment.prepare_teacher(settings, registry, base, tmp_path / "teacher", checkpoint)


def test_changed_checkpoint_and_source_bytes_fail_before_fit(world, tmp_path):
    settings, registry, base, checkpoint, _ = world
    out = tmp_path / "teacher"
    experiment.prepare_teacher(settings, registry, base, out, checkpoint)
    saved = (out / "parent_initial.pt").read_bytes()
    (out / "parent_initial.pt").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="input changed"):
        experiment.load_prepared(out)
    (out / "parent_initial.pt").write_bytes(saved)
    cohort = json.loads((out / "cohort/training_cohort.json").read_text())
    source = Path(cohort["cohorts"]["train"][0]["source_annotation"]["path"])
    source.write_bytes(b"changed source")
    with pytest.raises(RuntimeError, match="SHA mismatch"):
        experiment.load_prepared(out)


def test_all_unknown_masks_do_not_become_negative_training_targets(world, tmp_path):
    settings, registry, base, checkpoint, _ = world
    r = json.loads((base / "dataset/manifest.json").read_text())[0]
    annotation = registry.annotation(r["image_id"], "source_label")
    import cv2
    pixels = cv2.imread(annotation["path"], cv2.IMREAD_UNCHANGED)
    write_png(Path(annotation["path"]), np.zeros(pixels.shape[:2], np.uint8))
    out = tmp_path / "teacher"
    prepared = experiment.prepare_teacher(settings, registry, base, out, checkpoint)
    assert prepared["coverage"]["teacher_train"] == 1
    assert prepared["coverage"]["skipped_all_unknown"] == [{"image_id": r["image_id"], "split": "train"}]


def test_worse_or_equal_adaptation_keeps_exact_parent_checkpoint(tmp_path):
    comparison = {"baseline": {"a2d2": {"paint_iou": .7}},
                  "candidate": {"a2d2": {"paint_iou": .69}}, "checkpoint": str(tmp_path / "adaptation/best.pt")}
    selected, improved = experiment.select_checkpoint(tmp_path, comparison)
    assert not improved and selected == tmp_path / "parent_initial.pt"
    comparison["candidate"]["a2d2"]["paint_iou"] = .7
    assert not experiment.select_checkpoint(tmp_path, comparison)[1]
    comparison["candidate"]["a2d2"]["paint_iou"] = .71
    assert experiment.select_checkpoint(tmp_path, comparison) == (tmp_path / "adaptation/best.pt", True)


def test_real_cpu_adaptation_warm_starts_and_keeps_training_isolated(world, tmp_path, monkeypatch):
    torch = pytest.importorskip("torch")
    settings, registry, base, checkpoint, _ = world
    torch.set_num_threads(1); torch.manual_seed(4)
    model = torch.nn.Conv2d(3, 5, 1)
    initial = {name: value.clone() for name, value in model.state_dict().items()}
    parent_config = json.loads((checkpoint.parent / "config.json").read_text())
    torch.save({"model": initial, "config": parent_config, "test_images_used": 0,
                "baseline_revision": training.REVISION}, checkpoint)
    out = tmp_path / "teacher"
    prepared = experiment.prepare_teacher(settings, registry, base, out, checkpoint)
    # Tiny dimensions shorten this real optimizer smoke test, without changing
    # production's pinned 1024x512/eight-epoch configuration.
    prepared["config"].update(epochs=2, width=32, height=32, workers=0, requested_batch_size=2)
    prepared.pop("manifest_sha256")
    prepared["manifest_sha256"] = experiment._digest(prepared)
    atomic_json(out / "experiment.json", prepared)
    monkeypatch.setattr(training, "cached_model", lambda: (torch.nn.Conv2d(3, 5, 1), {"license": "MIT", "fixture": True}))
    before = list(registry.conn.iterdump())
    result = experiment.train_teacher(out, device="cpu")
    assert list(registry.conn.iterdump()) == before
    assert result["candidate_only"] and not result["auto_promote"]
    assert result["test_images_used"] == 0 and result["checkpoint_sha256"]
    assert result["training_image_ids"] == prepared["training_ancestry"]["training_image_ids"]
    assert result["training_records"] == prepared["training_ancestry"]["training_records"]
    assert {row["image_id"] for row in result["training_records"]} == set(result["training_image_ids"])
    assert result["comparison"]["trained_steps"] == 2
    changed = torch.load(out / "adaptation/last.pt", weights_only=True)["model"]
    assert any(not torch.equal(initial[name], value) for name, value in changed.items())
    parent = torch.load(out / "parent_initial.pt", weights_only=True)["model"]
    assert all(torch.equal(initial[name], value) for name, value in parent.items())
    assert json.loads((out / "status.json").read_text())["phase"] == "complete_awaiting_review"


def test_training_record_uses_image_hash_not_semantic_mask_hash():
    original = {"image_id": "one", "source": "a2d2", "session_id": "train", "split": "train",
                "sha256": "a" * 64, "source_sha256": "b" * 64,
                "assets": {"image": {"sha256": "a" * 64}, "source": {"sha256": "b" * 64}}}
    records = experiment.training_records([original, original])
    assert len(records) == 1 and records[0]["source_sha256"] == "a" * 64
    missing_image = {key: value for key, value in original.items() if key not in ("assets", "sha256")}
    with pytest.raises(ValueError, match="IMAGE SHA256"):
        experiment.training_records([missing_image])


def test_partial_preparation_fails_clearly_without_replacing_frozen_artifact(world, tmp_path):
    settings, registry, base, checkpoint, _ = world
    out = tmp_path / "teacher"
    marker = out / "cohort/training_cohort.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("keep exact incomplete frozen artifact")
    with pytest.raises(FileExistsError, match="fresh output"):
        experiment.prepare_teacher(settings, registry, base, out, checkpoint)
    assert marker.read_text() == "keep exact incomplete frozen artifact"
