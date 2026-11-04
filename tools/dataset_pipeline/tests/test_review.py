"""Review transaction, phone access and UFLD export tests. Uses disposable data."""
from pathlib import Path
import csv
import json
import uuid

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from bcdata.config import ensure_layout, load_settings
from bcdata.db import Registry
from bcdata.geometry import lane_xs
from bcdata.lanes import current_geometry
from bcdata.review_export import export_reviewed
from bcdata.review_server import create_app
from bcdata.review_store import ReviewError, ReviewStore
from bcdata.sources.comma10k import session_of


def build_review_fixture(root):
    settings = load_settings(overrides={"data_root": str(root), "export": {"reviewed_only": True}})
    ensure_layout(settings)
    reg = Registry(settings.db_path)
    for index, split in enumerate(("train", "val", "test")):
        image_id = f"sample-{index}"
        rgb = np.full((874, 1164, 3), 40 + index * 5, np.uint8)
        rgb[410:660, 500:510] = 230
        path = root / "raw" / (image_id + ".png")
        Image.fromarray(rgb).save(path)
        reg.add_image(image_id=image_id, source="comma10k", session_id=f"session-{index}", source_ref=f"fixture:{index}",
                      path=str(path), width=1164, height=874, sha256=str(index), license_id="comma10k", pool="commercial", status="curated", split=split,
                      tags={"lighting": {"value": "day", "origin": "heuristic"}})
        lane = {"id": 0, "slot": 2, "physical": True, "kind": "solid", "fit": {"basis": "interp", "ys": [410, 650], "xs": [550, 450]},
                "y_top": 410, "y_bottom": 650, "points": [[450, 650, True], [550, 410, True]], "flags": [], "x_ref": 450, "segments": 1, "gaps": []}
        geo = {"image_id": image_id, "version": "fixture-auto", "width": 1164, "height": 874, "horizon_y": 367, "valid_bottom": 665, "lanes": [lane], "tags": {"lane_count": 1}}
        geo_path = root / "lane_geometry" / (image_id + ".json")
        geo_path.write_text(json.dumps(geo))
        reg.add_annotation(image_id, "geometry", "geometry", "fixture-auto", str(geo_path))
        reg.set_qc(image_id, .8, "medium", .2, [], False, "fixture-auto")
    reg.close()
    return settings


@pytest.fixture
def setup_review(tmp_path):
    settings = build_review_fixture(tmp_path)
    return settings, ReviewStore(settings)


def decide(store, image_id="sample-0", action="approved", revision=0, **kwargs):
    return store.decide(image_id, action, "Nick", revision, str(uuid.uuid4()), **kwargs)


def test_session_prefixes_group_by_recording():
    a = session_of("r055_0fcd4b3c68d5a5dc_2019-05-07--01-09-06_14_200.png")
    b = session_of("0099_0fcd4b3c68d5a5dc_2019-05-07--01-09-06_15_400.png")
    assert a[0] == b[0] == "0fcd4b3c68d5a5dc_2019-05-07--01-09-06"
    assert b[1] - a[1] == 1400


def test_requeue_zero_lanes_preserves_edges_history_and_nonempty_decisions(setup_review):
    settings, store = setup_review
    edges = {"left": {"status": "visible", "kind": "curb", "points": [[100, 600], [200, 400]]},
             "right": {"status": "unknown", "kind": "pavement_edge", "points": []}}
    original = decide(store, action="corrected", lanes=[], road_edges=edges)
    decide(store, image_id="sample-1")
    decide(store, image_id="sample-2", action="rejected")
    before = store.detail("sample-0")
    with store.registry() as reg:
        human_path = Path(reg.annotation("sample-0", "human")["path"])
    saved_bytes = human_path.read_bytes()
    reopened = store.requeue_empty_lanes()
    assert [item["image_id"] for item in reopened] == ["sample-0"]
    pending = store.detail("sample-0")
    assert pending["review_status"] == "pending" and pending["revision"] == before["revision"] + 1
    assert pending["geometry"]["road_edges"] == before["geometry"]["road_edges"]
    assert human_path.read_bytes() == saved_bytes
    assert store.detail("sample-1")["review_status"] == "approved"
    assert store.detail("sample-2")["review_status"] == "rejected"
    assert store.requeue_empty_lanes() == []
    assert store.next_image("fixture-owner")["image_id"] == "sample-0"
    assert export_reviewed(settings)["exported"] == 1
    with pytest.raises(ReviewError, match="Another reviewer"):
        decide(store, revision=before["revision"])
    store.undo(reopened[0]["event_id"], "Nick")
    assert store.detail("sample-0")["review_status"] == "corrected"
    assert store.detail("sample-0")["geometry"]["road_edges"] == before["geometry"]["road_edges"]


