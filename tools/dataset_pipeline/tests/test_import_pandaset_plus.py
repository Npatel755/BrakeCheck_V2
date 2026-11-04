import io
import json
from pathlib import Path
import zipfile

from PIL import Image
import pytest

from bcdata.import_pandaset_plus import (ANNOTATION_POLICY_RISKS, DOWNLOAD, FrontImageArchive,
    atomic_bytes, convert_record, download_url, image_id, json_bytes, make_audits,
    native_geometry, official_splits, run_import, sha256, source_records, verify_jpeg)


def notebook(train=("001",), test=("006",)):
    return json_bytes({"cells": [{"cell_type": "code", "source": [
        f"train_seq_front = {list(train)!r}\n", f"test_seq_front = {list(test)!r}\n",
        "raise RuntimeError('must never execute notebook code')\n"]}]})


def native():
    return [[[200, 950], [500, 700], [820, 540]],
            [[700, 1050], [800, 800], [900, 540]],
            [[1250, 1050], [1150, 800], [1010, 540]],
            [[1800, 950], [1400, 700], [1100, 540]]]


def record(sequence="001", frame="00", lanes=None, split="train"):
    return {"image_id": image_id(sequence, frame), "sequence": sequence, "frame": frame,
            "split": split, "session_id": f"pandaset_plus/{sequence}/front_camera",
            "native_member": f"{sequence}/{frame}.json",
            "native_raw": json.dumps(native() if lanes is None else lanes, indent=3).encode()}


def jpeg(size=(1920, 1080)):
    data = io.BytesIO()
    Image.new("RGB", size, "navy").save(data, format="JPEG")
    return data.getvalue()


def fixture_upstream(tmp_path, entries):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in entries:
            info = zipfile.ZipInfo()
            info.filename = info.orig_filename = name
            archive.writestr(info, raw)
    raw = data.getvalue()
    atomic_bytes(tmp_path / "part", raw)
    atomic_bytes(tmp_path / "lane_train_test_split.ipynb", notebook())
    return (("part", len(raw), sha256(raw)),)


def test_official_split_literals_preserve_train_test_and_hold_without_execution(tmp_path):
    expected = fixture_upstream(tmp_path, [("001/00.json", b"[]"), ("006/00.json", b"[]"), ("016/00.json", b"[]")])
    rows = source_records(tmp_path, expected=expected, expected_count=3)
    assert [(r["sequence"], r["split"]) for r in rows] == [("001", "train"), ("006", "test"), ("016", "hold")]
    assert rows[2]["native_raw"] == b"[]"
    assert rows[0]["session_id"] != rows[1]["session_id"]


@pytest.mark.parametrize("value", [notebook(train=("001",), test=("001",)),
    notebook(train=("001", "001")), notebook(train=("bad",)), b'{"cells":[]}'])
def test_invalid_official_splits_are_not_silently_reassigned(value):
    with pytest.raises(ValueError):
        official_splits(value)


@pytest.mark.parametrize("member", ["../001/00.json", "/001/00.json", "001/80.json", "001/00.pkl",
    "001\\00.json", "front_camera/001/00.json", "001/../006/00.json"])
def test_unsafe_or_other_format_annotation_members_rejected(tmp_path, member):
    expected = fixture_upstream(tmp_path, [(member, b"[]")])
    with pytest.raises(ValueError, match="member"):
        source_records(tmp_path, expected=expected, expected_count=None)


def test_annotation_parts_and_count_are_pinned(tmp_path):
    expected = fixture_upstream(tmp_path, [("001/00.json", b"[]")])
    with pytest.raises(ValueError, match="count"):
        source_records(tmp_path, expected=expected, expected_count=2)
    (tmp_path / "part").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        source_records(tmp_path, expected=expected, expected_count=1)


