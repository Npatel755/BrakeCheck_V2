"""Offline integration tests. All vendor calls are replaced; no messages or credits."""
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import requests
from fastapi.testclient import TestClient
import config
import contacts
import crash
import main
import pairing
import pairing_store
import voice

APP_KEY = 'test-app-key-0000000000000000000000'
AGENT_KEY = 'test-agent-key-00000000000000000000'
HEADERS = {'X-BrakeCheck-Key': APP_KEY}
AGENT_HEADERS = {'X-Agent-Key': AGENT_KEY}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('BRAKECHECK_APP_KEY', APP_KEY)
    monkeypatch.setenv('AGENT_SHARED_KEY', AGENT_KEY)
    monkeypatch.setattr(contacts, 'STORE', tmp_path / 'contacts.json')
    monkeypatch.setattr(pairing_store, 'STORE', tmp_path / 'paired_devices.json')
    monkeypatch.setenv('BRAKECHECK_DISABLE_MDNS', '1')
    pairing.PAIRING_CODES._pending.clear()
    crash.CRASHES.clear()
    def network_forbidden(*args, **kwargs):
        raise AssertionError('Unexpected vendor network call during offline tests')
    monkeypatch.setattr(requests.Session, 'request', network_forbidden)
    monkeypatch.setattr(voice, 'say', lambda kind, lang='en': f'MP3:{kind}:{lang}'.encode())
    monkeypatch.setattr(voice, 'stt', lambda data, name='reply.m4a': "I'm okay")
    class FakeTimer:
        def __init__(self, seconds, callback): self.callback, self.seconds = callback, seconds
        def start(self): pass
        def cancel(self): pass
    monkeypatch.setattr(crash.threading, 'Timer', FakeTimer)
    yield
    crash.CRASHES.clear()


@pytest.fixture
def client():
    return TestClient(main.app, headers=HEADERS)


def start(client, **kwargs):
    result = client.post('/crash/start', json={'lat': 33.75, 'lon': -84.38, **kwargs})
    assert result.status_code == 200, result.text
    return result.json()['crash_id']


def mock_delivery(monkeypatch, sent=1, error=None):
    calls = []
    def send(c):
        calls.append(dict(c))
        if error: raise error
        return {'sent': sent, 'failed': []}
    monkeypatch.setattr(crash, '_send_photon_alert', send)
    return calls


def test_requires_app_auth():
    unauth = TestClient(main.app)
    for method, path, payload in [('GET', '/health', None), ('GET', '/contacts', None), ('POST', '/crash/start', {})]:
        assert unauth.request(method, path, json=payload).status_code == 401


def test_missing_app_key_fails_closed(client, monkeypatch):
    monkeypatch.delenv('BRAKECHECK_APP_KEY')
    assert client.get('/health').status_code == 503


def test_pairing_code_is_single_use_and_device_token_is_stored_as_hash(client):
    issued = client.post('/pairing/issue')
    assert issued.status_code == 200
    code = issued.json()['code']
    assert len(code) >= 32 and issued.json()['expires_in'] == 300

    unauthenticated = TestClient(main.app)
    exchanged = unauthenticated.post('/pairing/exchange', json={
        'code': code,
        'device_name': 'Test iPhone',
    })
    assert exchanged.status_code == 200
    token = exchanged.json()['device_token']
    assert len(token) >= 32

    stored = pairing_store.STORE.read_text(encoding='utf-8')
    assert token not in stored
    paired_client = TestClient(main.app, headers={'X-BrakeCheck-Device': token})
    assert paired_client.get('/health').status_code == 200
    assert unauthenticated.post('/pairing/exchange', json={
        'code': code,
        'device_name': 'Replay',
    }).status_code == 401

    device_id = exchanged.json()['device_id']
    assert client.delete(f'/pairing/devices/{device_id}').status_code == 200
    assert paired_client.get('/health').status_code == 401


def test_voice_and_prewarm(client):
    assert client.post('/voice/alert', json={'kind': 'brake', 'lang': 'es'}).content == b'MP3:brake:es'
    result = client.post('/voice/prewarm').json()
    assert 'checkin' in result['cached'] and 'failed_ack' in result['cached']
    assert client.post('/voice/alert', json={'kind': 'custom'}).status_code == 422


def test_voice_provider_failure_is_explicit(client, monkeypatch):
    monkeypatch.setattr(voice, 'say', lambda *args: (_ for _ in ()).throw(RuntimeError('vendor failed')))
    assert client.post('/voice/alert', json={}).status_code == 502


def test_no_gps_does_not_invent_coordinates(client):
    cid = start(client, lat=None, lon=None)
    state = client.get(f'/crash/{cid}').json()
    assert state['lat'] is None and state['lon'] is None
    assert '_timer' not in state