def test_approval_is_durable_locked_and_categorized(setup_review):
    settings, store = setup_review
    original = store.detail("sample-0")["geometry"]
    result = decide(store)
    reloaded = ReviewStore(settings)
    detail = reloaded.detail("sample-0")
    assert detail["origin"] == "human" and detail["geometry"]["lanes"] == original["lanes"]
    assert reloaded.stats()["accepted"] == 1 and reloaded.stats()["pending"] == 2
    assert settings.path("reviewed", "human_approved", "train", "sample-0.png").exists()
    with reloaded.registry() as reg:
        assert reg.image("sample-0")["locked"] == 1
        reg.add_annotation("sample-0", "geometry", "geometry", "bad-replacement", "missing.json")
        assert reg.annotation("sample-0", "geometry")["model_version"] == "fixture-auto"
    assert result["revision"] == 1


def test_duplicate_retry_does_not_double_count_and_conflict_rejected(setup_review):
    _, store = setup_review
    event_id = str(uuid.uuid4())
    for _ in range(2):
        store.decide("sample-0", "approved", "Nick", 0, event_id)
    assert store.stats()["reviewed"] == 1
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM review_events") == 1
    with pytest.raises(ReviewError, match="different content"):
        store.decide("sample-0", "approved", "Nick", 0, event_id, notes="changed after a lost response")
    with pytest.raises(ReviewError, match="Another reviewer"):
        decide(store, revision=0, action="rejected")
    assert store.detail("sample-0")["review_status"] == "approved"


def test_correction_uses_exact_polyline_and_undo_restores_prior_human(setup_review):
    _, store = setup_review
    approve = decide(store)
    original = store.detail("sample-0")["geometry"]
    points = [[400, 650], [490, 530], [560, 410]]
    correction = decide(store, action="corrected", revision=1, lanes=[{"slot": 2, "kind": "dashed", "points": points}])
    detail = store.detail("sample-0")
    lane = detail["geometry"]["lanes"][0]
    assert lane["kind"] == "dashed" and lane["polyline_points"] == points
    assert lane_xs(lane, [410, 530, 650]).tolist() == [560, 490, 400]
    store.undo(correction["event_id"], "Nick")
    assert store.detail("sample-0")["geometry"]["lanes"] == original["lanes"]
    with pytest.raises(ReviewError, match="newer decision"):
        store.undo(approve["event_id"], "Nick")


@pytest.mark.parametrize("lanes", [
    [{"slot": 2, "points": [[20, 650], [30, 650]]}],
    [{"slot": 2, "points": [[-1, 650], [30, 410]]}],
    [{"slot": 2, "points": [[20, 650], [float('nan'), 410]]}],
    [{"slot": 2, "points": [[20, 650], [30, 410]]}, {"slot": 2, "points": [[40, 650], [50, 410]]}],
])
def test_invalid_correction_does_not_change_review_state(setup_review, lanes):
    _, store = setup_review
    with pytest.raises(ReviewError):
        decide(store, action="corrected", lanes=lanes)
    assert store.stats()["reviewed"] == 0


def test_undo_rejection_returns_image_to_pending_without_fake_human_label(setup_review):
    settings, store = setup_review
    result = decide(store, action="rejected")
    assert settings.path("reviewed", "human_rejected", "train", "sample-0.png").exists()
    store.undo(result["event_id"], "Nick")
    assert store.detail("sample-0")["origin"] == "source_derived_proposal"
    assert store.stats()["pending"] == 3
    assert not settings.path("reviewed", "human_rejected", "train", "sample-0.png").exists()


