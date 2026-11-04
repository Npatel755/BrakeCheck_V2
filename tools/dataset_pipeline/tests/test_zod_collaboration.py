"""Two real sessions must never silently overwrite each other's reviews."""
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException
import pytest

from bcdata.zod_main_review import next_sample
from .test_zod_mirror import fixture, decision


def test_two_simultaneous_reviewers_get_different_samples(tmp_path):
    _, store = fixture(tmp_path, count=5)
    with ThreadPoolExecutor(2) as pool:
        items = list(pool.map(lambda owner: next_sample(store, owner=owner), ['alice', 'bob']))
    assert len({item['image_id'] for item in items}) == 2
    for item, owner in zip(items, ['alice', 'bob']):
        store.claim(item['image_id'], owner, revision=item['revision'], fingerprint=item['fingerprint'])
    with pytest.raises(HTTPException, match='Another reviewer'):
        store.claim(items[0]['image_id'], 'bob')


def test_only_owner_can_save_and_retry_is_idempotent(tmp_path):
    _, store = fixture(tmp_path, count=1)
    item = next_sample(store, owner='alice')
    body = decision(item)
    with pytest.raises(HTTPException) as error:
        store.decide(body, 'Bob', owner='bob')
    assert error.value.status_code == 409
    saved = store.decide(body, 'Alice', owner='alice')
    assert saved['revision'] == 1
    store.claim(item['image_id'], 'bob')
    assert store.decide(body, 'Alice', owner='alice')['revision'] == 1
    with pytest.raises(HTTPException) as error:
        store.decide(decision(item, event='bob-event-000001'), 'Bob', owner='bob')
    assert error.value.status_code == 409
    db = store._db()
    assert db.execute('SELECT COUNT(*) FROM audit_events').fetchone()[0] == 1
    db.close()


def test_expired_claim_renewal_and_old_draft_cannot_overwrite(tmp_path):
    _, store = fixture(tmp_path, count=1)
    item = next_sample(store, owner='alice')
    db = store._db()
    db.execute('UPDATE zod_review_claims SET expires=0')
    db.commit()
    db.close()
    store.claim(item['image_id'], 'bob', revision=item['revision'], fingerprint=item['fingerprint'])
    store.decide(decision(item), 'Bob', owner='bob')
    with pytest.raises(HTTPException) as error:
        store.claim(item['image_id'], 'alice', revision=item['revision'], fingerprint=item['fingerprint'])
    assert error.value.status_code == 409
    assert store.detail(item['image_id'])['decision']['reviewer'] == 'Bob'


def test_releasing_claim_cannot_release_another_reviewers_photo(tmp_path):
    _, store = fixture(tmp_path, count=1)
    item = next_sample(store, owner='alice')
    store.release(item['image_id'], 'bob')
    assert next_sample(store, owner='bob') is None
    store.release(item['image_id'], 'alice')
    assert next_sample(store, owner='bob')['image_id'] == item['image_id']


def test_regular_queue_renews_long_edit_and_rejects_stale_revision(tmp_path):
    from .test_review import build_review_fixture
    from bcdata.review_store import ReviewStore, ReviewError
    import uuid
    store = ReviewStore(build_review_fixture(tmp_path))
    item = store.next_image('alice')
    store.renew_claim(item['image_id'], item['revision'], 'alice')
    with pytest.raises(ReviewError):
        store.renew_claim(item['image_id'], item['revision'], 'bob')
    store.renew_claim(item['image_id'], item['revision'], 'alice', release=True)
    store.renew_claim(item['image_id'], item['revision'], 'bob')
    store.decide(item['image_id'], 'approved', 'Bob', item['revision'], str(uuid.uuid4()), owner='bob')
    with pytest.raises(ReviewError):
        store.renew_claim(item['image_id'], item['revision'], 'alice')


def test_http_sessions_reserve_separately_and_cannot_overwrite(tmp_path):
    from fastapi.testclient import TestClient
    from .test_review import build_review_fixture
    from bcdata.review_server import create_app
    settings = build_review_fixture(tmp_path)
    fixture(tmp_path, count=5)
    app = create_app(settings, start_worker=False)
    alice = {'X-Review-Client': 'alice-browser-0000001'}
    bob = {'X-Review-Client': 'bob-browser-000000001'}
    with TestClient(app, base_url='http://localhost', client=('127.0.0.1', 12345)) as client:
        first = client.get('/api/next?status=zod_audit', headers=alice).json()
        second = client.get('/api/next?status=zod_audit', headers=bob).json()
        assert first['image_id'] != second['image_id']
        url = '/api/images/' + first['image_id']
        # Background metadata reads never acquire or move a reservation.
        assert client.get(url, headers=bob).status_code == 200
        renewal = {'revision': first['revision'], 'fingerprint': first['fingerprint']}
        assert client.post(url + '/claim', json={**renewal, 'activate': True}, headers=bob).status_code == 409
        assert client.post(url + '/claim', json=renewal, headers=alice).status_code == 200
        body = {**renewal, 'image_id': first['image_id'], 'action': 'approved', 'reviewer': 'Alice', 'event_id': 'http-alice-event001'}
        assert client.post('/api/reviews', json=body, headers=bob).status_code == 409
        assert client.post('/api/reviews', json=body, headers=alice).status_code == 200
        assert client.post('/api/reviews', json=body, headers=alice).status_code == 200
        body.update(event_id='http-bob-event00001', reviewer='Bob')
        assert client.post('/api/reviews', json=body, headers=bob).status_code == 409


def test_main_warning_approval_retry_does_not_become_a_different_request(tmp_path):
    from bcdata.zod_main_review import decide, detail
    from bcdata.review_server import DecisionRequest
    _, store = fixture(tmp_path, count=1, warnings=True)
    item = detail(store, 'zod-000000')
    body = DecisionRequest(image_id=item['image_id'], revision=item['revision'], fingerprint=item['fingerprint'],
                           action='approved', event_id='warning-approve-retry', complete_annotation=True)
    first = decide(store, body, 'Alice')
    second = decide(store, body, 'Alice')
    assert first['revision'] == second['revision'] == 1


def test_background_reads_and_late_heartbeat_do_not_move_active_reservation(tmp_path):
    from fastapi.testclient import TestClient
    from .test_review import build_review_fixture
    from bcdata.review_server import create_app
    settings = build_review_fixture(tmp_path)
    _, store = fixture(tmp_path, count=8)
    app = create_app(settings, start_worker=False)
    alice = {'X-Review-Client': 'alice-browser-0000001'}
    with TestClient(app, base_url='http://localhost', client=('127.0.0.1', 12345)) as client:
        first = client.get('/api/next?status=zod_audit', headers=alice).json()
        second = client.get('/api/next?status=zod_audit&exclude=' + first['image_id'], headers=alice).json()
        assert first['image_id'] != second['image_id']
        assert client.get('/api/images/' + first['image_id'], headers=alice).status_code == 200
        late = client.post('/api/images/' + first['image_id'] + '/claim', headers=alice,
                           json={'revision': first['revision'], 'fingerprint': first['fingerprint']})
        assert late.status_code == 409
        db = store._db()
        rows = [row[0] for row in db.execute('SELECT image_id FROM zod_review_claims')]
        db.close()
        assert rows == [second['image_id']]
