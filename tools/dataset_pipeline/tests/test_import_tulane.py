import io
import json
from pathlib import Path

from PIL import Image
import pytest

from bcdata.import_tulane import (PINNED, SLOTS, atomic_bytes, convert_record, download_url,
    image_id, import_lock, json_bytes, make_audits, native_geometry, run_import,
    sha256, source_records, validate_raw_file, verify_jpeg)


def native(split="train", frame=0):
    ys = list(range(160, 720, 10))
    return {"raw_file": f"{split}/source/Town04/straight/{frame:04d}_image.jpg", "h_samples": ys,
            "lanes": [[400] * len(ys), [800] * len(ys), [200] * len(ys), [1000] * len(ys)]}


def jpg(color="navy", size=(1280, 720)):
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, format="JPEG")
    return stream.getvalue()


def fixtures(output, train=4, val=2):
    expected = {}
    for split, count in (("train", train), ("val", val)):
        filename = "source_" + split + ".json"
        raw = b"".join(json_bytes(native(split, index)) for index in range(count))
        atomic_bytes(output / "upstream" / filename, raw)
        expected[split] = (filename, count, sha256(raw))
    return expected


@pytest.mark.parametrize("path", ["train/target/Town04/straight/0000_image.jpg", "../train/source/Town04/straight/0000_image.jpg",
    "/train/source/Town04/straight/0000_image.jpg", "train/source/../straight/0000_image.jpg",
    "train\\source\\Town04\\straight\\0000_image.jpg", "C:/train/source/Town04/straight/0000_image.jpg",
    "train/source/Town04/straight/0000_image.jpg:evil", "val/source/Town04/straight/0000_image.jpg",
    "train/source/Town04//straight/0000_image.jpg", "train/source/Town04/straight/../../secret.jpg"])
def test_source_scope_rejects_target_and_traversal(path):
    with pytest.raises(ValueError):
        validate_raw_file(path, "train")


def test_slot_mapping_keeps_original_samples_and_native_absences():
    value = native()
    value["lanes"][0][:3] = [-2] * 3
    geometry, reasons = native_geometry(value, "train")
    lanes = {row["slot"]: row for row in geometry["lanes"]}
    assert [lanes[slot]["points"][0][0] for slot in (1, 2, 3, 4)] == [200, 400, 800, 1000]
    assert lanes[2]["y_top"] == 190
    assert len(lanes[2]["points"]) == 53
    assert lanes[2]["fit"]["ys"] == list(range(190, 720, 10))
    assert reasons == []
    assert "not_verified_visible_paint" in geometry["annotation_policy"]


def test_native_gap_and_outside_sample_are_reported_without_clamping_or_filling():
    value = native()
    value["lanes"][1][4] = -2
    value["lanes"][3][-1] = 1280
    geometry, reasons = native_geometry(value, "train")
    lanes = {row["slot"]: row for row in geometry["lanes"]}
    assert reasons == ["native_interior_missing_samples", "native_point_at_right_image_edge"]
    assert 200 not in lanes[3]["fit"]["ys"]
    assert len(lanes[3]["segments"]) == 2
    assert lanes[3]["missing_sample_ys"] == [200]
    assert lanes[4]["points"][-1] == [1000, 700]
    assert value["lanes"][3][-1] == 1280


def test_singleton_native_lane_is_retained_as_native_not_invalid_drawable_geometry():
    value = native()
    value["lanes"][0] = [-2] * 55 + [400]
    geometry, reasons = native_geometry(value, "train")
    assert "native_lane_has_fewer_than_two_points" in reasons
    assert 2 not in {lane["slot"] for lane in geometry["lanes"]}
    assert value["lanes"][0][-1] == 400


@pytest.mark.parametrize("mutation", [lambda value: value["lanes"].pop(),
    lambda value: value["h_samples"].pop(), lambda value: value["lanes"][0].__setitem__(0, -1),
    lambda value: value["lanes"][0].__setitem__(0, 1281), lambda value: value["lanes"][0].__setitem__(0, True)])
def test_malformed_native_labels_are_not_converted_to_empty_labels(mutation):
    value = native()
    mutation(value)
    with pytest.raises(ValueError):
        native_geometry(value, "train")


def test_native_annotation_fingerprint_is_required(tmp_path):
    expected = fixtures(tmp_path)
    (tmp_path / "upstream/source_train.json").write_bytes(b"bad\n")
    with pytest.raises(ValueError, match="SHA256"):
        source_records(tmp_path / "upstream", expected=expected)


