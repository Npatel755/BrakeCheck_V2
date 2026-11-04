import json
from pathlib import Path

import numpy as np
import pytest

from bcdata.quality import write_png
from bcdata.source_mask_training import freeze_source_cohort, load_source_cohort
from bcdata.teachers.base import file_sha256
from tests.test_pipeline import build


def prepare(settings, registry, licenses):
    build(settings, registry, licenses, n=4)
    for i, row in enumerate(registry.images()):
        registry.update_image(row["image_id"], status="curated", split="val" if i == 3 else "train")
    folder = settings.path("licenses")
    folder.mkdir(exist_ok=True)
    (folder / "comma10k-LICENSE.txt").write_text("Synthetic MIT licence fixture", encoding="utf-8")


def test_freeze_original_records_not_latest_registry(settings, registry, licenses, tmp_path):
    prepare(settings, registry, licenses)
    out = tmp_path / "frozen"
    manifest = freeze_source_cohort(settings, registry, out)
    loaded, pairs = load_source_cohort(out / "training_cohort.json")
    assert loaded == manifest and len(pairs["train"]) == 3 and len(pairs["val"]) == 1
    row = manifest["cohorts"]["train"][0]
    assert row["source_annotation"]["id"] and row["source_sha256"]
    assert manifest["licenses"]["comma10k"]["files"][0]["sha256"]
    original = pairs["train"][0][1]().copy()
    fake = tmp_path / "new-source.png"
    write_png(fake, np.zeros_like(original))
    registry.add_annotation(row["image_id"], "source_label", "comma10k", "later", str(fake), ["comma10k"],
                            params={"human": True, "format": "class_map"})
    assert np.array_equal(pairs["train"][0][1](), original)
    with pytest.raises(FileExistsError):
        freeze_source_cohort(settings, registry, out)


def test_excludes_dropped_rejected_test_and_nonhuman(settings, registry, licenses, tmp_path):
    build(settings, registry, licenses, n=6)
    license_dir = settings.path("licenses")
    license_dir.mkdir(exist_ok=True)
    (license_dir / "comma10k-LICENSE.txt").write_text("MIT fixture")
    ids = [r["image_id"] for r in registry.images()]
    for index, image_id in enumerate(ids):
        registry.update_image(image_id, status="curated", split="val" if index == 5 else "train")
    registry.update_image(ids[0], status="dropped")
    registry.update_image(ids[1], review_status="rejected")
    registry.update_image(ids[2], split="test")
    old = registry.annotation(ids[3], "source_label")
    registry.add_annotation(ids[3], "source_label", "comma10k", "fake", old["path"], ["comma10k"],
                            params={"human": False, "format": "class_map"})
    manifest = freeze_source_cohort(settings, registry, tmp_path / "cohort")
    assert {r["image_id"] for rows in manifest["cohorts"].values() for r in rows} == {ids[4], ids[5]}


def test_cross_split_drive_rejected_including_test(settings, registry, licenses, tmp_path):
    prepare(settings, registry, licenses)
    rows = registry.images()
    registry.update_image(rows[-1]["image_id"], session_id=rows[0]["session_id"], split="test")
    with pytest.raises(RuntimeError, match="session leakage"):
        freeze_source_cohort(settings, registry, tmp_path / "cohort")


def test_sha_and_manifest_tampering_rejected(settings, registry, licenses, tmp_path):
    prepare(settings, registry, licenses)
    out = tmp_path / "cohort"
    manifest = freeze_source_cohort(settings, registry, out)
    _, pairs = load_source_cohort(out / "training_cohort.json")
    row = manifest["cohorts"]["train"][0]
    Path(row["source_annotation"]["path"]).write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="SHA mismatch"):
        pairs["train"][0][1]()
    manifest["cohorts"]["train"][0]["split"] = "val"
    (out / "training_cohort.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest SHA"):
        load_source_cohort(out / "training_cohort.json")


def test_requires_license_and_original_source(settings, registry, licenses, tmp_path):
    prepare(settings, registry, licenses)
    settings.path("licenses", "comma10k-LICENSE.txt").unlink()
    with pytest.raises(RuntimeError, match="licence snapshot missing"):
        freeze_source_cohort(settings, registry, tmp_path / "missing")
    settings.path("licenses", "comma10k-LICENSE.txt").write_text("MIT")
    image = registry.images()[0]
    ann = registry.annotation(image["image_id"], "source_label")
    registry.add_annotation(image["image_id"], "source_label", "model", "fake", ann["path"], ["comma10k"],
                            params={"human": True, "format": "class_map"})
    with pytest.raises(RuntimeError, match="original dataset source"):
        freeze_source_cohort(settings, registry, tmp_path / "fake")


def test_audi_original_provenance_and_palette_binding(settings, registry, licenses, tmp_path):
    prepare(settings, registry, licenses)
    row = registry.images()[0]
    source = registry.annotation(row["image_id"], "source_label")
    palette = tmp_path / "class_list.json"
    palette.write_text('{"#ffc125":"Solid line"}')
    provenance = tmp_path / "original.json"
    provenance.write_text(json.dumps({"camera": "front_center", "originals_unchanged": True,
        "palette_sha256": file_sha256(palette), "files": {
            "img": {"sha256": file_sha256(row["path"])}, "mask": {"sha256": file_sha256(source["path"])}}}))
    registry.update_image(row["image_id"], source="a2d2", license_id="a2d2")
    registry.add_annotation(row["image_id"], "source_label", "a2d2", "original", source["path"], ["a2d2"],
                            params={"human": True, "format": "a2d2_rgb", "palette_path": str(palette),
                                    "palette_sha256": file_sha256(palette), "provenance_path": str(provenance),
                                    "provenance_sha256": file_sha256(provenance)})
    settings.path("licenses", "a2d2-LICENSE.txt").write_text("CC BY-ND fixture")
    settings.path("licenses", "a2d2-NOTICE.txt").write_text("Audi attribution fixture")
    manifest = freeze_source_cohort(settings, registry, tmp_path / "audi")
    frozen = next(r for r in manifest["cohorts"]["train"] if r["source"] == "a2d2")
    assert frozen["assets"]["provenance"]["sha256"] and frozen["palette_sha256"]
    assert manifest["distribution_scope"] == "private_local_development"
    assert len(manifest["licenses"]["a2d2"]["files"]) == 2
    Path(source["path"]).write_bytes(b"nonoriginal mask")
    with pytest.raises(RuntimeError, match="pinned original image/mask provenance"):
        freeze_source_cohort(settings, registry, tmp_path / "tampered")
