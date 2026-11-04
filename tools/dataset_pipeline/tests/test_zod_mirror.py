import io
import json
import tarfile

from fastapi import HTTPException
from PIL import Image
import pytest

from bcdata import import_zod_mirror as importer
from bcdata.simulation_review import SimulationCorrection, SimulationDecision
from bcdata.zod_mirror import connect, convert, encoded, insert_annotation, sha
from bcdata.zod_mirror_review import ZodMirrorReview


def polygon(x=1000, y=1400, cls="lm_solid", end=1900):
    return {"geometry": {"type": "Polygon", "coordinates": [[[x, y], [x + 15, y], [x + 15, end], [x, end], [x, y]]]},
            "properties": {"class": cls}}


def photo(width=1280, height=720):
    stream = io.BytesIO()
    Image.new("RGB", (width, height), (50, 70, 90)).save(stream, "JPEG")
    return stream.getvalue()


def fixture(tmp_path, count=5, warnings=False):
    root = tmp_path / "experiments/zod_mirror"
    db = connect(root)
    for index in range(count):
        frame_id = f"{index:06}"
        insert_annotation(db, frame_id, encoded([polygon()]).encode())
        raw = photo()
        path = root / "images/1280x720" / (frame_id + ".jpg")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        db.execute("UPDATE frames SET image_path=?,image_sha=?,image_ready=1,qc_json=? WHERE frame_id=?",
                   (path.relative_to(root).as_posix(), sha(raw),  '["paint_polygon_needs_manual_trace"]' if warnings else "[]", frame_id))
    db.commit()
    db.close()
    return root, ZodMirrorReview(tmp_path)


def decision(item, action="approve", event="event-00000001"):
    return SimulationDecision(image_id=item["image_id"], action=action, event_id=event,
                              revision=item["revision"], fingerprint=item["fingerprint"])


def test_unclassified_road_paint_does_not_erase_valid_lanes():
    unclassified = polygon(x=2500)
    unclassified["properties"] = {"ContainsText": True}
    geometry, warnings = convert([unclassified, polygon()], "zod-000001")
    assert len(geometry["lanes"]) == 1
    assert len(geometry["source_polygons"]) == 2
    assert geometry["source_class_counts"] == {"road_paint_other": 1, "lm_solid": 1}


def test_compatible_dashes_share_identity_but_preserve_gaps():
    geometry, _ = convert([polygon(y=1400, end=1500, cls="lm_dashed"),
                           polygon(y=1700, end=1800, cls="lm_dashed")], "zod-000001")
    assert len(geometry["lanes"]) == 1
    segments = geometry["lanes"][0]["segments"]
    assert len(segments) == 2
    assert max(point[1] for point in segments[0]) < min(point[1] for point in segments[1])
    assert geometry["lanes"][0]["points"] == [point for segment in segments for point in segment]


def test_missing_annotation_is_a_warning_not_verified_empty():
    geometry, warnings = convert(None, "zod-000001")
    assert not geometry["lanes"]
    assert "missing_or_invalid_native_lane_annotations" in warnings


def test_three_spread_samples_cover_a_conversion_group_and_all_photos_remain_held(tmp_path):
    _, store = fixture(tmp_path)
    assert store.status()["counts"]["pending"] == 3
    assert store.status()["counts"]["groups"] == 1
    assert store.status()["counts"]["indexed"] == 5
    assert [item["image_id"] for item in store.queue(limit=12)["items"]] == ["zod-000000", "zod-000002", "zod-000004"]
    assert not store.queue(split="train")["items"]
    assert all(item["group_images"] == 5 and not item["training_eligible"] for item in store.queue(limit=12)["items"])


def test_decisions_are_per_photo_idempotent_and_revision_checked(tmp_path):
    _, store = fixture(tmp_path)
    item = store.detail("zod-000000")
    request = decision(item)
    saved = store.decide(request, "Nick")
    assert saved["decision"]["action"] == "approve"
    assert store.decide(request, "Nick")["revision"] == saved["revision"]
    assert store.status()["counts"]["pending"] == 2
    assert store.status()["counts"]["eligible_train_candidates"] == 0
    with pytest.raises(HTTPException) as error:
        store.decide(decision(item, "exclude", "event-00000002"), "Nick")
    assert error.value.status_code == 409


def test_warning_needs_whole_photo_confirmation_and_manual_fixes_survive_reimport(tmp_path):
    root, store = fixture(tmp_path, count=1, warnings=True)
    item = store.detail("zod-000000")
    with pytest.raises(HTTPException):
        store.decide(decision(item), "Nick")
    lanes = [{"slot": 2, "points": [[400, 500], [350, 650]]}]
    body = SimulationCorrection(image_id=item["image_id"], revision=item["revision"], fingerprint=item["fingerprint"],
                                lanes=lanes, resolved_qc_reasons=["paint_polygon_needs_manual_trace"],
                                approve=True, complete_annotation=True)
    saved = store.correct(body, "Nick")
    assert saved["decision"]["action"] == "approve"
    db = connect(root)
    assert not insert_annotation(db, "000000", encoded([polygon()]).encode())
    db.commit()
    db.close()
    assert store.detail("zod-000000")["geometry"]["lanes"] == lanes
    assert store.detail("zod-000000")["source_geometry"]["lanes"] != lanes
    assert store.detail("zod-000000")["group_state"] == "needs_repair"


