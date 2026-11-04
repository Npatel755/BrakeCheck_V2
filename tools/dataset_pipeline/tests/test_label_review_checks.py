"""Targeted annotation checks preserve existing labels until a real review."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from bcdata.review_server import create_app
from bcdata.review_store import ReviewError, ReviewStore
from tests.test_review import build_review_fixture
from tests.test_source_validation import approve_samples, setup_source
from bcdata.source_validation import refresh_groups


def candidate(image_id, observation="Check visible paint endpoints."):
    return {"image_id": image_id, "topics": ["visible_span"], "observation": observation}


def save(store, image_id="sample-0", action="approved", **kwargs):
    detail = store.detail(image_id)
    return store.decide(image_id, action, "Nick", detail["revision"], str(uuid.uuid4()), **kwargs)


def state(store, image_id):
    with store.registry() as reg:
        return {
            "image": dict(reg.image(image_id)),
            "annotations": [dict(row) for row in reg.conn.execute("SELECT * FROM annotations WHERE image_id=? ORDER BY id", (image_id,))],
            "events": [dict(row) for row in reg.conn.execute("SELECT * FROM review_events WHERE image_id=? ORDER BY rowid", (image_id,))],
            "reviews": [dict(row) for row in reg.conn.execute("SELECT * FROM reviews WHERE image_id=? ORDER BY id", (image_id,))],
            "qc": dict(reg.conn.execute("SELECT * FROM qc WHERE image_id=?", (image_id,)).fetchone()),
            "version": store._revision(reg, image_id),
            "source_check": [dict(row) for row in reg.conn.execute("SELECT * FROM source_conversion_checks WHERE image_id=?", (image_id,))],
        }


def test_enqueue_is_durable_idempotent_and_preserves_saved_labels(tmp_path):
    settings = build_review_fixture(tmp_path)
    store = ReviewStore(settings)
    save(store)
    before = state(store, "sample-0")
    geo = store.detail("sample-0")["geometry"]
    result = store.enqueue_label_checks([candidate("sample-0")], "misses-1")
    assert result["enqueued"] == 1
    assert state(store, "sample-0") == before
    store = ReviewStore(settings)
    again = store.enqueue_label_checks([candidate("sample-0", "Do not replace the original reason")], "misses-2")
    assert again["already_present"] == 1 and again["enqueued"] == 0
    detail = store.detail("sample-0")
    assert detail["geometry"] == geo and detail["review_status"] == "approved"
    assert detail["label_check"]["batch_id"] == "misses-1"
    assert detail["label_check"]["observation"] == "Check visible paint endpoints."
    assert store.stats()["label_checks_pending"] == store.stats()["label_checks_total_pending"] == 1
    assert store.detail("sample-1")["label_check"] is None


@pytest.mark.parametrize("bad", [
    [candidate("sample-0"), candidate("missing")],
    [candidate("sample-0"), candidate("sample-0")],
    [{"image_id": "sample-0", "topics": [], "observation": "reason"}],
    [{"image_id": "sample-0", "topics": ["visible_span"], "observation": ""}],
])
def test_invalid_import_is_atomic(tmp_path, bad):
    store = ReviewStore(build_review_fixture(tmp_path))
    with pytest.raises(ReviewError):
        store.enqueue_label_checks(bad, "batch")
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM label_review_checks") == 0


def test_excluded_images_are_rejected_without_partial_import(tmp_path):
    store = ReviewStore(build_review_fixture(tmp_path))
    with store.registry() as reg:
        reg.update_image("sample-1", status="excluded")
    with pytest.raises(ReviewError, match="excluded"):
        store.enqueue_label_checks([candidate("sample-0"), candidate("sample-1")], "batch")
    assert store.stats()["label_checks_total_pending"] == 0


def test_queue_fetches_existing_approved_and_source_validated_without_resetting(tmp_path):
    settings, store, samples, held = setup_source(tmp_path)
    approve_samples(store, samples)
    refresh_groups(settings)
    approved = samples[0]
    before = {image_id: state(store, image_id) for image_id in (approved, held)}
    store.enqueue_label_checks([candidate(approved), candidate(held)], "batch")
    assert state(store, held) == before[held]
    assert state(store, approved) == before[approved]
    items = store.batch("reviewer", status="label_check")
    assert {item["image_id"] for item in items} == {approved, held}
    assert next(item for item in items if item["image_id"] == approved)["review_status"] == "approved"
    assert next(item for item in items if item["image_id"] == held)["source_conversion"]["state"] == "validated"
    assert store.next_image("other", status="label_check") is None
    assert store.stats()["source_validated"] == 1


@pytest.mark.parametrize("action", ["approved", "corrected", "rejected"])
def test_real_decision_completes_task_and_undo_reopens_exact_task(tmp_path, action):
    store = ReviewStore(build_review_fixture(tmp_path))
    save(store)
    before = store.detail("sample-0")
    store.enqueue_label_checks([candidate("sample-0")], "batch")
    result = save(store, action=action, **({"lanes": []} if action == "corrected" else {}))
    detail = store.detail("sample-0")
    assert detail["label_check"]["state"] == "completed"
    assert detail["label_check"]["completed_event"] == result["event_id"]
    assert store.stats()["label_checks_total_pending"] == 0
    assert store.next_image("owner", status="label_check") is None
    assert store.enqueue_label_checks([candidate("sample-0")], "batch")["enqueued"] == 0
    assert store.detail("sample-0")["label_check"]["state"] == "completed"
    store.undo(result["event_id"], "Nick")
    reopened = store.detail("sample-0")
    assert reopened["label_check"]["state"] == "pending"
    assert reopened["label_check"]["completed_event"] is None
    assert reopened["review_status"] == before["review_status"]
    assert reopened["geometry"]["lanes"] == before["geometry"]["lanes"]
    assert store.stats()["label_checks_pending"] == 1


def test_retrying_old_approval_does_not_complete_new_check(tmp_path):
    store = ReviewStore(build_review_fixture(tmp_path))
    event_id = str(uuid.uuid4())
    store.decide("sample-0", "approved", "Nick", 0, event_id)
    store.enqueue_label_checks([candidate("sample-0")], "batch")
    assert store.decide("sample-0", "approved", "Nick", 0, event_id)["idempotent"]
    assert store.detail("sample-0")["label_check"]["state"] == "pending"


def test_legacy_undo_leaves_pending_task_untouched(tmp_path):
    store = ReviewStore(build_review_fixture(tmp_path))
    result = save(store)
    with store.registry() as reg:
        row = reg.conn.execute("SELECT before_state,after_state FROM review_events WHERE event_id=?", (result["event_id"],)).fetchone()
        snapshots = []
        for key in ("before_state", "after_state"):
            snapshot = json.loads(row[key])
            snapshot.pop("label_check", None)
            snapshots.append(json.dumps(snapshot))
        reg.conn.execute("UPDATE review_events SET before_state=?,after_state=? WHERE event_id=?", (*snapshots, result["event_id"]))
        reg.conn.commit()
    store.enqueue_label_checks([candidate("sample-0")], "batch")
    store.undo(result["event_id"], "Nick")
    assert store.detail("sample-0")["label_check"]["state"] == "pending"


def test_skip_moves_existing_approval_to_later_without_human_decision(tmp_path):
    settings = build_review_fixture(tmp_path)
    store = ReviewStore(settings)
    save(store)
    store.enqueue_label_checks([candidate("sample-0")], "batch")
    before = state(store, "sample-0")
    detail = store.next_image("owner", status="label_check")
    store.defer("sample-0", detail["revision"], "Nick", "owner")
    store = ReviewStore(settings)
    assert state(store, "sample-0") == before
    assert store.stats()["label_checks_pending"] == 0
    assert store.stats()["label_checks_total_pending"] == 1
    assert store.stats()["review_later"] == 1
    assert store.leaderboard()["reviewers"][0]["review_later"] == 1
    assert store.next_image("owner", status="label_check") is None
    assert store.batch("owner", status="label_check") == []
    assert store.next_image("owner", status="later")["image_id"] == "sample-0"
    assert store.batch("owner", status="later")[0]["image_id"] == "sample-0"
    store.defer("sample-0", detail["revision"], "Nick", "owner", False)
    assert store.stats()["label_checks_pending"] == 1
    assert store.stats()["review_later"] == 0
    store.defer("sample-0", detail["revision"], "Nick", "owner")
    save(store, owner="owner")
    assert not store.detail("sample-0")["review_later"]
    with pytest.raises(ReviewError, match="saved decision"):
        store.defer("sample-0", store.detail("sample-0")["revision"], "Nick", "owner")


def test_claims_exclusions_and_split_filter_work_for_label_checks(tmp_path):
    store = ReviewStore(build_review_fixture(tmp_path))
    for image_id in ("sample-0", "sample-1", "sample-2"):
        save(store, image_id)
    store.enqueue_label_checks([candidate(f"sample-{n}") for n in range(3)], "batch")
    first = store.next_image("first", split="train", status="label_check")
    assert first["image_id"] == "sample-0"
    assert store.next_image("second", split="train", status="label_check") is None
    with pytest.raises(ReviewError, match="Another reviewer"):
        store.defer("sample-0", first["revision"], "Nick", "second")
    with pytest.raises(ReviewError, match="Another reviewer"):
        save(store, owner="second")
    items = store.batch("second", status="label_check", exclude=["sample-2"])
    assert [item["image_id"] for item in items] == ["sample-1"]
    assert store.next_image("third", status="label_check")["image_id"] == "sample-2"
    assert store.next_image("fourth", status="label_check") is None


def test_two_reviewers_cannot_claim_the_same_label_check(tmp_path):
    store = ReviewStore(build_review_fixture(tmp_path))
    save(store)
    store.enqueue_label_checks([candidate("sample-0")], "batch")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda owner: store.next_image(owner, status="label_check"), ("one", "two")))
    assert sum(result is not None for result in results) == 1


def test_completed_task_is_not_reopened_by_undo_of_later_noncheck_decision(tmp_path):
    store = ReviewStore(build_review_fixture(tmp_path))
    store.enqueue_label_checks([candidate("sample-0")], "batch")
    first = save(store)
    later = save(store, action="rejected")
    store.undo(later["event_id"], "Nick")
    check = store.detail("sample-0")["label_check"]
    assert check["state"] == "completed" and check["completed_event"] == first["event_id"]


def test_http_next_and_batch_expose_category_metadata(tmp_path):
    settings = build_review_fixture(tmp_path)
    store = ReviewStore(settings)
    save(store)
    store.enqueue_label_checks([candidate("sample-0")], "batch")
    app = create_app(settings, start_worker=False)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 123)) as client:
        response = client.get("/api/next?status=label_check")
        assert response.status_code == 200
        assert response.json()["label_check"]["topics"] == ["visible_span"]
        response = client.get("/api/batch?status=label_check")
        assert response.status_code == 200
        assert [item["image_id"] for item in response.json()] == ["sample-0"]