def test_review_claims_avoid_duplicate_assignment(setup_review):
    _, store = setup_review
    first = store.next_image("phone-a")
    second = store.next_image("phone-b")
    assert first["image_id"] != second["image_id"]
    with pytest.raises(ReviewError, match="Another reviewer is working"):
        decide(store, first["image_id"], owner="phone-b")


def test_export_only_human_accepted_and_undo_removes_from_lists(setup_review):
    settings, store = setup_review
    approval = decide(store)
    decide(store, "sample-1", "rejected")
    report = export_reviewed(settings)
    assert report["exported"] == 1 and report["counts"] == {"train": 1, "val": 0, "test": 0}
    assert report["validation"]["ok"] and report["pending"] == 1
    out = Path(report["out"])
    with (out / "manifest.csv").open(newline="") as f:
        manifest = list(csv.DictReader(f))
    assert [r["image_id"] for r in manifest] == ["sample-0"]
    assert manifest[0]["label_origin"] == "human"
    assert not (out / "images/val/sample-1.jpg").exists()
    store.undo(approval["event_id"], "Nick")
    report = export_reviewed(settings)
    assert report["exported"] == 0
    assert (out / "list/train_gt.txt").read_text() == ""
    assert not (out / "images/train/sample-0.jpg").exists()
    assert json.loads((out / "READY.json").read_text())["complete_review"] is False


def test_no_lane_negative_is_exported_and_complete_review_marked(setup_review):
    settings, store = setup_review
    decide(store, action="corrected", lanes=[])
    decide(store, "sample-1")
    decide(store, "sample-2")
    report = export_reviewed(settings)
    assert report["state"] == "ready" and report["validation"]["all_splits_checked"] == 3
    out = Path(report["out"])
    assert (out / "list/train_gt.txt").read_text().strip().endswith("0 0 0 0")
    assert (out / "images/train/sample-0.lines.txt").read_text() == ""
    assert json.loads((out / "READY.json").read_text())["all_training_splits_nonempty"] is True


def test_phone_pairing_security_and_review_roundtrip(setup_review):
    settings, _ = setup_review
    app = create_app(settings, start_worker=False)
    with TestClient(app, base_url="http://localhost", client=("192.168.1.55", 23456)) as client:
        assert client.get("/api/stats").status_code == 401
        assert client.get("/api/session").json()["paired"] is False
        assert client.post("/api/pair", json={"code": "999999" if app.state.pairing["pin"] != "999999" else "111111", "reviewer": "Phone"}).status_code == 401
        response = client.post("/api/pair", json={"code": app.state.pairing["pin"], "reviewer": "Phone"})
        assert response.status_code == 200 and "HttpOnly" in response.headers["set-cookie"]
        assert client.get("/api/host").status_code == 403
        assert client.get("/api/stats", headers={"Origin": "https://unrelated.example"}).status_code == 403
        image = client.get("/api/next").json()
        response = client.post("/api/reviews", json={"image_id": image["image_id"], "action": "approved", "revision": image["revision"], "event_id": str(uuid.uuid4()), "reviewer": "Forged Name"})
        assert response.status_code == 200, response.text
        detail = client.get("/api/images/" + image["image_id"]).json()
        assert detail["geometry"]["reviewer"] == "Phone"
        assert client.get(image["image_url"]).status_code == 200
        assert client.post("/api/logout").status_code == 200
        assert client.get("/api/stats").status_code == 401


