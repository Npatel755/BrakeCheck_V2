import io
import json
from pathlib import Path
import zipfile

from PIL import Image
import pytest
import requests

from bcdata.import_college_station import (
    OfficialHTTP, RangedSplitZIP, apply_coverage_policy, assign_audit_groups, image_record, json_bytes, load_journal,
    make_audits, paired_entries, parse_lines, sha256, source_identity, verify_record,
)


class MemoryRanges:
    def __init__(self, pieces):
        self.pieces = pieces
        self.calls = []

    def get(self, url, *, start, end):
        fid = int(url.rsplit("/", 1)[-1])
        self.calls.append((fid, start, end))
        return self.pieces[fid][start:end + 1]


def split_reader(data, cut=31, block_size=19):
    parts, pieces = [], {}
    for number, start in enumerate(range(0, len(data), cut), 1):
        piece = data[start:start + cut]
        pieces[number] = piece
        parts.append({"filename": f"fixture.zip.{number:03}", "filesize": len(piece), "id": number})
    http = MemoryRanges(pieces)
    return RangedSplitZIP(parts, http, block_size=block_size, cache_blocks=2), http


def jpeg_bytes(size=(100, 100)):
    output = io.BytesIO()
    Image.new("RGB", size, (80, 90, 100)).save(output, "JPEG")
    return output.getvalue()


NAME = "CollegeStation_dataset/CollegeStation_dataset_annotated/morning/01_S_College_Ave/fixture.jpg"


def test_ranges_cross_part_and_block_boundaries_and_eof():
    reader, http = split_reader(bytes(range(200)))
    assert reader.read(75) == bytes(range(75))
    reader.seek(-15, 2)
    assert reader.read(100) == bytes(range(185, 200))
    assert reader.read(5) == b""
    assert all(end < len(http.pieces[fid]) for fid, start, end in http.calls)


def test_neighboring_header_reads_share_a_bounded_range_cache():
    reader, http = split_reader(bytes(range(200)), cut=200, block_size=32)
    assert reader.read(5) == bytes(range(5))
    reader.seek(2)
    assert reader.read(5) == bytes(range(2, 7))
    assert len(http.calls) == 1
    reader.seek(130)
    reader.read(50)
    assert len(reader.cache) <= 2


def test_split_parts_must_not_silently_skip_a_volume():
    parts = [{"filename": "x.zip.001", "filesize": 10, "id": 1},
             {"filename": "x.zip.003", "filesize": 10, "id": 3}]
    with pytest.raises(ValueError, match="complete"):
        RangedSplitZIP(parts, MemoryRanges({}))


def test_split_reader_extracts_real_zip64_local_headers_and_crc_checks():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        with archive.open(NAME, "w", force_zip64=True) as member:
            member.write(jpeg_bytes())
        archive.writestr(NAME[:-4] + ".lines.txt", "20 90 40 50\n")
        archive.writestr(NAME.replace("fixture", "unpaired"), jpeg_bytes())
        archive.writestr("TX21/raw.mp4", b"raw video never fetched")
    reader, _ = split_reader(output.getvalue(), cut=89, block_size=64)
    with zipfile.ZipFile(reader) as archive:
        pairs = paired_entries(archive, "CollegeStation_dataset")
        assert len(pairs) == 1
        assert archive.read(pairs[0][0]) == jpeg_bytes()
        assert archive.read(pairs[0][1]) == b"20 90 40 50\n"


def test_corrupt_remote_zip_entry_fails_crc_before_conversion():
    output = io.BytesIO()
    payload = b"native-entry-payload"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("source.lines.txt", payload)
    damaged = output.getvalue().replace(payload, b"Native-entry-payload", 1)
    reader, _ = split_reader(damaged)
    with zipfile.ZipFile(reader) as archive, pytest.raises(zipfile.BadZipFile, match="CRC"):
        archive.read("source.lines.txt")


class Response:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self.body = body
        self.headers = headers or {}
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, size):
        yield self.body

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)

    def get(self, *args, **kwargs):
        return next(self.responses)


def test_whole_archive_fallback_is_refused_without_reading_body():
    response = Response(200, b"whole archive")
    http = OfficialHTTP(Session([response]), attempts=1)
    with pytest.raises(ValueError, match="exact byte range"):
        http.get("https://official.example/archive", start=10, end=12)
    assert response.closed
    assert http.network_bytes == 0