def test_resume_verifies_jpeg_and_receipt_preserves_exact_native_record(tmp_path):
    expected = fixtures(tmp_path)
    record = source_records(tmp_path / "upstream", expected=expected)[0]
    image = jpg()
    calls = []
    def download(url):
        calls.append(url)
        return image
    first = convert_record(tmp_path, record, fetcher=download)
    second = convert_record(tmp_path, record, fetcher=download)
    assert first == second and len(calls) == 1
    assert (tmp_path / first["source_label"]).read_bytes() == record["native_line"]
    assert first["source_label_sha256"] == sha256(record["native_line"])
    assert first["source_sha256"] == sha256(image)
    assert "target" not in calls[0] and "datasetVersionNumber=2" in calls[0]
    (tmp_path / first["image"]).write_bytes(b"broken image")
    repaired = convert_record(tmp_path, record, fetcher=download)
    assert len(calls) == 2 and repaired["source_sha256"] == first["source_sha256"]
    (tmp_path / first["image"]).write_bytes(jpg("red"))
    convert_record(tmp_path, record, fetcher=download)
    assert len(calls) == 3  # Valid JPEG with changed bytes is still a fingerprint failure.


def test_bad_dimensions_never_publish_image_geometry_or_label(tmp_path):
    expected = fixtures(tmp_path)
    record = source_records(tmp_path / "upstream", expected=expected)[0]
    with pytest.raises(ValueError, match="1280"):
        convert_record(tmp_path, record, fetcher=lambda url: jpg(size=(640, 360)))
    assert not list(tmp_path.glob("geometry/*.json"))
    assert not list(tmp_path.glob("labels/records/*.json"))


def test_truncated_jpeg_with_valid_dimensions_is_rejected():
    with pytest.raises(OSError):
        verify_jpeg(jpg()[:-200])


def test_audits_cover_every_group_and_spread_over_time(tmp_path):
    expected = fixtures(tmp_path, train=8, val=3)
    records = source_records(tmp_path / "upstream", expected=expected)
    rows = [{key: row[key] for key in ("image_id", "raw_file", "split", "group_key", "qc_reasons")} for row in records]
    audits = make_audits(rows)
    assert len(audits["groups"]) == 2
    assert sum(group["count"] for group in audits["groups"]) == 11
    assert audits["groups"][0]["sample_ids"] == [image_id(native("train", frame)["raw_file"]) for frame in (0, 3, 7)]
    assert all(len(group["sample_ids"]) == 3 for group in audits["groups"])
    assert audits["eligible_train_images"] == 0


def test_offline_full_chain_is_isolated_and_final_manifest_immutable(tmp_path):
    production = tmp_path / "registry.sqlite"
    production.write_bytes(b"untouched production database sentinel")
    output = tmp_path / "experiments/simulated_tulane"
    expected = fixtures(output)
    result = run_import(output, workers=2, expected=expected, fetcher=lambda url: jpg(), with_notices=False)
    assert result["state"] == "awaiting_audit"
    assert result["train_images"] == 4 and result["val_images"] == 2
    assert result["downloaded"] == result["converted"] == result["total_expected"] == 6
    assert result["eligible_train_images"] == 0 and result["training_started"] is False
    rows = [json.loads(line) for line in (output / "manifest.jsonl").read_bytes().splitlines()]
    assert len(rows) == 6 and all(row["source"] == "carlane_tulane" and row["synthetic"] for row in rows)
    assert all(row["label_origin"] == "simulator_projected_gt" for row in rows)
    assert production.read_bytes() == b"untouched production database sentinel"
    manifest = (output / "manifest.jsonl").read_bytes()
    audit = (output / "audits.json").read_bytes()
    def forbid_download(url):
        raise AssertionError("Completed resume must not download")
    assert run_import(output, expected=expected, fetcher=forbid_download, with_notices=False) == result
    assert (output / "manifest.jsonl").read_bytes() == manifest
    assert (output / "audits.json").read_bytes() == audit
    (output / "manifest.jsonl").write_bytes(manifest + b" ")
    with pytest.raises(ValueError, match="manifest changed"):
        run_import(output, expected=expected, fetcher=forbid_download, with_notices=False)


def test_partial_failure_never_exposes_partial_manifest_and_can_resume(tmp_path):
    expected = fixtures(tmp_path, train=1, val=1)
    def download(url):
        if "val%2Fsource" in url:
            raise RuntimeError("offline fixture interruption")
        return jpg()
    with pytest.raises(RuntimeError, match="failed"):
        run_import(tmp_path, expected=expected, fetcher=download, with_notices=False)
    assert not (tmp_path / "manifest.jsonl").exists()
    assert not (tmp_path / "audits.json").exists()
    assert json.loads((tmp_path / "import.json").read_bytes())["state"] == "error"
    result = run_import(tmp_path, expected=expected, fetcher=lambda url: jpg(), with_notices=False)
    assert result["converted"] == 2 and result["state"] == "awaiting_audit"


def test_another_importer_cannot_acquire_lock(tmp_path):
    with import_lock(tmp_path):
        with pytest.raises(RuntimeError, match="already running"):
            with import_lock(tmp_path):
                pass
