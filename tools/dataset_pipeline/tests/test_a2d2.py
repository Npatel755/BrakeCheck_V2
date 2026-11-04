"""Original source integrity, staged import and recording-level isolation."""
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from bcdata.licenses import LicenseRegistry
from bcdata.sources import a2d2


PALETTE = {"#ffc125": "Solid line", "#8000ff": "Dashed line", "#ff00ff": "RD normal street",
           "#48d1cc": "Ego car", "#ff0000": "Car 1", "#8b636c": "Non-drivable street"}


def source_objects(drives=("20180807_145028",), frames=(0, 10, 40, 80, 120), missing_info=False):
    payloads = {a2d2.PALETTE_KEY: json.dumps(PALETTE).encode(),
                a2d2.CALIBRATION_KEY: b'{"cameras":{"front_center":{}}}'}
    for drive in drives:
        for frame in frames:
            basename = f"{drive.replace('_', '')}_camera_frontcenter_{frame:09d}"
            prefix = f"{a2d2.PREFIX}{drive}/"
            rgb = np.random.default_rng(frame + int(drive[-2:])).integers(0, 256, (64, 96, 3), dtype=np.uint8)
            rgb[0, 0] = (frame % 255, 80, 180)
            mask = np.full_like(rgb, (255, 0, 255))
            mask[:, 28:31] = (255, 193, 37)
            mask[:, 60:63] = (128, 0, 255)
            payloads[prefix + "camera/cam_front_center/" + basename + ".png"] = cv2.imencode(".png", rgb)[1].tobytes()
            payloads[prefix + "label/cam_front_center/" + basename.replace("_camera_", "_label_") + ".png"] = cv2.imencode(".png", cv2.cvtColor(mask, cv2.COLOR_RGB2BGR))[1].tobytes()
            if not missing_info:
                payloads[prefix + "camera/cam_front_center/" + basename + ".json"] = json.dumps({"cam_tstamp": frame}).encode()
    objects = [{"key": key, "size": len(payload), "etag": hashlib.md5(payload).hexdigest()}
               for key, payload in payloads.items()]
    return objects, payloads


def local_download(monkeypatch, payloads):
    def download(url, destination, **kwargs):
        key = url.removeprefix(a2d2.BASE_URL)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payloads[key])
        return destination
    monkeypatch.setattr(a2d2, "download", download)


def permissive_fixture_license(tmp_path):
    path = tmp_path / "licenses.yaml"
    path.write_text("datasets:\n  a2d2:\n    license: CC-BY-ND-4.0\n    status: cleared_conditions\n    pool: commercial\n")
    return LicenseRegistry(path)


def test_exact_paint_class_mapping_excludes_other_paint_and_road():
    rgb = np.array([[(255, 193, 37), (128, 0, 255), (200, 125, 210), (210, 50, 115),
                     (255, 0, 255), (72, 209, 204), (255, 0, 0), (139, 99, 108), (255, 193, 38)]], dtype=np.uint8)
    assert a2d2.mask_to_classes(rgb, PALETTE).tolist() == [[2, 2, 0, 0, 1, 5, 4, 3, 0]]


@pytest.mark.parametrize("key", ["../x", "/x", "C:/x", "a/../x", "a\\x", "a//x", "a/./x"])
def test_object_paths_cannot_escape_root(tmp_path, key):
    with pytest.raises(ValueError, match="Unsafe|escaped"):
        a2d2.safe_path(tmp_path, key)


def test_plan_spreads_frames_across_recordings_and_never_splits_a_drive(settings):
    objects, _ = source_objects(drives=("20180807_145028", "20180810_142822", "20180810_150607"))
    settings.cfg.setdefault("sources", {})["a2d2"] = {"min_frame_gap": 30}
    planned = a2d2.plan(settings, objects=objects)
    assert len({item["session"] for item in planned[:3]}) == 3
    assert planned == a2d2.plan(settings, objects=list(reversed(objects)))
    assert a2d2.plan(settings, limit=5, objects=objects) == planned[:5]
    for drive in {item["session"] for item in planned}:
        items = [item for item in planned if item["session"] == drive]
        assert len({item["split"] for item in items}) == 1
        frames = sorted(item["frame"] for item in items)
        assert all(right - left >= 30 for left, right in zip(frames, frames[1:]))


def test_rank_split_reserves_whole_drives_and_is_independent_of_image_limit(settings):
    from collections import Counter
    drives = tuple(f"201808{day:02d}_145028" for day in range(1, 24))
    objects, _ = source_objects(drives=drives, frames=(0, 40, 80, 120))
    complete = a2d2.plan(settings, objects=objects)
    assignments = {item["session"]: item["split"] for item in complete}
    assert Counter(assignments.values()) == {"train": 19, "val": 2, "test": 2}
    for limit in (3, 23, 35):
        limited = a2d2.plan(settings, limit=limit, objects=objects)
        assert limited == complete[:limit]
        assert all(item["split"] == assignments[item["session"]] for item in limited)
        assert len({item["session_universe_sha256"] for item in limited}) == 1
    settings.cfg.setdefault("sources", {})["a2d2"] = {"min_frame_gap": 90}
    sparse = a2d2.plan(settings, objects=objects)
    assert all(item["split"] == assignments[item["session"]] for item in sparse)