def test_slot_mapping_preserves_original_pixels_and_does_not_extend_curves():
    original = native()
    geometry, reasons = native_geometry(original, identifier="panda-test")
    assert reasons == []
    assert [r["slot"] for r in geometry["lanes"]] == [1, 2, 3, 4]
    assert [r["native_lane_index"] for r in geometry["lanes"]] == [0, 1, 2, 3]
    for lane, raw in zip(geometry["lanes"], original):
        assert lane["points"] == sorted(raw, key=lambda p: p[1])
        assert lane["polyline_points"] == raw
        assert lane["y_bottom"] == max(p[1] for p in raw)
        assert not lane["paint_semantics_verified"]
        assert lane["physical"] is False and lane["kind"] == "unknown"
    assert geometry["annotation_policy_risks"] == ANNOTATION_POLICY_RISKS
    assert original == native()


def test_native_unknown_semantics_are_nonblocking_policy_risks():
    geometry, reasons = native_geometry(native(), identifier="panda-test")
    assert not reasons
    assert "native_visibility_unspecified" in geometry["annotation_policy_risks"]
    assert "native_boundary_type_unspecified" in geometry["annotation_policy_risks"]


def test_transverse_turning_frame_cannot_become_clean_painted_lane_training():
    # Representative observed 006/00 geometry: crossroad line from x3..1232,
    # y555..755; preserves drawable candidate but records a blocking QC issue.
    geometry, reasons = native_geometry([[[3, 755], [500, 670], [1232, 555]]], identifier="panda-test")
    assert "native_transverse_or_turning_projection" in reasons
    assert len(geometry["lanes"]) == 1


@pytest.mark.parametrize("lanes,reason", [
    ([], "native_empty_projection_not_verified_no_lanes"),
    ([[[10, 600]]], "native_lane_has_fewer_than_two_points"),
    ([[[10, 600], [20, 600]]], "native_lane_has_no_vertical_extent"),
    ([[[True, 600], [20, 700]]], "native_malformed_lane_coordinates"),
    ([[[float("nan"), 600], [20, 700]]], "native_malformed_lane_coordinates"),
    ([[[20, 600, True], [20, 700]]], "native_malformed_lane_coordinates"),
    ([[[-3, 600], [-4, 800]]], "native_lane_has_fewer_than_two_in_frame_points"),
])
def test_invalid_or_absent_lanes_remain_qc_held_but_geometry_is_reviewable(lanes, reason):
    geometry, reasons = native_geometry(lanes, identifier="panda-test")
    assert reason in reasons
    assert geometry["lanes"] == []
    assert json.loads(json_bytes(geometry))["width"] == 1920


def test_outside_points_are_not_clamped_or_drawn_and_singletons_omitted():
    geometry, reasons = native_geometry([[[700, 1080], [800, 800], [900, 600]],
                                         [[1920, 900], [1000, 600]]], identifier="panda-test")
    assert "native_points_outside_image" in reasons
    assert "native_lane_has_fewer_than_two_in_frame_points" in reasons
    assert geometry["lanes"][0]["points"] == [[900., 600.], [800., 800.]]
    assert all(0 <= x < 1920 and 0 <= y < 1080 for line in geometry["lanes"] for x, y in line["points"])


def test_folded_native_curve_and_excess_same_side_slots_are_held():
    line = [[300, 1000], [400, 700], [450, 800], [500, 550]]
    _, reasons = native_geometry([line], identifier="panda-test")
    assert "native_lane_folds_in_y" in reasons
    geometry, reasons = native_geometry([[[x, 1000], [x + 20, 550]] for x in (100, 300, 600)], identifier="panda-test")
    assert "native_more_than_two_boundaries_on_one_side" in reasons
    assert [r["slot"] for r in geometry["lanes"]] == [1, 2]


def test_id_and_download_url_are_versioned_front_only():
    assert image_id("001", "00") == "panda-0bfa2f8d9cf9f89e"
    assert image_id("001", "01") != image_id("001", "00")
    assert download_url("001", "00").endswith("PandaSet%2F001%2Fcamera%2Ffront_camera%2F00.jpg?datasetVersionNumber=1")
    for sequence, frame in (("../", "00"), ("001", "80"), ("001", "1"), ("1", "00")):
        with pytest.raises(ValueError):
            image_id(sequence, frame)