@pytest.mark.parametrize('payload', [{'lat': 91, 'lon': 0}, {'lat': 1}, {'lat': None, 'lon': 1}, {'speed_mph': -1}])
def test_invalid_telemetry_rejected(client, payload):
    assert client.post('/crash/start', json=payload).status_code == 422


def test_start_retry_and_arm_are_idempotent(client):
    event = str(uuid.uuid4())
    cid = start(client, client_event_id=event)
    assert start(client, client_event_id=event) == cid
    before = client.get(f'/crash/{cid}').json()['deadline']
    one = client.post(f'/crash/{cid}/arm').json()
    two = client.post(f'/crash/{cid}/arm').json()
    assert one['armed'] and one['deadline'] == two['deadline'] and before > one['deadline']


def test_spoken_ok_cancels_timeout(client, monkeypatch):
    calls = mock_delivery(monkeypatch)
    cid = start(client)
    result = client.post(f'/crash/{cid}/reply', files={'audio': ('reply.m4a', b'voice', 'audio/mp4')}).json()
    assert result['status'] == 'ok' and not result['alerted']
    assert client.post(f'/crash/{cid}/timeout').json()['status'] == 'ok'
    assert calls == []
    assert client.get(result['ack_audio_url']).content == b'MP3:ok_ack:en'


def test_manual_help_and_retry_send_once(client, monkeypatch):
    calls = mock_delivery(monkeypatch)
    cid = start(client)
    one = client.post(f'/crash/{cid}/respond', json={'status': 'help'}).json()
    two = client.post(f'/crash/{cid}/timeout').json()
    assert one['alerted'] and one['sent'] == 1 and two['status'] == 'help'
    assert len(calls) == 1


def test_timer_fires_if_phone_disconnects(client, monkeypatch):
    calls = mock_delivery(monkeypatch)
    cid = start(client)
    crash.CRASHES[cid]['_timer'].callback()
    assert client.get(f'/crash/{cid}').json()['alerted'] is True
    assert len(calls) == 1


def test_concurrent_resolutions_send_once(client, monkeypatch):
    calls = mock_delivery(monkeypatch)
    cid = start(client)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: crash._resolve(crash.CRASHES[cid], 'help'), range(6)))
    assert len(calls) == 1 and all(x['sent'] == 1 for x in results)


@pytest.mark.parametrize('status', ['help', 'no_response'])
def test_dry_run_never_calls_agent(client, monkeypatch, status):
    calls = mock_delivery(monkeypatch)
    cid = start(client, dry_run=True)
    result = crash._resolve(crash.CRASHES[cid], status)
    assert not result['alerted'] and result['dry_run'] and not calls
    assert client.get(result['ack_audio_url']).content == b'MP3:demo_ack:en'


def test_zero_sent_is_not_alerted(client, monkeypatch):
    mock_delivery(monkeypatch, sent=0)
    cid = start(client)
    result = client.post(f'/crash/{cid}/timeout').json()
    assert not result['alerted'] and result['alert_error']
    assert client.get(result['ack_audio_url']).content == b'MP3:failed_ack:en'


def test_agent_offline_reports_failure(client, monkeypatch):
    mock_delivery(monkeypatch, error=requests.ConnectionError())
    result = client.post(f'/crash/{start(client)}/timeout').json()
    assert not result['alerted'] and 'Photon' in result['alert_error']


def test_no_confirmed_contacts_does_not_contact_agent(client):
    result = client.post(f'/crash/{start(client)}/timeout').json()
    assert not result['alerted'] and result['sent'] == 0
    assert 'No confirmed' in result['alert_error']


def test_transcription_failure_keeps_checkin_open(client, monkeypatch):
    def unavailable(*args): raise RuntimeError('offline')
    monkeypatch.setattr(voice, 'stt', unavailable)
    cid = start(client)
    response = client.post(f'/crash/{cid}/reply', files={'audio': ('r.m4a', b'audio', 'audio/mp4')})
    assert response.status_code == 502
    assert client.get(f'/crash/{cid}').json()['status'] == 'pending'
    result = client.post(f'/crash/{cid}/respond', json={'status': 'ok'}).json()
    assert result['status'] == 'ok'


def test_reply_size_limit(client):
    cid = start(client)
    result = client.post(f'/crash/{cid}/reply', files={'audio': ('r.m4a', b'x' * (crash.MAX_AUDIO + 1))})
    assert result.status_code == 413


def test_pending_ack_and_unknown_session(client):
    assert client.get(f'/crash/{start(client)}/ack.mp3').status_code == 409
    assert client.post('/crash/unknown/timeout').status_code == 404


