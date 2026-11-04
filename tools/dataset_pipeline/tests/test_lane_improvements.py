import json
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from bcdata.lane_improvements import credible_trace, source_sampling
from bcdata.lane_error_artifacts import ErrorCollection, match_lanes
from bcdata.lane_error_review import ErrorDecision, LaneErrorReview, create_lane_error_router


def test_source_balancing_preserves_step_budget_and_equal_source_probability():
    records = [{'source': s, 'split': 'train'} for s, count in [('audi', 3), ('comma', 4), ('college', 20)] for _ in range(count)]
    weights, metadata = source_sampling(records, 'balanced')
    assert len(weights) == metadata['samples_per_epoch'] == len(records)
    mass = [sum(w for r, w in zip(records, weights) if r['source'] == s) for s in metadata['source_counts']]
    np.testing.assert_allclose(mass, [1, 1, 1])
    assert source_sampling(records, 'uniform')[0] is None
    with pytest.raises(ValueError):
        source_sampling(records + [{'source': 'held', 'split': 'val'}], 'balanced')


def test_guard_keeps_smooth_short_lane_rejects_zigzags_jumps_and_nonfinite():
    assert credible_trace([[300, 300], [302, 310], [305, 320], [309, 330]])
    assert not credible_trace([[300, 300], [900, 310], [300, 320], [900, 330]])
    assert not credible_trace([[300, 300], [302, 310], [305, 320], [900, 530]])
    assert not credible_trace([[300, 300], [302, 310], [float('nan'), 320], [309, 330]])


@pytest.fixture
def review(tmp_path):
    snapshot = tmp_path / 'models/ufld_baseline/run-test/dataset'
    image = snapshot / 'images/val/photo-1.jpg'
    image.parent.mkdir(parents=True)
    from PIL import Image
    Image.new('RGB', (1640, 590)).save(image)
    writer = ErrorCollection(tmp_path, 'run-test-initial', snapshot, {'stage': 'initial', 'checkpoint_sha256': 'a'*64})
    reference = [[[300, 300], [330, 500]]]
    false = [[[800, 300], [830, 500]]]
    record = {'image_id': 'photo-1', 'image': 'images/val/photo-1.jpg', 'split': 'val', 'source': 'audi'}
    writer.add(record, reference, {'stock': false, 'short_span_v1': reference})
    writer.finish({})
    return LaneErrorReview(tmp_path), writer, record


def test_matching_and_review_categorization(review):
    store, writer, record = review
    assert match_lanes([], [[[1, 2], [3, 4]]])['fn'] == 1
    for kind in ('all', 'missed', 'false_positive', 'geometry'):
        queue = store.queue(writer.metadata['id'], 'stock', kind, 'pending', '', 1)
        assert queue['total'] == 1 and queue['items'][0]['counts'] == {'tp': 0, 'fp': 1, 'fn': 1}
    assert store.queue(writer.metadata['id'], 'short_span_v1', 'all', 'pending', '', 1)['total'] == 0
    assert store.photo(writer.metadata['id'], record['image_id']).is_file()


def test_notes_are_isolated_and_fingerprint_bound(review):
    store, writer, record = review
    item = store.case(writer.metadata['id'], record['image_id'])
    before = {p: p.read_bytes() for p in writer.snapshot.rglob('*') if p.is_file()}
    decision = ErrorDecision(image_id=record['image_id'], policy='stock', fingerprint=item['fingerprint'],
                             action='label_question', notes='Check the endpoint')
    assert store.decide(writer.metadata['id'], decision, 'Nick')['labels_changed'] is False
    assert store.queue(writer.metadata['id'], 'stock', 'all', 'pending', '', 1)['total'] == 0
    assert store.queue(writer.metadata['id'], 'stock', 'all', 'reviewed', '', 1)['total'] == 1
    assert all(p.read_bytes() == content for p, content in before.items())
    decision.fingerprint = 'stale'
    with pytest.raises(HTTPException) as error:
        store.decide(writer.metadata['id'], decision, 'Nick')
    assert error.value.status_code == 409
    with pytest.raises(ValueError):
        writer.add({**record, 'split': 'test'}, [], {})


