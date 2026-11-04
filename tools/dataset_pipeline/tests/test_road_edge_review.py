"""Road-edge review policy tests; every write uses disposable fixture images."""
import copy
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from bcdata.review_server import create_app
from bcdata.review_store import ReviewError, ReviewStore
from .test_review import decide, setup_review


POLICY = {
    "version": "brakecheck-boundaries-v2",
    "painted_lanes": "visible_paint_only",
    "road_edges": "visible_road_surface_boundary",
}


def edges(right="visible"):
    return {
        "left": {"status": "unknown", "points": []},
        "right": {"status": right, **({"kind": "curb", "points": [[900, 650], [790, 530], [700, 410]]}
                                        if right == "visible" else {"points": []})},
    }


def painted():
    return [{"slot": 2, "kind": "dashed", "points": [[450, 650], [550, 410]]}]


def test_legacy_proposal_has_unreviewed_edges_without_invented_negatives(setup_review):
    settings, store = setup_review
    geometry = store.detail("sample-0")["geometry"]
    assert geometry["labeling_policy"] == POLICY
    assert {side: edge["status"] for side, edge in geometry["road_edges"].items()} == {
        "left": "not_reviewed", "right": "not_reviewed",
    }
    decide(store)
    saved = ReviewStore(settings).detail("sample-0")["geometry"]
    assert all(edge["status"] == "not_reviewed" for edge in saved["road_edges"].values())
    # Approving old painted labels does not silently review either road edge.
    assert len(saved["lanes"]) == 1


def test_visible_road_edge_roundtrip_keeps_source_coordinates_and_paint_slots(setup_review):
    settings, store = setup_review
    expected = edges()
    # Road-edge records retain the full source image; UFLD's painted-line crop
    # must not truncate their vertices or round hand-placed coordinates.
    expected["right"]["points"] = [[900.125, 720.75], [790.5, 530.25], [700.25, 350.125]]
    decide(store, action="corrected", lanes=painted(), road_edges=expected)
    geometry = ReviewStore(settings).detail("sample-0")["geometry"]
    assert geometry["labeling_policy"] == POLICY
    assert geometry["road_edges"]["right"]["status"] == "visible"
    assert geometry["road_edges"]["right"]["kind"] == "curb"
    assert geometry["road_edges"]["right"]["points"] == expected["right"]["points"]
    assert geometry["road_edges"]["right"]["fit"]["ys"] == [350.125, 530.25, 720.75]
    assert geometry["road_edges"]["left"]["status"] == "unknown"
    assert [lane["slot"] for lane in geometry["lanes"]] == [2]
    with store.registry() as reg:
        path = reg.annotation("sample-0", "human")["path"]
    assert json.loads(open(path, encoding="utf-8").read())["road_edges"] == geometry["road_edges"]


def test_absent_unknown_and_not_reviewed_remain_distinct(setup_review):
    _, store = setup_review
    decide(store, action="corrected", lanes=[], road_edges={
        "left": {"status": "absent", "points": []},
        "right": {"status": "unknown", "points": []},
    })
    geometry = store.detail("sample-0")["geometry"]
    assert geometry["road_edges"]["left"]["status"] == "absent"
    assert geometry["road_edges"]["right"]["status"] == "unknown"
    assert geometry["lanes"] == []
    decide(store, image_id="sample-1", action="corrected", lanes=[], road_edges={"left": {"status": "absent"}})
    assert store.detail("sample-1")["geometry"]["road_edges"]["right"]["status"] == "not_reviewed"


def test_omitted_edges_preserve_previous_human_edges_and_undo_restores_them(setup_review):
    _, store = setup_review
    first = decide(store, action="corrected", lanes=painted(), road_edges=edges())
    original = copy.deepcopy(store.detail("sample-0")["geometry"]["road_edges"])
    decide(store, action="corrected", revision=1, lanes=[], road_edges=None)
    assert store.detail("sample-0")["geometry"]["road_edges"] == original
    updated = decide(store, action="corrected", revision=2, lanes=[], road_edges=edges("absent"))
    assert store.detail("sample-0")["geometry"]["road_edges"]["right"]["status"] == "absent"
    store.undo(updated["event_id"], "Nick")
    assert store.detail("sample-0")["geometry"]["road_edges"] == original
    assert first["event_id"] != updated["event_id"]