def test_contact_invite_confirm_optout_and_delete(client, monkeypatch):
    calls = []
    def photon(method, path, **kwargs):
        calls.append((method, path))
        return {'id': 'fake-user-id', 'assignedPhoneNumber': '+14045550100'}
    monkeypatch.setattr(contacts, '_photon', photon)
    phone = '+14045550101'
    payload = {'phone': phone, 'first_name': 'Test'}
    contact = client.post('/contacts', json=payload).json()
    assert not contact['confirmed'] and 'msg=YES' in contact['invite_text']
    client.post('/contacts', json=payload)
    assert len(calls) == 1  # Re-adding the same number does not consume a Photon slot.
    assert client.post('/contacts/confirm', json={'phone': phone}).status_code == 401
    confirm = client.post('/contacts/confirm', json={'phone': phone}, headers=AGENT_HEADERS)
    assert confirm.json()['confirmed']
    assert client.get('/contacts/authorized', params={'phone': phone}, headers=AGENT_HEADERS).json()['authorized']
    client.post('/contacts/confirm', json={'phone': phone, 'confirmed': False}, headers=AGENT_HEADERS)
    assert not contacts.confirmed_contacts()
    assert client.delete(f'/contacts/{phone}').status_code == 200
    assert client.get('/contacts').json() == []


def test_test_message_requires_confirmation_and_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(contacts, '_photon', lambda *args, **kwargs: {
        'id': 'fake-user-id', 'assignedPhoneNumber': '+14045550100'})
    phone = '+14045550101'
    client.post('/contacts', json={'phone': phone, 'first_name': 'Alex'})
    assert client.post(f'/contacts/{phone}/test').status_code == 409

    client.post('/contacts/confirm', json={'phone': phone}, headers=AGENT_HEADERS)
    calls = []

    class Accepted:
        def raise_for_status(self): pass
        def json(self): return {'accepted': True}

    def send(url, **kwargs):
        calls.append((url, kwargs))
        return Accepted()

    monkeypatch.setattr(contacts.requests, 'post', send)
    result = client.post(f'/contacts/{phone}/test')
    assert result.status_code == 200 and result.json() == {'accepted': True}
    assert calls[0][0].endswith('/test')
    assert calls[0][1]['json'] == {
        'phone': phone, 'first_name': 'Alex', 'driver_name': 'Harrison'}
    assert not any(key in calls[0][1]['json'] for key in ('lat', 'lon', 'location'))

    limited = client.post(f'/contacts/{phone}/test')
    assert limited.status_code == 429 and len(calls) == 1


def test_test_message_provider_failure_still_limits_retry(client, monkeypatch):
    monkeypatch.setattr(contacts, '_photon', lambda *args, **kwargs: {'id': 'fake-user-id'})
    phone = '+14045550102'
    client.post('/contacts', json={'phone': phone, 'first_name': 'Alex'})
    client.post('/contacts/confirm', json={'phone': phone}, headers=AGENT_HEADERS)
    attempts = []
    def fail(contact):
        attempts.append(contact)
        raise contacts.HTTPException(502, 'Provider unavailable.')
    monkeypatch.setattr(contacts, '_send_test_message', fail)
    assert client.post(f'/contacts/{phone}/test').status_code == 502
    monkeypatch.setattr(contacts, '_send_test_message', lambda contact: {'accepted': True})
    assert client.post(f'/contacts/{phone}/test').status_code == 429
    assert len(attempts) == 1


def test_agent_credentials_fail_closed(client, monkeypatch):
    monkeypatch.delenv('AGENT_SHARED_KEY')
    assert client.post('/contacts/confirm', json={'phone': '+14045550101'}, headers=AGENT_HEADERS).status_code == 401


def test_invalid_phone_rejected(client):
    assert client.post('/contacts', json={'phone': '+1', 'first_name': 'Test'}).status_code == 422


def test_trip_forwarding(client, monkeypatch):
    calls = []
    monkeypatch.setattr(crash, '_agent', lambda path, data: calls.append((path, data)) or {'stored': True})
    r = client.post('/trip', json={'minutes': 4, 'hardBrakes': 2, 'closeCalls': 1, 'maxSpeedMph': 25, 'time': time.time()})
    assert r.json()['stored'] and calls[0][0] == '/trip'


@pytest.mark.parametrize(('text', 'status'), [("I'm okay", 'ok'), ("I'm not okay", 'help'), ('Help me', 'help'),
                                            ('Estoy bien', 'ok'), ('ayuda', 'help'), ('', 'no_response')])
def test_reply_classification(text, status):
    assert voice.classify_reply(text) == status