def test_auth_and_path_guards(review):
    store, writer, record = review
    app = FastAPI()
    def session(request: Request):
        if request.headers.get('x-paired') != 'yes':
            raise HTTPException(401)
        return {'reviewer': 'Nick'}
    app.include_router(create_lane_error_router(store.root, session))
    with TestClient(app) as client:
        base = '/api/studio/model-errors'
        assert client.get(base).status_code == 401
        response = client.get(base, headers={'x-paired': 'yes'})
        assert response.status_code == 200 and len(response.json()['collections']) == 1
        assert client.get(base + '/run-test-initial/cases/photo-1/photo').status_code == 401
        assert client.get(base + '/run-test-initial/cases/photo-1/photo', headers={'x-paired': 'yes'}).status_code == 200
    with pytest.raises(HTTPException):
        store.case('..', 'photo-1')
    payload = store.case(writer.metadata['id'], record['image_id'])
    payload['image'] = '../../../../outside.jpg'
    (writer.path / 'cases/photo-1.json').write_text(json.dumps(payload))
    with pytest.raises(HTTPException):
        store.photo(writer.metadata['id'], record['image_id'])


def test_accept_dismisses_alert_and_undo_reopens_without_changing_scores(review):
    store, writer, record = review
    collection = writer.metadata['id']
    before = (writer.path / 'metadata.json').read_bytes()
    item = store.case(collection, record['image_id'])
    body = ErrorDecision(image_id=record['image_id'], policy='stock', fingerprint=item['fingerprint'], action='acceptable')
    assert store.summary(collection, 'stock')['open_alerts'] == 1
    result = store.decide(collection, body, 'Nick')
    assert result['summary']['accepted_predictions'] == 1
    assert result['summary']['open_alerts'] == result['summary']['unreviewed'] == 0
    assert store.queue(collection, 'stock', 'all', 'open', '', 1)['total'] == 0
    assert store.queue(collection, 'stock', 'all', 'accepted', '', 1)['total'] == 1
    # Replacing an existing verdict must not double-count it.
    body.action = 'confirmed_model_error'
    result = store.decide(collection, body, 'Nick')
    assert result['summary']['accepted_predictions'] == 0
    assert result['summary']['confirmed_model_errors'] == result['summary']['open_alerts'] == 1
    assert store.queue(collection, 'stock', 'all', 'follow_up', '', 1)['total'] == 1
    undone = store.undo(collection, record['image_id'], 'stock', item['fingerprint'])
    assert undone['summary']['unreviewed'] == undone['summary']['open_alerts'] == 1
    assert undone['summary']['confirmed_model_errors'] == 0
    assert store.queue(collection, 'stock', 'all', 'pending', '', 1)['total'] == 1
    assert (writer.path / 'metadata.json').read_bytes() == before


def test_legacy_votes_and_fingerprint_changes_are_counted_correctly(review):
    store, writer, record = review
    collection = writer.metadata['id'];item = store.case(collection, record['image_id'])
    body = ErrorDecision(image_id=record['image_id'], policy='stock', fingerprint=item['fingerprint'], action='label_question')
    store.decide(collection, body, 'Nick')
    assert store.queue(collection, 'stock', 'all', 'label_question', '', 1)['total'] == 1
    body.action = 'skip';store.decide(collection, body, 'Nick')
    assert store.summary(collection, 'stock')['skipped'] == 1
    assert store.queue(collection, 'stock', 'all', 'skipped', '', 1)['total'] == 1
    rows = json.loads((writer.path / 'index.json').read_text());rows[0]['fingerprint'] = 'new-predictions'
    (writer.path / 'index.json').write_text(json.dumps(rows))
    summary = store.summary(collection, 'stock')
    assert summary['skipped'] == 0 and summary['unreviewed'] == 1
    with pytest.raises(HTTPException) as error:
        store.undo(collection, record['image_id'], 'stock', 'wrong-fingerprint')
    assert error.value.status_code == 409
