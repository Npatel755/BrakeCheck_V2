"""Crash recovery tests use offline providers and the real API."""
import time
import threading
import requests

import pytest
from fastapi.testclient import TestClient
from test_integration import HEADERS, isolated, client, start
import crash
import contacts
import main


def test_pending_checkin_and_deadline_survive_backend_restart(client, monkeypatch):
    monkeypatch.setattr(crash, '_send_photon_alert', lambda c: {'sent': 1, 'failed': []})
    cid = start(client)
    deadline = client.get(f'/crash/{cid}').json()['deadline']
    crash.CRASHES.clear()  # Simulate a new worker loading the durable state.
    assert crash.recover() == []
    assert client.get(f'/crash/{cid}').json()['deadline'] == deadline
    crash.CRASHES[cid]['_timer'].callback()
    assert client.get(f'/crash/{cid}').json()['alerted'] is True


def test_backend_startup_restores_saved_checkin(client):
    cid = start(client)
    crash.CRASHES.clear()
    with TestClient(main.app, headers=HEADERS) as restarted:
        assert restarted.get(f'/crash/{cid}').json()['status'] == 'pending'


def test_old_timer_cannot_resolve_rearmed_checkin(client, monkeypatch):
    calls = []
    monkeypatch.setattr(crash, '_send_photon_alert', lambda c: (calls.append(c['id']) or {'sent': 1, 'failed': []}))
    cid = start(client)
    first_timer = crash.CRASHES[cid]['_timer']
    assert client.post(f'/crash/{cid}/arm').status_code == 200
    first_timer.callback()
    assert client.get(f'/crash/{cid}').json()['status'] == 'pending'
    crash.CRASHES[cid]['_timer'].callback()
    assert client.get(f'/crash/{cid}').json()['status'] == 'no_response'
    assert calls == [cid]


def test_unfinished_agent_request_resumes_with_same_event_id(client, monkeypatch):
    cid = start(client)
    with crash.LOCK:
        c = crash.CRASHES[cid]
        c.update(status='help', transcript='Selected on screen', sent=0, failed=[],
                 failed_count=0, alert_error=None, delivery_pending=True)
        crash._save_locked()
    calls = []
    monkeypatch.setattr(crash, '_send_photon_alert',
                        lambda c: (calls.append(c['id']) or {'sent': 1, 'failed': []}))
    crash.CRASHES.clear()
    workers = crash.recover()
    assert len(workers) == 1
    workers[0].join(timeout=2)
    assert not workers[0].is_alive()
    result = client.get(f'/crash/{cid}').json()
    assert result['sent'] == 1 and not result['delivery_pending'] and calls == [cid]
    crash.CRASHES.clear()
    assert crash.recover() == []
    assert client.get(f'/crash/{cid}').json()['sent'] == 1


def test_temporary_agent_outage_retries_frozen_recipients(client, monkeypatch):
    recipients = ['+14045550101']
    monkeypatch.setattr(contacts, 'confirmed_contacts', lambda: list(recipients))
    first_attempt = threading.Event()
    calls = []

    def agent(path, payload):
        calls.append(dict(payload))
        if len(calls) == 1:
            first_attempt.set()
            raise requests.ConnectionError('Agent starting')
        return {'sent': 1, 'failed': []}

    monkeypatch.setattr(crash, '_agent', agent)
    cid = start(client)
    response = client.post(f'/crash/{cid}/respond', json={'status': 'help'}).json()
    assert response['delivery_pending']
    assert first_attempt.wait(1)
    recipients[:] = ['+14045550102']
    crash.CRASHES[cid]['_delivery_worker'].join(timeout=3)
    outcome = client.get(f'/crash/{cid}').json()
    assert outcome['alerted'] and outcome['sent'] == 1 and not outcome['delivery_pending']
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0]['contacts'] == ['+14045550101']


def test_completed_sessions_are_evicted_before_store_reaches_capacity(client, monkeypatch):
    monkeypatch.setattr(crash, 'MAX_SESSIONS', 2)
    oldest = start(client, dry_run=True)
    assert client.post(f'/crash/{oldest}/respond', json={'status': 'ok'}).status_code == 200
    middle = start(client, dry_run=True)
    newest = start(client, dry_run=True)
    assert client.get(f'/crash/{oldest}').status_code == 404
    crash.CRASHES.clear()
    assert crash.recover() == []
    assert client.get(f'/crash/{middle}').status_code == 200
    assert client.get(f'/crash/{newest}').status_code == 200


def test_stale_pending_checkin_is_not_sent_as_late_emergency(client, monkeypatch):
    cid = start(client)
    with crash.LOCK:
        crash.CRASHES[cid]['time'] = time.time() - crash.MAX_RECOVERY_AGE - 1
        crash._save_locked()
    calls = []
    monkeypatch.setattr(crash, '_send_photon_alert', lambda c: calls.append(c['id']))
    crash.CRASHES.clear()
    assert crash.recover() == []
    result = client.get(f'/crash/{cid}').json()
    assert result['status'] == 'no_response' and not result['alerted']
    assert 'offline' in result['alert_error'] and calls == []


def test_stale_unfinished_delivery_reports_unknown_without_resending(client, monkeypatch):
    cid = start(client)
    with crash.LOCK:
        c = crash.CRASHES[cid]
        c.update(time=time.time() - crash.MAX_RECOVERY_AGE - 1, status='help',
                 transcript='Selected on screen', sent=0, failed=[], failed_count=0,
                 alert_error=None, delivery_pending=True)
        crash._save_locked()
    calls = []
    monkeypatch.setattr(crash, '_send_photon_alert', lambda c: calls.append(c['id']))
    crash.CRASHES.clear()
    assert crash.recover() == []
    result = client.get(f'/crash/{cid}').json()
    assert not result['delivery_pending'] and not result['alerted']
    assert 'unknown' in result['alert_error'] and calls == []


def test_completed_ok_checkin_remains_ok_without_agent_retry(client, monkeypatch):
    cid = start(client)
    assert client.post(f'/crash/{cid}/respond', json={'status': 'ok'}).json()['status'] == 'ok'
    monkeypatch.setattr(crash, '_send_photon_alert', lambda c: pytest.fail('OK check-in must not alert'))
    crash.CRASHES.clear()
    assert crash.recover() == []
    assert client.get(f'/crash/{cid}').json()['status'] == 'ok'
    assert client.post(f'/crash/{cid}/timeout').json()['status'] == 'ok'


def test_restart_discards_resolved_checkins_older_than_a_day(client):
    cid = start(client)
    assert client.post(f'/crash/{cid}/respond', json={'status': 'ok'}).status_code == 200
    with crash.LOCK:
        crash.CRASHES[cid]['time'] = time.time() - 86401
        crash._save_locked()
    crash.CRASHES.clear()
    assert crash.recover() == []
    assert client.get(f'/crash/{cid}').status_code == 404


def test_corrupt_crash_state_fails_startup_closed(client):
    crash.STORE.write_text('not JSON', encoding='utf-8')
    with pytest.raises(RuntimeError, match='could not be restored'):
        crash.recover()