def test_bad_sample_holds_entire_group_even_after_later_sample_approval(tmp_path):
    _, store = fixture(tmp_path)
    store.decide(decision(store.detail("zod-000000"), "exclude"), "Nick")
    store.decide(decision(store.detail("zod-000002"), event="event-00000002"), "Nick")
    counts = store.status()["counts"]
    assert counts["groups_blocked"] == 1
    assert counts["photos_in_repair_groups"] == 5
    assert len(store.queue(status="needs_repair", limit=12)["items"]) == 3
    assert store.detail("zod-000004")["group_state"] == "needs_repair"
    assert not store.queue()["items"]
    assert counts["pending"] == 0


def test_existing_review_survives_sample_plan_and_replanning(tmp_path):
    root, store = fixture(tmp_path)
    db = connect(root)
    # An existing reviewed photo must remain evidence even if it is not the
    # default first/middle/last position. Build a genuine vote first.
    db.close()
    store.decide(decision(store.detail("zod-000001")), "Nick")
    db = connect(root)
    db.execute("DELETE FROM zod_audit_plan")
    db.commit()
    db.close()
    assert store.detail("zod-000001")["decision"]["action"] == "approve"
    assert "zod-000001" in [item["image_id"] for item in store.queue(status="all", limit=12)["items"]]


def test_different_warnings_and_marking_types_never_share_a_group(tmp_path):
    root, store = fixture(tmp_path)
    db = connect(root)
    db.execute("UPDATE frames SET qc_json='[\"missing_or_invalid_native_lane_annotations\"]' WHERE image_id='zod-000001'")
    db.commit()
    db.close()
    assert store.status()["counts"]["groups"] == 2
    assert store.detail("zod-000001")["group_images"] == 1
    assert store.detail("zod-000000")["group_images"] == 4


def test_diagnosed_conversion_is_held_without_fabricating_a_vote(tmp_path):
    from bcdata.zod_audit_groups import record_finding
    root, store = fixture(tmp_path)
    db = connect(root)
    record_finding(db, "zod-000001", "Distant fragments replace visible boundaries", "reported-example inspection")
    db.close()
    assert store.status()["counts"]["groups_blocked"] == 1
    item = store.detail("zod-000000")
    assert item["group_state"] == "needs_repair"
    assert item["conversion_findings"]
    assert item["decision"] is None


def test_source_tamper_blocks_review(tmp_path):
    root, store = fixture(tmp_path, count=1)
    (root / "images/1280x720/000000.jpg").write_bytes(b"changed")
    with pytest.raises(HTTPException) as error:
        store.detail("zod-000000")
    assert error.value.status_code == 409


def test_empty_human_correction_requires_explicit_photo_confirmation(tmp_path):
    _, store = fixture(tmp_path, count=1)
    item = store.detail("zod-000000")
    body = SimulationCorrection(image_id=item["image_id"], revision=item["revision"],
                                fingerprint=item["fingerprint"], lanes=[], approve=True)
    with pytest.raises(HTTPException) as error:
        store.correct(body, "Nick")
    assert error.value.status_code == 422
    assert store.detail("zod-000000")["geometry"]["lanes"]


def test_both_resolution_archives_produce_one_audit_per_frame(tmp_path, monkeypatch):
    root = tmp_path / "experiments/zod_mirror"
    upstream = root / "upstream"
    upstream.mkdir(parents=True)
    def archive(name, members):
        path = upstream / name
        with tarfile.open(path, "w:gz") as tar:
            for member, raw in members:
                info = tarfile.TarInfo(member)
                info.size = len(raw)
                tar.addfile(info, io.BytesIO(raw))
        return path.stat().st_size, sha(path.read_bytes())
    files = {"annotations.tar.gz": archive("annotations.tar.gz", [("single_frames/000000/annotations/lane_markings.json", encoded([polygon()]).encode())]),
             "1280x720_images.tar.gz": archive("1280x720_images.tar.gz", [("1280x720_images/000000.jpg", photo())]),
             "640x360_images.tar.gz": archive("640x360_images.tar.gz", [("640x360_images/000000.jpg", photo(640,360))])}
    monkeypatch.setattr(importer, "FILES", files)
    importer.run(root)
    store = ZodMirrorReview(tmp_path)
    assert store.status()["counts"]["imported"] == 1
    assert len(store.queue(limit=12)["items"]) == 1
    assert (root / "images/640x360/000000.jpg").exists()


def test_zod_routes_require_the_existing_review_session(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from bcdata.zod_mirror_review import create_zod_mirror_router
    def denied():
        raise HTTPException(401, "Pair first")
    app = FastAPI()
    app.include_router(create_zod_mirror_router(tmp_path, denied))
    with TestClient(app) as client:
        for path in ("/api/studio/zod", "/api/studio/zod/queue",
                     "/api/studio/zod/samples/zod-000000", "/api/studio/zod/samples/zod-000000/photo"):
            assert client.get(path).status_code == 401


def test_archive_symlink_cannot_escape_the_isolated_import(tmp_path, monkeypatch):
    root = tmp_path / "experiments/zod_mirror"
    (root / "upstream").mkdir(parents=True)
    path = root / "upstream/annotations.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("single_frames/000000/annotations/lane_markings.json")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../outside.json"
        tar.addfile(info)
    monkeypatch.setattr(importer, "FILES", {"annotations.tar.gz": (path.stat().st_size, sha(path.read_bytes()))})
    with pytest.raises(ValueError, match="Links"):
        importer.run(root)
    assert not (tmp_path / "outside.json").exists()
