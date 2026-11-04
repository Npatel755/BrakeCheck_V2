"""Road-edge export semantics using disposable registry/images and real review transactions."""
from pathlib import Path
import json

from PIL import Image
import pytest

from bcdata.review_export import export_reviewed
from bcdata.review_store import ReviewStore
from bcdata.road_edge_export import validate_export
from tests.test_review import build_review_fixture, decide


@pytest.fixture
def review(tmp_path):
    settings = build_review_fixture(tmp_path)
    return settings, ReviewStore(settings)


def edges(left="visible", right="unknown"):
    return {
        "left": {"status": left, "kind": "pavement_edge", "points": [[200.25, 700.5], [410.75, 420.25]] if left == "visible" else []},
        "right": {"status": right, "kind": "curb", "points": [[1030.5, 750.75], [720.25, 440.5]] if right == "visible" else []},
    }


def artifact(report):
    out = Path(report["road_edges"]["out"])
    records = [json.loads(line) for line in (out / "annotations.jsonl").read_text().splitlines()]
    return out, records


def test_legacy_paint_approvals_never_become_negative_road_edges(review):
    settings, store = review
    for index in range(3):
        decide(store, f"sample-{index}")
    report = export_reviewed(settings)
    out, records = artifact(report)
    assert report["exported"] == 3
    assert report["road_edges"]["exported"] == 0 and records == []
    assert report["road_edges"]["excluded_not_reviewed"] == 3
    assert report["road_edges"]["status_counts_accepted"]["right"]["not_reviewed"] == 3
    assert report["road_edges"]["validation"]["ok"]
    assert json.loads((out / "READY.json").read_text())["training_ready"] is False
    painted = Path(report["out"])
    assert (painted / "list/train_gt.txt").read_text().strip().endswith("0 1 0 0")


def test_exact_source_edge_points_and_unknown_mask_do_not_enter_painted_slots(review):
    settings, store = review
    decide(store, action="corrected", lanes=[], road_edges=edges())
    report = export_reviewed(settings)
    out, records = artifact(report)
    record = records[0]
    assert record["road_edges"]["left"]["points"] == edges()["left"]["points"]
    assert record["road_edges"]["left"]["training_mask"] is True
    assert record["road_edges"]["left"]["is_negative"] is False
    assert record["road_edges"]["right"] == {"status": "unknown", "kind": None, "points": [], "training_mask": False, "is_negative": False}
    assert record["suggested_crop"] == {"source_xyxy": [0.0, 240.0, 1164.0, 659.0], "canvas_width": 1640, "canvas_height": 590, "applied": False}
    # The lower edge points exceed the suggested crop; they must remain intact.
    assert record["road_edges"]["left"]["points"][0][1] > record["suggested_crop"]["source_xyxy"][3]
    with Image.open(out / record["image"]) as photo:
        assert photo.size == (1164, 874)
    assert record["human_review"]["label_origin"] == "human"
    assert record["source_image"]["pool"] == "commercial"
    assert report["road_edges"]["revision"] == report["revision"]
    assert report["road_edges"]["status_counts_exported"]["right"] == {"unknown": 1}
    assert (Path(report["out"]) / "list/train_gt.txt").read_text().strip().endswith("0 0 0 0")
    assert (Path(report["out"]) / "images/train/sample-0.lines.txt").read_text() == ""
    policy = json.loads((out / "policy.json").read_text())
    assert policy["statuses"]["unknown"]["training_mask"] is False
    assert "adapter" in policy["adapter_required"]


def test_only_both_reviewed_accepted_commercial_pairs_export_and_absent_is_explicit_negative(review):
    settings, store = review
    decide(store, action="corrected", lanes=[], road_edges=edges(left="absent", right="visible"))
    decide(store, "sample-1", "corrected", lanes=[], road_edges=edges(right="not_reviewed"))
    decide(store, "sample-2", "rejected")
    report = export_reviewed(settings)
    _, records = artifact(report)
    assert [record["image_id"] for record in records] == ["sample-0"]
    assert records[0]["road_edges"]["left"]["is_negative"] is True
    assert records[0]["road_edges"]["left"]["training_mask"] is True
    assert report["road_edges"]["excluded_not_reviewed"] == 1
    # Commercial-pool filtering is inherited from the same export snapshot.
    with store.registry() as reg:
        reg.conn.execute("UPDATE images SET pool='quarantine' WHERE image_id='sample-0'")
        reg.conn.commit()
    report = export_reviewed(settings)
    out, records = artifact(report)
    assert records == []
    assert not (out / "images/train/sample-0.png").exists()


def test_undo_removes_current_edge_artifacts_but_preserves_source_and_audit(review):
    settings, store = review
    decide(store)
    correction = decide(store, action="corrected", revision=1, lanes=[], road_edges=edges(right="absent"))
    report = export_reviewed(settings)
    out, records = artifact(report)
    image = out / records[0]["image"]
    assert image.exists()
    with store.registry() as reg:
        immutable_human = Path(reg.annotation("sample-0", "human")["path"])
        source = Path(reg.image("sample-0")["path"])
    store.undo(correction["event_id"], "Nick")
    report = export_reviewed(settings)
    _, records = artifact(report)
    assert records == [] and not image.exists()
    assert source.exists() and immutable_human.exists()
    assert report["exported"] == 1  # Older painted approval is still current.
    assert report["road_edges"]["excluded_not_reviewed"] == 1


def test_road_edge_export_preserves_adopted_candidate_licenses_and_pinned_provenance(review):
    settings, store = review
    candidate = settings.path("lane_geometry", "teachers", "sam3", "sample-0.json")
    candidate.parent.mkdir(parents=True)
    candidate.write_text(json.dumps({"teacher_version": "sam3:pinned", "source_guided": False,
                                     "model_meta": {"repository_revision": "fixture-revision", "license_sha256": "fixture-sha"}}))
    license_path = settings.path("licenses", "sam3-LICENSE.txt")
    license_path.parent.mkdir(parents=True, exist_ok=True)
    license_path.write_text("exact fixture license snapshot")
    decide(store, action="corrected", lanes=[], road_edges=edges(), candidate="sam3")
    report = export_reviewed(settings)
    out, records = artifact(report)
    human = records[0]["human_review"]
    assert human["candidate_provenance"]["repository_revision"] == "fixture-revision"
    assert human["candidate_provenance"]["license_sha256"] == "fixture-sha"
    assert human["model_licenses"] == ["sam3"]
    assert (out / "licenses/sam3-LICENSE.txt").read_text() == license_path.read_text()
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["license_ids"] == ["comma10k", "sam3"]


def test_validation_rejects_unknown_side_accidentally_used_as_negative(review):
    settings, store = review
    decide(store, action="corrected", lanes=[], road_edges=edges())
    report = export_reviewed(settings)
    out, records = artifact(report)
    records[0]["road_edges"]["right"]["training_mask"] = True
    records[0]["road_edges"]["right"]["is_negative"] = True
    (out / "annotations.jsonl").write_text(json.dumps(records[0]) + "\n")
    validation = validate_export(out)
    assert not validation["ok"] and "Unsafe road-edge supervision mask" in validation["errors"][0]