def test_server_truncation_is_retried_and_rate_limit_respects_retry_after():
    sleeps = []
    first = Response(429, b"", {"Retry-After": "3"})
    second = Response(206, b"a", {"Content-Range": "bytes 10-12/100"})
    third = Response(206, b"abc", {"Content-Range": "bytes 10-12/100"})
    http = OfficialHTTP(Session([first, second, third]), attempts=3, sleep=sleeps.append)
    assert http.get("https://official.example/archive", start=10, end=12) == b"abc"
    assert sleeps == [3, 2]
    assert all(response.closed for response in (first, second, third))


def test_geometry_keeps_native_coordinates_and_only_orders_slots():
    geometry, reasons = parse_lines(b"20 90 40 50\n80 90 60 50\n", 100, 100)
    assert reasons == []
    assert [lane["slot"] for lane in geometry["lanes"]] == [2, 3]
    assert geometry["lanes"][0]["points"] == [[20.0, 90.0, True], [40.0, 50.0, True]]
    assert geometry["lanes"][0]["y_bottom"] == 90
    assert geometry["lanes"][0]["kind"] == "unknown"
    assert geometry["coordinate_system"] == "original_image_pixels"
    assert geometry["lanes"][0]["visibility"] == "unknown"
    assert geometry["lanes"][0]["visible_paint_verified"] is False
    assert geometry["lanes"][0]["physical_evidence"] == "publisher_annotation_only_unverified"
    assert geometry["source_annotation"]["label_status"] == "source_only_awaiting_audit"


def test_empty_native_label_is_source_zero_without_claiming_independent_paint_verification():
    geometry, reasons = parse_lines(b"\n", 100, 100)
    assert reasons == []
    assert geometry["lanes"] == []
    assert geometry["tags"]["no_lanes"] is True
    assert geometry["source_annotation"]["visible_paint_verified"] is False


@pytest.mark.parametrize("data,reason", [
    (b"nan 10 20 40", "invalid_native_polyline"),
    (b"12 10 20", "invalid_native_polyline"),
    (b"x 10 20 40", "invalid_native_number"),
    (b"20 20 20 20", "degenerate_native_polyline"),
    (b"-2 90 20 50", "native_points_outside_image"),
    (b"0 90 99 91", "potential_transverse_marking"),
])
def test_uncertain_labels_are_held_and_never_become_false_no_lane_approval(data, reason):
    record, geometry = image_record("CollegeStation_dataset", NAME, jpeg_bytes(), data)
    assert reason in record["qc_reasons"]
    assert record["split"] == "hold"
    assert json.loads(geometry)["tags"]["no_lanes"] is False


def test_physical_road_group_is_shared_across_lighting_and_spelling_variants():
    a = NAME.replace("01_S_College_Ave", "02_W_Villa_Maria")
    b = a.replace("morning", "night").replace("Maria", "Maia")
    first = source_identity("CollegeStation_dataset", a)
    second = source_identity("CollegeStation_dataset", b)
    assert first[0] == second[0]
    assert first[2] == second[2]
    assert first[1] != second[1]


def test_real_artifact_hashes_detect_tampering_before_resume(tmp_path):
    picture, native = jpeg_bytes(), b"20 90 40 50\n"
    row, geometry = image_record("CollegeStation_dataset", NAME, picture, native)
    for key, data in (("image", picture), ("source_label", native), ("geometry", geometry)):
        path = tmp_path / row[key]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    assert verify_record(tmp_path, row)
    previous = dict(row, converter="college-station-native-lines-v1")
    assert not verify_record(tmp_path, previous)
    assert verify_record(tmp_path, previous, require_converter=False)
    (tmp_path / row["source_label"]).write_bytes(b"changed")
    assert not verify_record(tmp_path, row)
    row["image"] = "../outside.jpg"
    assert not verify_record(tmp_path, row)


def test_truncated_jpeg_scan_data_is_not_published_as_an_auditable_image():
    with pytest.raises(OSError):
        image_record("CollegeStation_dataset", NAME, jpeg_bytes()[:-30], b"20 90 40 50\n")


def test_audits_are_stable_cover_every_group_and_select_three_distinct_members():
    rows = []
    for group, count in (("day", 9), ("night", 2)):
        rows.extend({"image_id": group + str(i), "group_key": group, "split": "train",
                     "qc_reasons": [], "session_id": "same-road", "sequence_id": group} for i in range(count))
    forward, reverse = make_audits(rows), make_audits(list(reversed(rows)))
    assert forward == reverse
    assert sum(group["count"] for group in forward["groups"]) == len(rows)
    assert [len(group["sample_ids"]) for group in forward["groups"]] == [3, 2]
    assert len({item for group in forward["groups"] for item in group["sample_ids"]}) == 5
    assert forward["groups"][0]["sample_ids"] == ["day0", "day4", "day8"]