def test_model_adoption_preserves_existing_human_road_edges(setup_review):
    settings, store = setup_review
    decide(store, action="corrected", lanes=painted(), road_edges=edges())
    original = copy.deepcopy(store.detail("sample-0")["geometry"]["road_edges"])
    candidate = settings.path("lane_geometry", "teachers", "sam3", "sample-0.json")
    candidate.parent.mkdir(parents=True)
    candidate.write_text(json.dumps({"teacher": "sam3", "teacher_version": "sam3:pinned", "source_guided": False}), encoding="utf-8")
    decide(store, action="corrected", revision=1, lanes=painted(), candidate="sam3")
    geometry = store.detail("sample-0")["geometry"]
    assert geometry["road_edges"] == original
    assert geometry["candidate_provenance"]["version"] == "sam3:pinned"


def test_plain_approval_does_not_apply_an_unsaved_edge_draft(setup_review):
    _, store = setup_review
    decide(store, action="corrected", lanes=painted(), road_edges=edges())
    original = copy.deepcopy(store.detail("sample-0")["geometry"]["road_edges"])
    decide(store, revision=1, road_edges=edges("absent"))
    assert store.detail("sample-0")["geometry"]["road_edges"] == original


def test_explicit_reset_returns_edges_to_unreviewed(setup_review):
    _, store = setup_review
    decide(store, action="corrected", lanes=painted(), road_edges=edges())
    decide(store, action="corrected", revision=1, lanes=painted(), road_edges={})
    geometry = store.detail("sample-0")["geometry"]
    assert all(edge["status"] == "not_reviewed" and edge["points"] == []
               for edge in geometry["road_edges"].values())


@pytest.mark.parametrize("bad_edge", [
    {"status": "visible", "kind": "curb", "points": [[900, 650]]},
    {"status": "visible", "kind": "curb", "points": [[900, 650], [790, 650]]},
    {"status": "visible", "kind": "curb", "points": [[1164, 650], [790, 410]]},
    {"status": "visible", "kind": "curb", "points": [[900, 650], [float("nan"), 410]]},
    {"status": "visible", "kind": "curb", "points": [[True, 650], [790, 410]]},
    {"status": "visible", "kind": "shadow", "points": [[900, 650], [790, 410]]},
    {"status": "absent", "points": [[900, 650], [790, 410]]},
    {"status": "unknown", "points": [[900, 650], [790, 410]]},
    {"status": "guessed", "points": []},
])
def test_invalid_edge_is_rejected_transactionally(setup_review, bad_edge):
    _, store = setup_review
    with pytest.raises(ReviewError):
        decide(store, action="corrected", lanes=painted(), road_edges={"right": bad_edge})
    assert store.stats()["reviewed"] == 0
    assert store.detail("sample-0")["revision"] == 0


def test_retry_fingerprint_includes_edge_changes(setup_review):
    _, store = setup_review
    event_id = str(uuid.uuid4())
    payload = dict(lanes=painted(), road_edges=edges())
    store.decide("sample-0", "corrected", "Nick", 0, event_id, **payload)
    retry = store.decide("sample-0", "corrected", "Nick", 0, event_id, **payload)
    assert retry["idempotent"] is True
    with pytest.raises(ReviewError, match="different content"):
        store.decide("sample-0", "corrected", "Nick", 0, event_id, lanes=painted(), road_edges=edges("absent"))
    assert store.stats()["reviewed"] == 1


def test_phone_api_saves_edges_and_rejects_invalid_geometry(setup_review):
    settings, store = setup_review
    app = create_app(settings, start_worker=False)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        body = {"image_id": "sample-0", "action": "corrected", "revision": 0,
                "event_id": str(uuid.uuid4()), "lanes": painted(), "road_edges": edges()}
        response = client.post("/api/reviews", json=body)
        assert response.status_code == 200, response.text
        actual = client.get("/api/images/sample-0").json()["geometry"]["road_edges"]
        assert actual["right"]["points"] == edges()["right"]["points"]
        body.update(image_id="sample-1", event_id=str(uuid.uuid4()), road_edges={"right": {
            "status": "visible", "kind": "curb", "points": [[900, 650], [900, 650]],
        }})
        response = client.post("/api/reviews", json=body)
        assert response.status_code in (400, 422), response.text
    assert store.stats()["reviewed"] == 1