def test_rank_split_retains_nonempty_holdouts_for_three_drives():
    assignments = a2d2._drive_splits(["a2d2:a", "a2d2:b", "a2d2:c"], {"train": 0.8, "val": 0.1, "test": 0.1}, 0)
    assert set(assignments.values()) == {"train", "val", "test"}


def test_plan_requires_paired_front_labels_and_preserves_missing_official_info(settings):
    objects, _ = source_objects(missing_info=True)
    objects = [obj for obj in objects if not ("_label_" in obj["key"] and "000000040" in obj["key"])]
    planned = a2d2.plan(settings, objects=objects)
    assert planned and all(item["metadata"] is None for item in planned)
    assert all(item["frame"] != 40 for item in planned)
    objects.append({"key": a2d2.PREFIX + "20180807_145028/camera/cam_front_right/other.png", "size": 7, "etag": "0" * 32})
    assert a2d2.plan(settings, objects=objects) == planned


def test_missing_source_palette_fails_before_network(settings):
    objects, _ = source_objects()
    with pytest.raises(ValueError, match="Missing official"):
        a2d2.plan(settings, objects=[obj for obj in objects if obj["key"] != a2d2.PALETTE_KEY])


def test_checksum_failure_cannot_be_used_or_left_as_complete_file(tmp_path, monkeypatch):
    obj = {"key": a2d2.PALETTE_KEY, "size": 4, "etag": hashlib.md5(b"good").hexdigest()}
    local_download(monkeypatch, {obj["key"]: b"evil"})
    with pytest.raises(IOError, match="checksum mismatch"):
        a2d2.download_object(obj, tmp_path)
    assert not a2d2.safe_path(tmp_path, obj["key"]).exists()


def test_same_size_cached_corruption_is_redownloaded_and_verified(tmp_path, monkeypatch):
    obj = {"key": a2d2.PALETTE_KEY, "size": 4, "etag": hashlib.md5(b"good").hexdigest()}
    target = a2d2.safe_path(tmp_path, obj["key"])
    target.parent.mkdir(parents=True)
    target.write_bytes(b"evil")
    local_download(monkeypatch, {obj["key"]: b"good"})
    assert a2d2.download_object(obj, tmp_path).read_bytes() == b"good"


def test_partial_download_resumes_and_verifies_final_bytes(tmp_path):
    payload = b"original-source-image"
    key = a2d2.PREFIX + "source.png"
    obj = {"key": key, "size": len(payload), "etag": hashlib.md5(payload).hexdigest()}
    target = a2d2.safe_path(tmp_path, key)
    target.parent.mkdir(parents=True)
    target.with_suffix(".png.part").write_bytes(payload[:7])
    class Response:
        status_code = 206
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def raise_for_status(self):
            pass
        def iter_content(self, size):
            yield payload[7:]
    class HTTP:
        def get(self, url, **kwargs):
            assert kwargs["headers"]["Range"] == "bytes=7-"
            return Response()
    assert a2d2.download_object(obj, tmp_path, HTTP()).read_bytes() == payload


def test_multipart_etag_requires_a_real_checksum(tmp_path):
    with pytest.raises(ValueError, match="single-part"):
        a2d2.download_object({"key": a2d2.PALETTE_KEY, "size": 20, "etag": "a" * 32 + "-2"}, tmp_path)


