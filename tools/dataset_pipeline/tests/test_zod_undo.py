import json

from fastapi import HTTPException
import pytest

from bcdata.simulation_review import SimulationCorrection
from bcdata.zod_audit_groups import record_finding
from .test_zod_mirror import fixture, decision


@pytest.mark.parametrize('action', ['approve', 'exclude', 'skip'])
def test_undo_restores_review_state_and_is_idempotent(tmp_path, action):
    _, store = fixture(tmp_path, count=1)
    before = store.detail('zod-000000')
    store.decide(decision(before, action=action), 'Alice')
    first = store.undo('event-00000001', 'Alice')
    assert store.undo('event-00000001', 'Alice') == first
    after = store.detail('zod-000000')
    assert after['decision'] is None and after['geometry'] == before['geometry']
    assert after['revision'] == 2 and after['training_eligible'] is False
    db = store._db()
    assert db.execute('SELECT COUNT(*) FROM audit_events').fetchone()[0] == 2
    assert db.execute('SELECT repair_required FROM zod_audit_members').fetchone()[0] == 0
    db.close()


def test_undo_correction_restores_previous_human_labels_and_group_hold(tmp_path):
    _, store = fixture(tmp_path, count=1)
    item = store.detail('zod-000000')
    store.correct(SimulationCorrection(image_id=item['image_id'], revision=item['revision'], fingerprint=item['fingerprint'],
                  lanes=[{'slot': 2, 'points': [[310, 450], [260, 500]]}], resolved_qc_reasons=[], approve=True), 'Alice')
    before = store.detail(item['image_id'])
    store.correct(SimulationCorrection(image_id=item['image_id'], revision=before['revision'], fingerprint=before['fingerprint'],
                  lanes=[{'slot': 2, 'points': [[410, 450], [360, 500]]}], resolved_qc_reasons=[], approve=True), 'Alice')
    db = store._db()
    event_id = db.execute('SELECT event_id FROM audit_events ORDER BY rowid DESC LIMIT 1').fetchone()[0]
    db.close()
    store.undo(event_id, 'Alice')
    after = store.detail(item['image_id'])
    assert after['geometry'] == before['geometry']
    assert after['decision'] == before['decision']
    assert after['group_state'] == 'needs_repair'


def test_undo_rejects_another_reviewer_newer_edits_and_active_claim(tmp_path):
    _, store = fixture(tmp_path, count=1)
    store.decide(decision(store.detail('zod-000000')), 'Alice')
    with pytest.raises(HTTPException) as error:
        store.undo('event-00000001', 'Bob')
    assert error.value.status_code == 403
    store.claim('zod-000000', 'bob-session')
    with pytest.raises(HTTPException) as error:
        store.undo('event-00000001', 'Alice', owner='alice-session')
    assert error.value.status_code == 409
    store.decide(decision(store.detail('zod-000000'), action='skip', event='bob-event-000001'), 'Bob', owner='bob-session')
    with pytest.raises(HTTPException) as error:
        store.undo('event-00000001', 'Alice')
    assert error.value.status_code == 409
    assert store.detail('zod-000000')['decision']['reviewer'] == 'Bob'


def test_undo_keeps_independent_converter_finding(tmp_path):
    _, store = fixture(tmp_path, count=1)
    store.decide(decision(store.detail('zod-000000'), action='exclude'), 'Alice')
    db = store._db()
    record_finding(db, 'zod-000000', 'duplicate boundary diagnosed', 'converter')
    db.close()
    store.undo('event-00000001', 'Alice')
    assert store.detail('zod-000000')['group_state'] == 'needs_repair'


def test_manual_identical_outer_and_main_labels_cannot_be_approved(tmp_path):
    _, store = fixture(tmp_path, count=1)
    item = store.detail('zod-000000')
    with pytest.raises(HTTPException) as error:
        store.correct(SimulationCorrection(image_id=item['image_id'], revision=item['revision'], fingerprint=item['fingerprint'],
                      lanes=[{'slot': slot, 'points': [[310, 450], [260, 500]]} for slot in (1, 2)],
                      resolved_qc_reasons=[], approve=True), 'Alice')
    assert error.value.status_code == 422
    assert store.detail(item['image_id'])['revision'] == 0