def test_archive_scope_is_checked_before_opening_transport():
    archive = FrontImageArchive([record()], reader_factory=lambda *a, **kw: pytest.fail("transport must not open"))
    bad = [DOWNLOAD + "/PandaSet%2F001%2Flidar%2F00.pkl?datasetVersionNumber=1",
           download_url("001", "01"), download_url("006", "00"), download_url("001", "00").replace("front_camera", "back_camera"),
           download_url("001", "00").replace("datasetVersionNumber=1", "datasetVersionNumber=2"),
           download_url("001", "00").replace("%2F", "/")]
    for url in bad:
        with pytest.raises(ValueError):
            archive(url)


def test_original_jpeg_and_exact_raw_json_preserved_and_resumed(tmp_path):
    source = record()
    raw = jpeg()
    calls = []
    def fetcher(url):
        calls.append(url)
        return raw
    row = convert_record(tmp_path, source, fetcher=fetcher)
    assert len(calls) == 1
    assert (tmp_path / row["image"]).read_bytes() == raw
    assert (tmp_path / row["source_label"]).read_bytes() == source["native_raw"]
    assert row["source_label"] == f"native/{source['image_id']}.json"
    assert row["source_sha256"] == sha256(raw)
    assert row["source_label_sha256"] == sha256(source["native_raw"])
    assert row["geometry_sha256"] == sha256((tmp_path / row["geometry"]).read_bytes())
    assert row["pool"] == "commercial" and row["synthetic"] is False
    assert not row["qc_reasons"]
    assert convert_record(tmp_path, source, fetcher=fetcher) == row
    assert len(calls) == 1


def test_corrupt_cached_jpeg_is_repaired_from_same_pinned_source(tmp_path):
    source = record()
    path = tmp_path / f"images/{source['image_id']}.jpg"
    atomic_bytes(path, b"partial")
    raw = jpeg()
    convert_record(tmp_path, source, fetcher=lambda url: raw)
    assert path.read_bytes() == raw


def test_wrong_dimensions_or_truncated_images_never_become_receipts():
    with pytest.raises(ValueError, match="1920"):
        verify_jpeg(jpeg((1280, 720)))
    with pytest.raises(OSError):
        verify_jpeg(jpeg()[:-500])


def test_spread_audits_cover_exactly_groups_and_never_mix_splits(tmp_path):
    rows = []
    for sequence, split in (("001", "train"), ("006", "test"), ("016", "hold")):
        for frame in ("00", "20", "40", "60", "79"):
            rows.append(convert_record(tmp_path, record(sequence, frame, split=split), fetcher=lambda url: jpeg()))
    audits = make_audits(rows)
    assert audits["schema"] == "real_lane_audits:v1"
    assert len(audits["groups"]) == 3
    samples = [value for g in audits["groups"] for value in g["sample_ids"]]
    assert len(samples) == len(set(samples)) == 9
    for group in audits["groups"]:
        matching = [r for r in rows if r["group_key"] == group["group_key"]]
        assert group["count"] == len(matching) == 5
        assert group["sample_ids"] == [matching[i]["image_id"] for i in (0, 2, 4)]
        assert all(r["split"] == group["split"] for r in matching)
    assert audits["eligible_train_images"] == 0


