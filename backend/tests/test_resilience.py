"""Offline regressions for deadlines, malformed providers and authentication."""
import pytest
from fastapi import HTTPException

from test_integration import isolated, client, start, mock_delivery
import config
import crash
import voice
import pairing_store
import pairing

REAL_STT = voice.stt


def test_replaced_timer_cannot_expire_rearmed_checkin(client, monkeypatch):
    calls = mock_delivery(monkeypatch)
    cid = start(client)
    initial_timer = crash.CRASHES[cid]['_timer']
    armed = client.post(f'/crash/{cid}/arm').json()
    current_timer = crash.CRASHES[cid]['_timer']
    assert current_timer is not initial_timer

    # Simulate the old callback already running when Timer.cancel() was called.
    initial_timer.callback()
    state = client.get(f'/crash/{cid}').json()
    assert state['status'] == 'pending' and state['deadline'] == armed['deadline']
    assert calls == []

    current_timer.callback()
    assert client.get(f'/crash/{cid}').json()['status'] == 'no_response'
    assert len(calls) == 1


@pytest.mark.parametrize('authenticate', [
    lambda: config.require_admin('invalid-é'),
    lambda: config.require_client('invalid-é', None),
    lambda: config.require_agent('invalid-é'),
])
def test_non_ascii_credentials_are_rejected_cleanly(authenticate):
    with pytest.raises(HTTPException) as result:
        authenticate()
    assert result.value.status_code == 401


@pytest.mark.parametrize('payload', [None, [], 'text', {}, {'text': None}, {'text': 5}, {'text': []}])
def test_malformed_transcription_keeps_manual_response_available(client, monkeypatch, payload):
    class Response:
        status_code = 200
        def json(self):
            return payload

    monkeypatch.setattr(voice, 'stt', REAL_STT)
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'offline-test')
    monkeypatch.setattr(voice.requests, 'post', lambda *args, **kwargs: Response())
    cid = start(client, dry_run=True)
    response = client.post(f'/crash/{cid}/reply', files={'audio': ('reply.m4a', b'audio', 'audio/mp4')})
    assert response.status_code == 502
    assert client.get(f'/crash/{cid}').json()['status'] == 'pending'
    assert client.post(f'/crash/{cid}/respond', json={'status': 'ok'}).json()['status'] == 'ok'


def test_non_json_transcription_is_a_provider_error(client, monkeypatch):
    class Response:
        status_code = 200
        def json(self):
            raise ValueError('Not JSON')

    monkeypatch.setattr(voice, 'stt', REAL_STT)
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'offline-test')
    monkeypatch.setattr(voice.requests, 'post', lambda *args, **kwargs: Response())
    cid = start(client, dry_run=True)
    assert client.post(f'/crash/{cid}/reply', files={'audio': ('reply.m4a', b'audio')}).status_code == 502
    assert client.get(f'/crash/{cid}').json()['status'] == 'pending'


@pytest.mark.parametrize('transcript', ['', '  ', "  I'm okay  "])
def test_valid_transcription_is_trimmed(monkeypatch, transcript):
    class Response:
        status_code = 200
        def json(self):
            return {'text': transcript}

    monkeypatch.setenv('ELEVENLABS_API_KEY', 'offline-test')
    monkeypatch.setattr(voice.requests, 'post', lambda *args, **kwargs: Response())
    assert REAL_STT(b'audio') == transcript.strip()


@pytest.mark.parametrize('stored', [
    'not JSON', '[]', 'null', '{"bad-token": {}}',
    '{"' + 'a' * 64 + '": null}',
    '{"' + 'a' * 64 + '": {"id": "device", "name": "Phone", "paired_at": "invalid"}}',
])
def test_damaged_pairing_store_is_preserved_and_rejected(client, stored):
    pairing_store.STORE.write_text(stored, encoding='utf-8')
    assert not pairing_store.has_device_token('x' * 32)
    assert client.get('/pairing/devices').status_code == 503
    code = client.post('/pairing/issue').json()['code']
    assert client.post('/pairing/exchange', json={'code': code}).status_code == 503
    assert client.delete('/pairing/devices/unknown').status_code == 503
    assert pairing_store.STORE.read_text(encoding='utf-8') == stored


def test_voice_storage_error_has_a_recoverable_response(client, monkeypatch):
    def unavailable(*args):
        raise PermissionError('Cache inaccessible')
    monkeypatch.setattr(voice, 'say', unavailable)
    assert client.post('/voice/alert', json={}).status_code == 502


def test_pairing_expiration_uses_elapsed_time_and_rejects_exact_deadline(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(pairing.time, 'monotonic', lambda: now[0])
    codes = pairing.PairingCodes()
    valid = codes.issue()
    expired = codes.issue()
    now[0] += pairing.CODE_TTL_SECONDS - 0.01
    assert codes.consume(valid)
    assert not codes.consume(valid)
    now[0] += 0.01
    assert not codes.consume(expired)


def test_failed_pairing_store_write_preserves_existing_devices(client, monkeypatch):
    record = pairing_store.add_device('x' * 32, 'Existing phone')
    original = pairing_store.STORE.read_bytes()
    def unavailable(*args):
        raise PermissionError('Store is read-only')
    monkeypatch.setattr(type(pairing_store.STORE), 'replace', unavailable)
    code = client.post('/pairing/issue').json()['code']
    assert client.post('/pairing/exchange', json={'code': code}).status_code == 503
    assert client.delete('/pairing/devices/' + record['id']).status_code == 503
    assert pairing_store.STORE.read_bytes() == original
    assert pairing_store.has_device_token('x' * 32)


def test_replaced_timer_is_rejected_even_when_deadline_is_identical(client, monkeypatch):
    monkeypatch.setattr(crash.time, 'time', lambda: 1_800_000_000.0)
    cid = start(client, dry_run=True)
    c = crash.CRASHES[cid]
    initial_timer, deadline = c['_timer'], c['deadline']
    with crash.LOCK:
        crash._schedule(c, 60)
    assert c['deadline'] == deadline and c['_timer'] is not initial_timer
    initial_timer.callback()
    assert client.get(f'/crash/{cid}').json()['status'] == 'pending'
    c['_timer'].callback()
    assert client.get(f'/crash/{cid}').json()['status'] == 'no_response'