def test_pairing_allows_immediate_retry_and_rejects_untrusted_hosts(setup_review):
    settings, _ = setup_review
    app = create_app(settings, start_worker=False)
    with TestClient(app, base_url="http://localhost", client=("192.168.1.56", 23456)) as client:
        bad = "999999" if app.state.pairing["pin"] != "999999" else "111111"
        for _ in range(15):
            assert client.post("/api/pair", json={"code": bad}).status_code == 401
        assert client.post("/api/pair", json={"code": app.state.pairing["pin"]}).status_code == 200
        assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_malformed_pairing_codes_and_origins_are_rejected_cleanly(setup_review):
    settings, _ = setup_review
    app = create_app(settings, start_worker=False)
    with TestClient(app, base_url="http://localhost", client=("192.168.1.56", 23456)) as client:
        for code in ['é' * 6, '١٢٣٤٥٦', 'abcdef', '12345\n']:
            assert client.post('/api/pair', json={'code': code}).status_code == 422
        for origin in ['http://[', 'http://[invalid]', 'null', 'https://unrelated.example']:
            assert client.get('/api/session', headers={'Origin': origin}).status_code == 403
        assert client.post('/api/pair', json={'code': app.state.pairing['pin']}).status_code == 200
        assert client.get('/api/session', headers={'Origin': 'http://localhost'}).json()['paired']


def test_backup_keeps_sqlite_and_immutable_corrections(setup_review):
    settings, store = setup_review
    decide(store)
    folder = Path(store.backup())
    assert (folder / "metadata.sqlite").exists()
    assert len(json.loads((folder / "review_events.json").read_text())) == 1
    import zipfile
    with zipfile.ZipFile(folder / "human_labels.zip") as labels:
        assert len(labels.namelist()) == 1


def test_adopted_large_model_provenance_is_carried_into_export(setup_review):
    settings, store = setup_review
    path = settings.path("lane_geometry", "teachers", "sam3", "sample-0.json")
    path.parent.mkdir(parents=True)
    geo = store.detail("sample-0")["geometry"]
    path.write_text(json.dumps({**geo, "teacher": "sam3", "teacher_version": "sam3:pinned", "source_guided": False}))
    license_path = settings.path("licenses", "sam3-LICENSE.txt")
    license_path.parent.mkdir(exist_ok=True)
    license_path.write_text("test fixture license snapshot")
    points = [[450, 650], [550, 410]]
    decide(store, action="corrected", lanes=[{"slot":2, "points":points}], candidate="sam3")
    detail = store.detail("sample-0")
    assert detail["geometry"]["candidate_provenance"]["version"] == "sam3:pinned"
    assert detail["geometry"]["model_licenses"] == ["sam3"]
    report = export_reviewed(settings)
    out = Path(report["out"])
    assert (out / "licenses/sam3-LICENSE.txt").exists()
    with (out / "manifest.csv").open(newline="") as f:
        row = next(csv.DictReader(f))
    assert json.loads(row["model_licenses"]) == ["sam3"]


def test_resnet34_export_config_is_available_without_starting_training(setup_review):
    settings, store = setup_review
    settings.cfg["export"]["ufld"]["backbone"] = "34"
    report = export_reviewed(settings)
    config = Path(report["out"]) / "configs/brakecheck_culane_res34.py"
    assert "backbone= '34'" in config.read_text()
    assert report["training"] == "deferred"


def test_source_guided_large_model_is_not_scored_as_independent_accuracy(setup_review):
    settings, store = setup_review
    from bcdata.evaluate import evaluate
    from bcdata.teachers.base import TeacherOutput, save_output
    mask = np.zeros((874, 1164), np.float32)
    mask[410:650, 500:510] = 1
    paths = save_output(settings, "sam2_large", "fixture", "sample-2", TeacherOutput(lane=mask, meta={"source_guided": True}))
    fused = settings.path("annotations", "fused_predictions", "sample-2.json")
    fused.write_text(json.dumps({"mode":"source_label", "maps":{"lane":paths["lane"]}}))
    with store.registry() as reg:
        reg.add_annotation("sample-2", "teacher", "sam2_large", "fixture", paths["_meta"])
        reg.add_annotation("sample-2", "fused", "fusion", "fixture", str(fused))
        evaluation = evaluate(settings, reg, "test", teachers=["sam2_large"], write=False)
    assert evaluation["teachers"]["sam2_large"]["n"] == 0
    assert evaluation["reference_guided_excluded"] == {"sam2_large": 1}
