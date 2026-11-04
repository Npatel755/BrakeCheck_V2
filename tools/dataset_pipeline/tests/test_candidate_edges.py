"""Explicit model adoption for separate label groups; disposable data only."""
from pathlib import Path
import copy
import hashlib
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from bcdata.review_export import export_reviewed
from bcdata.review_server import create_app
from bcdata.review_store import ReviewError
from .test_review import decide, setup_review
from .test_road_edge_review import edges, painted


def candidate(settings, name="sam3", version=None, with_edges=True, **extra):
    path = settings.path("lane_geometry", "teachers", name, "sample-0.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    geometry = {
        "teacher": name, "teacher_version": version or name + ":fixture", "source_guided": name == "sam2_large",
        "lanes": painted(), "model_meta": {"repository_revision": name + "-pinned", "license_sha256": name + "-license-sha"},
        **extra,
    }
    if with_edges:
        geometry["road_edges"] = edges()
    path.write_text(json.dumps(geometry), encoding="utf-8")
    license_path = settings.path("licenses", name + "-LICENSE.txt")
    license_path.parent.mkdir(exist_ok=True)
    license_path.write_text(name + " fixture license", encoding="utf-8")
    return geometry


def edge_args(version="sam3:fixture"):
    return {"road_edges": edges(), "road_edge_candidate": "sam3", "road_edge_candidate_version": version}


def test_detail_capabilities_distinguish_edges_and_empty_source_refinement(setup_review):
    settings, store = setup_review
    candidate(settings)
    candidate(settings, "sam2_large", with_edges=False, lanes=[], model_meta={"components": 0})
    options = {item["name"]: item for item in store.detail("sample-0")["teacher_options"]}
    assert options["sam3"]["version"] == "sam3:fixture"
    assert options["sam3"]["capabilities"] == {"painted_lanes": True, "road_edges": True}
    assert options["sam3"]["summary"]["road_edges"] == 1
    assert options["sam3"]["summary"]["inference"] == "independent"
    assert options["sam2_large"]["capabilities"]["road_edges"] is False
    assert options["sam2_large"]["summary"]["painted_lines"] == 0
    assert "No source paint to refine" in options["sam2_large"]["summary"]["empty_reason"]
    candidate(settings, with_edges=False)
    sam3 = next(item for item in store.detail("sample-0")["teacher_options"] if item["name"] == "sam3")
    assert sam3["capabilities"]["road_edges"] is False


def test_adopting_edges_preserves_painted_geometry_provenance_and_license_dependencies(setup_review):
    settings, store = setup_review
    candidate(settings, "sam2_large", with_edges=False)
    candidate(settings)
    decide(store, action="corrected", lanes=painted(), candidate="sam2_large", candidate_version="sam2_large:fixture")
    before = copy.deepcopy(store.detail("sample-0")["geometry"])
    adopted = decide(store, action="corrected", revision=1, lanes=painted(), **edge_args())
    geometry = store.detail("sample-0")["geometry"]
    assert geometry["lanes"] == before["lanes"]
    assert geometry["candidate_provenance"] == before["candidate_provenance"]
    assert geometry["road_edge_candidate_provenance"] == {
        "name": "sam3", "version": "sam3:fixture", "source_guided": False,
        "repository_revision": "sam3-pinned", "license_sha256": "sam3-license-sha",
    }
    assert geometry["model_licenses"] == ["sam2", "sam3"]
    # Later painted-model adoption must not erase the road-edge dependency.
    decide(store, action="corrected", revision=2, lanes=painted(), candidate="sam2_large", candidate_version="sam2_large:fixture")
    latest = store.detail("sample-0")["geometry"]
    assert latest["road_edge_candidate_provenance"] == geometry["road_edge_candidate_provenance"]
    assert latest["road_edges"] == geometry["road_edges"]
    assert latest["model_licenses"] == ["sam2", "sam3"]
    assert adopted["decision"] == "corrected"


def test_edge_provenance_and_licenses_survive_export_and_saved_decision_undo(setup_review):
    settings, store = setup_review
    candidate(settings)
    decide(store, action="corrected", lanes=painted(), road_edges=edges("absent"))
    prior = copy.deepcopy(store.detail("sample-0")["geometry"])
    adoption = decide(store, action="corrected", revision=1, lanes=painted(), **edge_args())
    report = export_reviewed(settings)
    out = Path(report["road_edges"]["out"])
    record = json.loads((out / "annotations.jsonl").read_text().splitlines()[0])
    assert record["human_review"]["road_edge_candidate_provenance"]["version"] == "sam3:fixture"
    assert record["human_review"]["candidate_provenance"] == {}
    assert record["human_review"]["model_licenses"] == ["sam3"]
    assert (out / "licenses/sam3-LICENSE.txt").read_text() == "sam3 fixture license"
    store.undo(adoption["event_id"], "Nick")
    geometry = store.detail("sample-0")["geometry"]
    assert geometry["road_edges"] == prior["road_edges"]
    assert "road_edge_candidate_provenance" not in geometry
    assert geometry.get("model_licenses", []) == []


