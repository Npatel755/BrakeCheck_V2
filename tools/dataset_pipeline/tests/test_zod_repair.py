import json
from pathlib import Path

from fastapi import HTTPException
import pytest

from bcdata.zod_mirror import convert, convert_legacy
from bcdata.zod_main_review import decide, detail
from bcdata.review_server import DecisionRequest
from .test_zod_mirror import fixture


def native(image_id):
    return json.loads((Path(__file__).parent / 'fixtures/zod' / (image_id + '.json')).read_text())


def test_reported_tram_scene_keeps_long_boundaries_instead_of_tiny_far_fragments():
    source = native('zod-000001')
    old, _ = convert_legacy(source, 'zod-000001')
    new, _ = convert(source, 'zod-000001')
    assert max(p[1] for p in old['lanes'][1]['points']) - min(p[1] for p in old['lanes'][1]['points']) < 1
    assert [lane['slot'] for lane in new['lanes']] == [2, 3]
    assert all(max(p[1] for p in lane['points']) > 530 for lane in new['lanes'])
    assert all(len({p[1] for p in lane['points']}) >= 36 for lane in new['lanes'])
    assert any(len(lane['segments']) > 1 for lane in new['lanes'])


def test_reported_intersection_does_not_turn_sparse_specks_into_four_boundaries():
    source = native('zod-000002')
    old, _ = convert_legacy(source, 'zod-000002')
    new, warnings = convert(source, 'zod-000002')
    assert len(old['lanes']) == 4
    assert new['lanes'] == []
    assert 'no_converted_lane_requires_whole_photo_check' in warnings
    assert 'sparse_native_paint_needs_review' in warnings


def test_main_adapter_requires_explicit_complete_check_and_preserves_idempotent_edits(tmp_path):
    _, store = fixture(tmp_path, count=1, warnings=True)
    item = detail(store, 'zod-000000')
    body = DecisionRequest(image_id=item['image_id'], revision=item['revision'], action='corrected',
                           event_id='main-correction-1', fingerprint=item['fingerprint'],
                           lanes=[{'slot': 2, 'points': [[400, 500], [350, 650]]}])
    with pytest.raises(HTTPException):
        decide(store, body, 'Nick')
    body.complete_annotation = True
    result = decide(store, body, 'Nick')
    assert result['training_eligible'] is False
    assert result['group_state'] == 'needs_repair'
    assert decide(store, body, 'Nick')['revision'] == result['revision']


def test_main_adapter_never_accepts_a_stale_or_missing_model_receipt(tmp_path):
    _, store = fixture(tmp_path, count=1)
    item = detail(store, 'zod-000000')
    body = DecisionRequest(image_id=item['image_id'], revision=item['revision'], action='corrected',
                           event_id='main-correction-2', fingerprint=item['fingerprint'],
                           complete_annotation=True, candidate='invented_model', candidate_version='stale',
                           lanes=[{'slot': 2, 'points': [[400, 500], [350, 650]]}])
    with pytest.raises(HTTPException) as error:
        decide(store, body, 'Nick')
    assert error.value.status_code == 409


def test_nonempty_model_labels_cannot_be_saved_as_no_visible_paint(tmp_path):
    _, store = fixture(tmp_path, count=1)
    item = detail(store, 'zod-000000')
    body = DecisionRequest(image_id=item['image_id'], revision=item['revision'], action='corrected',
                           event_id='main-correction-3', fingerprint=item['fingerprint'],
                           complete_annotation=True, no_visible_lane_paint=True,
                           lanes=[{'slot': 2, 'points': [[400, 500], [350, 650]]}])
    with pytest.raises(HTTPException) as error:
        decide(store, body, 'Nick')
    assert error.value.status_code == 422


def test_main_no_visible_paint_choice_records_empty_labels_and_retries_safely(tmp_path):
    _, store = fixture(tmp_path, count=1, warnings=True)
    item = detail(store, 'zod-000000')
    body = DecisionRequest(image_id=item['image_id'], revision=item['revision'], action='corrected',
                           event_id='main-empty-draft-1', fingerprint=item['fingerprint'],
                           complete_annotation=True, no_visible_lane_paint=True, lanes=[])
    saved = decide(store, body, 'Nick')
    assert decide(store, body, 'Nick')['revision'] == saved['revision']
    item = detail(store, 'zod-000000')
    assert item['geometry']['lanes'] == []
    assert item['geometry']['no_visible_lane_paint'] is True
    assert item['geometry']['human_complete_annotation'] is True
    assert item['decision']['action'] == 'approve'


def test_rebuild_keeps_original_human_history_and_requires_fresh_audits(tmp_path):
    from bcdata.zod_repair_batch import REPAIR, run
    from bcdata.zod_mirror import connect
    from .test_zod_mirror import decision
    root, store = fixture(tmp_path, count=1)
    store.decide(decision(store.detail('zod-000000')), 'Nick')
    db = connect(root)
    before = dict(db.execute('SELECT * FROM frames').fetchone())
    events = [tuple(row) for row in db.execute('SELECT * FROM audit_events')]
    db.close()
    result = run(tmp_path, workers=1)
    assert result['state'] == 'audit_ready'
    db = connect(root)
    assert dict(db.execute('SELECT * FROM frames').fetchone()) == before
    assert [tuple(row) for row in db.execute('SELECT * FROM audit_events')] == events
    db.close()
    db = connect(root / REPAIR)
    assert db.execute('SELECT action FROM frames').fetchone()[0] is None
    assert db.execute('SELECT COUNT(*) FROM audit_events').fetchone()[0] == 0
    db.close()


def test_main_queue_routes_and_decisions_use_isolated_zod_store(tmp_path):
    from fastapi.testclient import TestClient
    from bcdata.local import local_settings
    from bcdata.review_server import create_app
    _, store = fixture(tmp_path)
    app = create_app(local_settings(tmp_path), start_worker=False)
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50100)) as client:
        response = client.get('/api/next?status=zod_audit')
        assert response.status_code == 200
        item = response.json()
        assert item['source'] == 'zod_mirror' and item['zod_audit']
        assert client.get('/api/images/' + item['image_id']).json()['fingerprint'] == item['fingerprint']
        response = client.post('/api/reviews', json={'image_id': item['image_id'], 'action': 'rejected',
                               'revision': item['revision'], 'fingerprint': item['fingerprint'],
                               'event_id': 'main-router-event1', 'notes': 'wrong slots'})
        assert response.status_code == 200
        assert response.json()['group_state'] == 'needs_repair'
        assert client.get('/api/next?status=zod_audit').json() is None
        assert client.get('/api/next?status=zod_repair').json()['group_state'] == 'needs_repair'
    assert store.status()['counts']['photos_in_repair_groups'] == 5