def test_paged_inventory_preserves_each_object_and_detects_repeated_tokens():
    class Response:
        def __init__(self, xml):
            self.content = xml.encode()
        def raise_for_status(self):
            pass
    class HTTP:
        def __init__(self):
            self.calls = 0
        def get(self, url, **kwargs):
            self.calls += 1
            suffix = "<IsTruncated>true</IsTruncated><NextContinuationToken>next</NextContinuationToken>" if self.calls == 1 else "<IsTruncated>false</IsTruncated>"
            if self.calls == 2:
                assert kwargs["params"]["continuation-token"] == "next"
            return Response(f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Contents><Key>{a2d2.PREFIX}{self.calls}.png</Key><Size>123</Size><ETag>"' + "a" * 32 + f'"</ETag></Contents>{suffix}</ListBucketResult>')
    objects, _ = a2d2.list_objects(a2d2.PREFIX, HTTP())
    assert len(objects) == 2
    class BadHTTP:
        def get(self, *args, **kwargs):
            return Response("<ListBucketResult><IsTruncated>true</IsTruncated><NextContinuationToken>same</NextContinuationToken></ListBucketResult>")
    with pytest.raises(ValueError, match="continuation"):
        a2d2.list_objects(a2d2.PREFIX, BadHTTP())


def test_ingest_preserves_originals_and_creates_no_human_reviews(settings, registry, tmp_path, monkeypatch):
    settings.cfg["quality"].update(min_width=32, min_height=32)
    objects, payloads = source_objects(frames=(0, 80), missing_info=True)
    local_download(monkeypatch, payloads)
    items = a2d2.plan(settings, objects=objects)
    licenses = permissive_fixture_license(tmp_path)
    result = a2d2.ingest(settings, registry, licenses, items=items, workers=1)
    assert result["added"] == result["staged"] == 2
    assert result["failed"] == 0
    assert registry.count("SELECT COUNT(*) FROM reviews") == 0
    assert registry.count("SELECT COUNT(*) FROM annotations WHERE kind='human'") == 0
    assert registry.count("SELECT COUNT(*) FROM images WHERE status='curated'") == 0
    for row in registry.images():
        assert row["status"] == "ingested" and row["review_status"] == "none" and row["locked"] == 0
        assert row["pool"] == "commercial" and row["has_source_label"] == 1
        ann = registry.annotation(row["image_id"], "source_label")
        params = json.loads(ann["params"])
        assert params["format"] == "a2d2_rgb" and params["human"] is True
        provenance = json.loads(Path(params["provenance_path"]).read_text())
        assert provenance["per_frame_metadata_available"] is False
        for source_file in provenance["files"].values():
            path = a2d2.safe_path(settings.path("raw", "a2d2"), source_file["key"])
            assert path.read_bytes() == payloads[source_file["key"]]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == source_file["sha256"]
    first = registry.images()[0]
    # The fixture human approval proves that a subsequent import cannot modify
    # a saved human annotation, review row or existing image state.
    human_path = tmp_path / "human.json"
    human_path.write_text('{"lanes": []}')
    registry.add_annotation(first["image_id"], "human", "fixture", "1", str(human_path))
    registry.add_review(first["image_id"], "fixture", "approved")
    before_image = dict(registry.image(first["image_id"]))
    before_reviews = [tuple(row) for row in registry.conn.execute("SELECT * FROM reviews")]
    second = a2d2.ingest(settings, registry, licenses, items=items, workers=1)
    assert second["duplicate"] == 2 and second["added"] == 0
    assert dict(registry.image(first["image_id"])) == before_image
    assert [tuple(row) for row in registry.conn.execute("SELECT * FROM reviews")] == before_reviews
    assert human_path.read_text() == '{"lanes": []}'


def test_import_never_attaches_audi_labels_to_other_source_duplicate(settings, registry, tmp_path, monkeypatch):
    settings.cfg["quality"].update(min_width=32, min_height=32)
    objects, payloads = source_objects(frames=(0,))
    local_download(monkeypatch, payloads)
    items = a2d2.plan(settings, objects=objects)
    image_key = items[0]["img"]
    existing = tmp_path / "other.png"
    existing.write_bytes(payloads[image_key])
    registry.add_image(image_id="original", source="other", session_id="heldout", source_ref="original",
                       path=str(existing), sha256=hashlib.sha256(existing.read_bytes()).hexdigest(),
                       license_id="other", pool="commercial", status="curated", split="test")
    before = dict(registry.image("original"))
    result = a2d2.ingest(settings, registry, permissive_fixture_license(tmp_path), items=items, workers=1)
    assert result["duplicate"] == 1 and result["image_ids"] == []
    assert dict(registry.image("original")) == before
    assert registry.annotations("original") == []


def test_new_frame_keeps_an_existing_recording_split_after_seed_change(settings, registry, tmp_path, monkeypatch):
    settings.cfg["quality"].update(min_width=32, min_height=32)
    objects, payloads = source_objects(frames=(0, 80))
    local_download(monkeypatch, payloads)
    licenses = permissive_fixture_license(tmp_path)
    items = a2d2.plan(settings, objects=objects)
    result = a2d2.ingest(settings, registry, licenses, items=items[:1], workers=1)
    existing_id = result["image_ids"][0]
    registry.update_image(existing_id, split="test")
    settings.cfg["seed"] = 935
    changed_plan = a2d2.plan(settings, objects=objects)
    result = a2d2.ingest(settings, registry, licenses, items=changed_plan, workers=1)
    assert result["failed"] == 0
    assert {row["split"] for row in registry.images()} == {"test"}


def test_bad_mask_pair_never_registers_an_image(settings, registry, tmp_path, monkeypatch):
    objects, payloads = source_objects(frames=(0,))
    mask_obj = next(obj for obj in objects if "_label_" in obj["key"])
    payload = cv2.imencode(".png", np.zeros((10, 10, 3), np.uint8))[1].tobytes()
    payloads[mask_obj["key"]] = payload
    mask_obj.update(size=len(payload), etag=hashlib.md5(payload).hexdigest())
    local_download(monkeypatch, payloads)
    result = a2d2.ingest(settings, registry, permissive_fixture_license(tmp_path),
                         items=a2d2.plan(settings, objects=objects), workers=1)
    assert result["failed"] == 1
    assert registry.count("SELECT COUNT(*) FROM images") == 0
