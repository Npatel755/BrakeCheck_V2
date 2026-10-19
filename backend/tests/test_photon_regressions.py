"""Photon regressions exercised through the real API with offline providers."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
import requests
from test_integration import isolated, client, start, AGENT_HEADERS
import contacts
import crash

PHONE = '+14045550101'


def test_reused_checkin_id_cannot_change_the_event_payload(client):
    event_id = str(uuid.uuid4())
    payload = {'client_event_id': event_id, 'lat': 33.75, 'lon': -84.38, 'dry_run': True}
    first = client.post('/crash/start', json=payload)
    assert first.status_code == 200
    assert client.post('/crash/start', json=payload).json() == first.json()
    for change in [{'lat': 34.0}, {'dry_run': False}, {'lang': 'es'}, {'speed_mph': 40}]:
        assert client.post('/crash/start', json={**payload, **change}).status_code == 409
    assert client.get(f'/crash/{event_id}').json()['status'] == 'pending'
    assert client.post(f'/crash/{event_id}/respond', json={'status': 'ok'}).json()['dry_run']


def add_confirmed(client, monkeypatch):
    monkeypatch.setattr(contacts, '_photon', lambda *a, **kw: {'id': 'test-user'})
    assert client.post('/contacts', json={'phone': PHONE, 'first_name': 'Alex'}).status_code == 200
    assert client.post('/contacts/confirm', json={'phone': PHONE}, headers=AGENT_HEADERS).json()['confirmed']


def test_failed_removal_cannot_be_reconfirmed_or_tested(client, monkeypatch):
    add_confirmed(client, monkeypatch)
    def unavailable(*a, **kw):
        raise contacts.HTTPException(502, 'Provider unavailable.')
    monkeypatch.setattr(contacts, '_photon', unavailable)
    assert client.delete(f'/contacts/{PHONE}').status_code == 502
    saved = client.get('/contacts').json()[0]
    assert saved['removal_pending'] and not saved['confirmed']
    assert client.post('/contacts/confirm', json={'phone': PHONE}, headers=AGENT_HEADERS).json()['known'] is False
    assert client.get('/contacts/authorized', params={'phone': PHONE}, headers=AGENT_HEADERS).json()['authorized'] is False
    assert client.post(f'/contacts/{PHONE}/test').status_code == 409
    assert client.post('/contacts', json={'phone': PHONE, 'first_name': 'Alex'}).status_code == 409
    monkeypatch.setattr(contacts, '_photon', lambda *a, **kw: {})
    assert client.delete(f'/contacts/{PHONE}').status_code == 200
    assert client.get('/contacts').json() == []


def test_slow_remote_removal_does_not_block_local_authorization(client, monkeypatch):
    add_confirmed(client, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def slow(*a, **kw):
        entered.set(); assert release.wait(3); return {}
    monkeypatch.setattr(contacts, '_photon', slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        deleting = pool.submit(client.delete, f'/contacts/{PHONE}')
        try:
            assert entered.wait(1)
            reading = pool.submit(client.get, '/contacts/authorized', params={'phone': PHONE}, headers=AGENT_HEADERS)
            assert reading.result(timeout=1).json()['authorized'] is False
        finally:
            release.set()
        assert deleting.result(timeout=1).status_code == 200


def test_slow_creation_keeps_existing_contacts_available_and_deduplicates(client, monkeypatch):
    add_confirmed(client, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def slow(*a, **kw):
        calls.append(a); entered.set(); assert release.wait(3)
        return {'id': 'second-user'}
    monkeypatch.setattr(contacts, '_photon', slow)
    payload = {'phone': '+14045550102', 'first_name': 'Second'}
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(client.post, '/contacts', json=payload)
        try:
            assert entered.wait(1)
            duplicate = pool.submit(client.post, '/contacts', json=payload)
            reading = pool.submit(client.get, '/contacts/authorized', params={'phone': PHONE}, headers=AGENT_HEADERS)
            assert reading.result(timeout=1).json()['authorized'] is True
        finally:
            release.set()
        assert first.result(timeout=1).status_code == duplicate.result(timeout=1).status_code == 200
    assert len(calls) == 1 and len(client.get('/contacts').json()) == 2


def test_polling_exposes_pending_delivery_without_holding_global_lock(client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def slow(c):
        calls.append(c['id']); entered.set(); assert release.wait(3)
        return {'sent': 1, 'failed': [{'to': '…02', 'error': 'Not accepted.'}]}
    monkeypatch.setattr(crash, '_send_photon_alert', slow)
    cid = start(client)
    with ThreadPoolExecutor(max_workers=3) as pool:
        deciding = pool.submit(client.post, f'/crash/{cid}/respond', json={'status': 'help'})
        try:
            assert entered.wait(1)
            polling = pool.submit(client.get, f'/crash/{cid}')
            state = polling.result(timeout=1).json()
            assert state['status'] == 'help' and state['delivery_pending']
            assert state['alerted'] is False
            assert client.get(f'/crash/{cid}/ack.mp3').status_code == 409
            duplicate = client.post(f'/crash/{cid}/respond', json={'status': 'help'}).json()
            assert duplicate['delivery_pending'] and calls == [cid]
            other = start(client, dry_run=True)
            assert client.post(f'/crash/{other}/respond', json={'status': 'ok'}).json()['status'] == 'ok'
        finally:
            release.set()
        outcome = deciding.result(timeout=1).json()
    assert outcome['delivery_pending']
    crash.CRASHES[cid]['_delivery_worker'].join(timeout=2)
    outcome = client.get(f'/crash/{cid}').json()
    assert outcome['sent'] == 1 and outcome['failed_count'] == 1 and not outcome['delivery_pending']
    assert client.get(f'/crash/{cid}').json()['alerted']
    assert client.get(f'/crash/{cid}/ack.mp3').status_code == 200


def test_lost_agent_response_retries_the_same_immutable_alert(client, monkeypatch):
    add_confirmed(client, monkeypatch)
    calls = []
    def agent(path, payload):
        calls.append((path, dict(payload)))
        if len(calls) == 1: raise requests.Timeout('Lost response')
        return {'sent': 1, 'failed': []}
    monkeypatch.setattr(crash, '_agent', agent)
    cid = start(client)
    result = client.post(f'/crash/{cid}/respond', json={'status': 'help'}).json()
    assert result['delivery_pending'] or result['alerted']
    crash.CRASHES[cid]['_delivery_worker'].join(timeout=3)
    result = client.get(f'/crash/{cid}').json()
    assert result['alerted'] and result['sent'] == 1
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0][1]['crashId'] == cid


@pytest.mark.parametrize('result', [None, [], {'sent': True, 'failed': []}, {'sent': 1, 'failed': 'invalid'},
    {'sent': 51, 'failed': []}, {'sent': 1, 'failed': ['bad']}, {'sent': 1, 'failed': [{'to': None, 'error': 'bad'}]}])
def test_malformed_agent_delivery_does_not_claim_acceptance(client, monkeypatch, result):
    monkeypatch.setattr(crash, '_send_photon_alert', lambda c: result)
    outcome = client.post(f'/crash/{start(client)}/respond', json={'status': 'help'})
    assert outcome.status_code == 200
    assert not outcome.json()['alerted'] and not outcome.json()['delivery_pending']
    assert outcome.json()['alert_error']


def test_malformed_test_reply_is_provider_error_instead_of_server_crash(client, monkeypatch):
    add_confirmed(client, monkeypatch)
    class Malformed:
        def raise_for_status(self): pass
        def json(self): return []
    monkeypatch.setattr(contacts.requests, 'post', lambda *a, **kw: Malformed())
    assert client.post(f'/contacts/{PHONE}/test').status_code == 502


@pytest.mark.parametrize('body', [[], {'data': ['invalid']}, {'data': {'id': {'bad': 'id'}}}])
def test_malformed_photon_user_response_is_reported_cleanly(client, monkeypatch, body):
    monkeypatch.setenv('SPECTRUM_PROJECT_ID', 'offline-project')
    monkeypatch.setenv('SPECTRUM_PROJECT_SECRET', 'offline-secret')
    class Malformed:
        status_code = 200
        content = b'json'
        def raise_for_status(self): pass
        def json(self): return body
    monkeypatch.setattr(contacts.requests, 'request', lambda *a, **kw: Malformed())
    assert client.post('/contacts', json={'phone': PHONE, 'first_name': 'Alex'}).status_code == 502
    assert client.get('/contacts').json() == []


def test_non_ascii_phone_digits_are_rejected_consistently(client):
    assert client.post('/contacts', json={'phone': '+1' + '٢' * 9, 'first_name': 'Alex'}).status_code == 422