@pytest.mark.parametrize("group", ["painted", "edges"])
def test_changed_artifact_version_rejects_new_save_transactionally(setup_review, group):
    settings, store = setup_review
    candidate(settings, version="sam3:newer")
    kwargs = ({"candidate": "sam3", "candidate_version": "sam3:fixture"} if group == "painted" else edge_args())
    with pytest.raises(ReviewError, match="candidate changed") as error:
        decide(store, action="corrected", lanes=painted(), **kwargs)
    assert error.value.status == 409
    assert store.stats()["reviewed"] == 0 and store.detail("sample-0")["revision"] == 0


@pytest.mark.parametrize("kwargs", [
    {"road_edge_candidate": "sam3", "road_edges": edges()},
    {"road_edge_candidate_version": "sam3:fixture", "road_edges": edges()},
    {"road_edge_candidate": "sam3", "road_edge_candidate_version": "sam3:fixture"},
    {"road_edge_candidate": "../sam3", "road_edge_candidate_version": "sam3:fixture", "road_edges": edges()},
])
def test_incomplete_or_unknown_edge_adoption_is_rejected(setup_review, kwargs):
    _, store = setup_review
    with pytest.raises(ReviewError):
        decide(store, action="corrected", lanes=painted(), **kwargs)
    assert store.stats()["reviewed"] == 0


def test_edge_adoption_requires_correction_and_actual_edge_candidate(setup_review):
    settings, store = setup_review
    candidate(settings, with_edges=False)
    with pytest.raises(ReviewError, match="Save a correction"):
        decide(store, **edge_args())
    with pytest.raises(ReviewError, match="no road-edge labels"):
        decide(store, action="corrected", lanes=painted(), **edge_args())


def test_same_adoption_retry_is_safe_after_artifact_update_but_changed_request_is_not(setup_review):
    settings, store = setup_review
    candidate(settings)
    event = str(uuid.uuid4())
    kwargs = dict(lanes=painted(), **edge_args())
    store.decide("sample-0", "corrected", "Nick", 0, event, **kwargs)
    candidate(settings, version="sam3:newer")
    assert store.decide("sample-0", "corrected", "Nick", 0, event, **kwargs)["idempotent"] is True
    with pytest.raises(ReviewError, match="different content"):
        store.decide("sample-0", "corrected", "Nick", 0, event, lanes=painted(), **edge_args("sam3:newer"))
    assert store.stats()["reviewed"] == 1


def test_pre_candidate_version_pending_request_keeps_its_fingerprint(setup_review):
    _, store = setup_review
    event = str(uuid.uuid4())
    road_edges = edges()
    body = {"image_id": "sample-0", "action": "corrected", "reviewer": "Nick", "revision": 0,
            "lanes": painted(), "notes": "", "candidate": None, "road_edges": road_edges}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    store.decide("sample-0", "corrected", "Nick", 0, event, lanes=painted(), road_edges=road_edges)
    with store.registry() as reg:
        saved = json.loads(reg.conn.execute("SELECT after_state FROM review_events WHERE event_id=?", (event,)).fetchone()[0])
    assert saved["request_digest"] == digest
    assert store.decide("sample-0", "corrected", "Nick", 0, event, lanes=painted(), road_edges=road_edges)["idempotent"] is True


def test_api_forwards_both_explicit_adoption_versions(setup_review):
    settings, store = setup_review
    candidate(settings)
    app = create_app(settings, start_worker=False)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        body = {"image_id": "sample-0", "action": "corrected", "revision": 0, "event_id": str(uuid.uuid4()),
                "lanes": painted(), "candidate": "sam3", "candidate_version": "sam3:fixture", **edge_args()}
        response = client.post("/api/reviews", json=body)
        assert response.status_code == 200, response.text
        geometry = client.get("/api/images/sample-0").json()["geometry"]
    assert geometry["candidate_provenance"]["version"] == "sam3:fixture"
    assert geometry["road_edge_candidate_provenance"]["version"] == "sam3:fixture"
    assert geometry["model_licenses"] == ["sam3"]