def test_audit_samples_span_temporal_sequence_with_natural_frame_numbers():
    rows = [{"image_id": "id-" + str(n), "group_key": "sequence", "split": "train",
             "qc_reasons": [], "session_id": "road", "sequence_id": "sequence",
             "source_member": "front/image_" + str(n) + ".jpg"} for n in (100, 1, 2, 11, 9)]
    assert make_audits(rows)["groups"][0]["sample_ids"] == ["id-1", "id-9", "id-100"]


def test_clean_audit_groups_are_capped_at_250_and_do_not_multiply_by_lane_count():
    records = [{"image_id": "id" + str(n), "source_archive": "US290", "split": "train",
                "source_member": f"US290/US290_dataset/east/b_w_b/1/img/frame{n}.jpg",
                "qc_reasons": [], "session_id": "us290-highway", "sequence_id": "one"} for n in range(274)]
    assigned = assign_audit_groups(records)
    audits = make_audits(assigned)
    assert sorted(group["count"] for group in audits["groups"]) == [24, 250]
    assert all(len(group["sample_ids"]) == 3 for group in audits["groups"])


def test_native_empty_audit_samples_are_not_hidden_in_positive_lane_cohorts():
    record = {"image_id": "id1", "source_archive": "US290", "split": "train",
              "source_member": "US290/US290_dataset/east/b_w_b/1/img/frame1.jpg",
              "qc_reasons": [], "session_id": "us290-highway", "sequence_id": "one",
              "source_label_sha256": sha256(b"")}
    positive = dict(record, image_id="id2", source_label_sha256=sha256(b"20 90 40 50\n"))
    groups = make_audits(assign_audit_groups([record, positive]))["groups"]
    assert len(groups) == 2
    assert any("native-empty" in group["group_key"] for group in groups)
    assert any("native-positive" in group["group_key"] for group in groups)


def test_qc_audits_combine_small_sequences_by_site_and_primary_cause():
    records = [{"image_id": "id" + str(n), "source_archive": "US290", "split": "hold",
                "source_member": f"US290/US290_dataset/east/b_w_b/{n}/img/frame{n}.jpg",
                "qc_reasons": ["native_points_outside_image"] + (["short_native_polyline"] if n else []),
                "session_id": "us290-highway", "sequence_id": "sequence" + str(n)} for n in range(3)]
    assigned = assign_audit_groups(records)
    audits = make_audits(assigned)
    assert len(audits["groups"]) == 1
    assert audits["groups"][0]["count"] == 3
    assert audits["groups"][0]["qc_reasons"] == ["native_points_outside_image", "short_native_polyline"]


def test_visually_demonstrated_partial_native_material_subset_is_held_without_synthesizing():
    original = {"image_id": "id", "source_archive": "US290", "split": "train", "intended_split": "train",
                "source_member": "US290/US290_dataset/west/w_b/1/img/frame1.jpg", "qc_reasons": [],
                "session_id": "us290-highway", "sequence_id": "west-wb", "geometry_sha256": "unchanged"}
    held = apply_coverage_policy([original])[0]
    assert held["split"] == "hold"
    assert held["qc_reasons"] == ["native_coverage_incomplete"]
    assert held["geometry_sha256"] == original["geometry_sha256"]
    assert original["split"] == "train"
    assert "native_coverage_incomplete" in assign_audit_groups([held])[0]["group_key"]
    unselected = dict(original, source_member=original["source_member"].replace("west/w_b", "west/white"))
    assert apply_coverage_policy([unselected])[0] == unselected


def test_interrupted_final_journal_line_is_repaired_before_appending(tmp_path):
    path = tmp_path / "progress.jsonl"
    row = {"source_member": "first", "image_id": "id1"}
    path.write_bytes(json_bytes(row) + b'{"source_member":')
    assert load_journal(tmp_path) == {"first": row}
    assert path.read_bytes() == json_bytes(row)
    with path.open("ab") as output:
        output.write(json_bytes({"source_member": "second", "image_id": "id2"}))
    assert len(load_journal(tmp_path)) == 2


def test_malformed_interior_journal_line_is_not_silently_discarded(tmp_path):
    (tmp_path / "progress.jsonl").write_bytes(b"bad\n" + json_bytes({"source_member": "second"}))
    with pytest.raises(ValueError):
        load_journal(tmp_path)