def test_complete_import_is_isolated_awaiting_audit_with_pinned_manifests(tmp_path):
    records = [record(frame=f"{i:02d}") for i in range(4)] + [record("006", split="test"), record("016", split="hold")]
    result = run_import(tmp_path, records=records, fetcher=lambda url: jpeg(), workers=2)
    assert result["state"] == "awaiting_audit"
    assert result["schema"] == "real_lane_import:v1" and result["license"] == "CC-BY-4.0"
    assert result["split_counts"] == {"train": 4, "test": 1, "hold": 1}
    assert result["eligible_train_images"] == 0 and result["auto_promote"] is False
    assert result["manifest_sha256"] == sha256((tmp_path / "manifest.jsonl").read_bytes())
    assert result["audits_sha256"] == sha256((tmp_path / "audits.json").read_bytes())
    assert len((tmp_path / "manifest.jsonl").read_bytes().splitlines()) == 6
    assert not (tmp_path / "registry.sqlite").exists()


def test_failed_or_limited_import_is_partial_and_does_not_claim_ready(tmp_path):
    records = [record(frame=f"{i:02d}") for i in range(2)]
    def fetcher(url):
        if "%2F01.jpg" in url:
            raise RuntimeError("offline")
        return jpeg()
    result = run_import(tmp_path, records=records, fetcher=fetcher, workers=1)
    assert result["state"] == "partial_import" and result["completed_images"] == 1
    assert result["error_count"] == 1
    assert "offline" not in (tmp_path / "errors.json").read_text()  # No possibly signed error URLs.
    result = run_import(tmp_path, records=records, fetcher=lambda url: jpeg(), workers=1, limit=1)
    assert result["state"] == "partial_import"
    assert result["total_paired_images"] == 2 and result["target_images"] == 1


def test_held_diagnostics_aggregate_exact_risk_with_split_and_provenance_kept(tmp_path):
    rows = [convert_record(tmp_path, record(s, f, lanes=[], split=split), fetcher=lambda url: jpeg())
            for s, f, split in (("001", "00", "train"), ("002", "40", "train"),
                                ("003", "79", "train"), ("006", "00", "test"))]
    assert len({r["group_key"] for r in rows[:3]}) == 1
    assert len({r["session_id"] for r in rows[:3]}) == 3
    assert rows[3]["group_key"] != rows[0]["group_key"]
    assert all(r["qc_reasons"] and "/qc-held/" in r["group_key"] for r in rows)
    groups = make_audits(rows)["groups"]
    assert sorted(g["count"] for g in groups) == [1, 3]
    assert len([g for g in groups if g["split"] == "train"][0]["sample_ids"]) == 3


def test_partial_manifest_resume_rebuilds_complete_groups_from_verified_receipts(tmp_path):
    records = [record("001", f"{i:02d}") for i in range(3)] + [record("002", lanes=[]), record("003", lanes=[])]
    first = run_import(tmp_path, records=records, fetcher=lambda url: jpeg(), workers=1, limit=2)
    assert first["state"] == "partial_import"
    initial = [json.loads(line) for line in (tmp_path / "manifest.jsonl").read_bytes().splitlines()]
    cached = {r["image_id"]: (r["source_sha256"], r["source_label_sha256"]) for r in initial}
    calls = []
    def fetcher(url):
        calls.append(url)
        return jpeg()
    final = run_import(tmp_path, records=records, fetcher=fetcher, workers=2)
    assert final["state"] == "awaiting_audit" and final["completed_images"] == 5
    assert len(calls) == 3  # Both verified cached photos are reused, not redownloaded.
    rows = [json.loads(line) for line in (tmp_path / "manifest.jsonl").read_bytes().splitlines()]
    assert len(rows) == len({r["image_id"] for r in rows}) == 5
    for r in rows:
        if r["image_id"] in cached:
            assert (r["source_sha256"], r["source_label_sha256"]) == cached[r["image_id"]]
    audits = json.loads((tmp_path / "audits.json").read_bytes())
    assert len(audits["groups"]) == 2
    assert sorted(g["count"] for g in audits["groups"]) == [2, 3]
    assert sum(g["count"] for g in audits["groups"]) == len(rows)
    assert final["manifest_sha256"] == sha256((tmp_path / "manifest.jsonl").read_bytes())
    assert final["audits_sha256"] == sha256((tmp_path / "audits.json").read_bytes())
