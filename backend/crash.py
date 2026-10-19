"""Authenticated voice/check-in endpoints for one backend worker.
Dry-run sessions never call the Photon agent.
"""
import asyncio
import json
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

import requests
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator

from config import require_client
import contacts
import voice

router = APIRouter(dependencies=[Depends(require_client)])
CRASHES: dict[str, dict] = {}
LOCK = threading.RLock()
MAX_AUDIO = 5 * 1024 * 1024
MAX_RECOVERY_AGE = 5 * 60
MAX_SESSIONS = 1024
MAX_IMPORT_SESSIONS = 8192
STORE = Path(os.getenv('CRASHES_FILE', Path(__file__).with_name('crashes.json')))


def _save_locked():
    """Commit check-in state before acknowledging it or contacting the agent."""
    STORE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STORE.with_name(f'{STORE.name}.{os.getpid()}.tmp')
    sessions = {key: {name: value for name, value in c.items() if not name.startswith('_')}
                for key, c in CRASHES.items()}
    temporary.write_text(json.dumps({'version': 1, 'sessions': sessions}), encoding='utf-8')
    temporary.replace(STORE)


def _start_timer_locked(c: dict):
    deadline = c['deadline']
    timer = threading.Timer(max(0, deadline - time.time()),
                            lambda: _resolve(c, 'no_response', expected_deadline=deadline))
    timer.daemon = True
    c['_timer'] = timer
    timer.start()


def recover():
    """Restore active countdowns and unfinished agent requests after a restart."""
    if not STORE.exists():
        return []
    try:
        state = json.loads(STORE.read_text(encoding='utf-8'))
        if not isinstance(state, dict) or state.get('version') != 1 or not isinstance(state.get('sessions'), dict):
            raise ValueError('Invalid crash state header.')
        sessions = state['sessions']
        if len(sessions) > MAX_IMPORT_SESSIONS:
            raise ValueError('Too many stored check-ins.')
        for key, c in sessions.items():
            if (not isinstance(key, str) or not isinstance(c, dict) or c.get('id') != key
                    or not {'client_event_id', 'lat', 'lon', 'speed_mph', 'lang', 'armed', 'alerted'} <= c.keys()
                    or not isinstance(c.get('client_event_id'), str)
                    or c.get('status') not in ('pending', 'ok', 'help', 'no_response')
                    or type(c.get('time')) not in (int, float) or not math.isfinite(c['time'])
                    or c['time'] < 0 or type(c.get('dry_run')) is not bool
                    or type(c.get('armed')) is not bool or type(c.get('alerted')) is not bool
                    or (c.get('delivery_pending') is not None and type(c['delivery_pending']) is not bool)
                    or any(name.startswith('_') for name in c)
                    or (c.get('delivery_pending') and (c['status'] == 'pending' or c['dry_run']))):
                raise ValueError('Invalid stored check-in.')
            if str(uuid.UUID(key)) != key or str(uuid.UUID(c['client_event_id'])) != key:
                raise ValueError('Invalid stored check-in ID.')
            CrashStart.model_validate(c)
            if c['status'] == 'pending' and (type(c.get('deadline')) not in (int, float)
                                             or not math.isfinite(c['deadline'])):
                raise ValueError('Invalid stored countdown.')
            if c['status'] != 'pending' and (type(c.get('sent')) is not int or not 0 <= c['sent'] <= 50
                                             or not isinstance(c.get('failed'), list)):
                raise ValueError('Invalid stored delivery status.')
            recipients = c.get('alert_contacts')
            if (recipients is not None and (not isinstance(recipients, list) or len(recipients) > 50
                                            or any(not isinstance(phone, str)
                                                   or re.fullmatch(r'\+[1-9][0-9]{7,14}', phone) is None
                                                   for phone in recipients))):
                raise ValueError('Invalid stored alert recipients.')
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError('Stored crash check-ins could not be restored safely.') from exc
    with LOCK:
        CRASHES.clear()
        CRASHES.update(sessions)
        changed = False
        now = time.time()
        for crash_id, c in list(CRASHES.items()):
            if c['status'] != 'pending' and not c.get('delivery_pending') and now - c['time'] > 86400:
                del CRASHES[crash_id]
                changed = True
                continue
            if now - c['time'] > MAX_RECOVERY_AGE and (c['status'] == 'pending' or c.get('delivery_pending')):
                was_pending = c['status'] == 'pending'
                c.update(status='no_response' if was_pending else c['status'], sent=0, failed=[],
                         failed_count=0, alerted=False, delivery_pending=False,
                         alert_error=('Check-in expired while the service was offline; no alert was sent.'
                                      if was_pending else 'Alert acceptance is unknown after an extended outage.'),
                         ack_audio_url=f"/crash/{c['id']}/ack.mp3")
                changed = True
        if len(CRASHES) > MAX_SESSIONS:
            # Older versions could write more sessions than recovery accepted.
            # Preserve every live countdown and alert; evict oldest completed results.
            completed = sorted((c for c in CRASHES.values()
                                if c['status'] != 'pending' and not c.get('delivery_pending')),
                               key=lambda c: c['time'])
            for c in completed[:len(CRASHES) - MAX_SESSIONS]:
                del CRASHES[c['id']]
                changed = True
            if len(CRASHES) > MAX_SESSIONS:
                raise RuntimeError('Too many active check-ins to restore safely.')
        if changed:
            _save_locked()
        for c in CRASHES.values():
            if c['status'] == 'pending':
                _start_timer_locked(c)
        unfinished = [c for c in CRASHES.values() if c.get('delivery_pending')]
    workers = []
    for c in unfinished:
        worker = threading.Thread(target=_resume_delivery, args=(c,), daemon=True)
        worker.start()
        workers.append(worker)
    return workers


def _agent(path: str, payload: dict) -> dict:
    key = os.getenv('AGENT_SHARED_KEY', '')
    if len(key) < 24:
        raise RuntimeError('Photon agent key is not configured.')
    response = requests.post(os.getenv('PHOTON_AGENT_URL', 'http://127.0.0.1:8787').rstrip('/') + path,
                             headers={'X-Agent-Key': key}, json=payload, timeout=20)
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict):
        raise RuntimeError('Photon agent returned an invalid response.')
    return result


def _audio(kind: str, lang: str) -> Response:
    try:
        return Response(voice.say(kind, lang), media_type='audio/mpeg')
    except (requests.RequestException, RuntimeError):
        raise HTTPException(502, 'Voice service unavailable. Use the on-screen check-in buttons.')


class AlertRequest(BaseModel):
    kind: Literal['brake'] = 'brake'
    lang: Literal['en', 'es'] = 'en'


@router.post('/voice/alert')
def voice_alert(req: AlertRequest):
    return _audio(req.kind, req.lang)


@router.post('/voice/prewarm')
def voice_prewarm(lang: Literal['en', 'es'] = 'en'):
    done = []
    for kind in voice.PHRASES[lang]:
        _audio(kind, lang)
        done.append(kind)
    return {'cached': done, 'lang': lang}


class CrashStart(BaseModel):
    lat: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    lon: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    speed_mph: float | None = Field(default=None, ge=0, le=220, allow_inf_nan=False)
    lang: Literal['en', 'es'] = 'en'
    dry_run: bool = False
    client_event_id: uuid.UUID = Field(default_factory=uuid.uuid4)

    @model_validator(mode='after')
    def coordinate_pair(self):
        if (self.lat is None) != (self.lon is None):
            raise ValueError('Supply both coordinates or neither.')
        return self


def _get(crash_id: str) -> dict:
    with LOCK:
        if crash_id not in CRASHES:
            raise HTTPException(404, 'Unknown crash ID. The backend may have restarted.')
        return CRASHES[crash_id]


def _public(c: dict) -> dict:
    with LOCK:
        return {k: v for k, v in c.items() if not k.startswith('_')}


def _schedule(c: dict, seconds: int):
    previous_deadline = c.get('deadline')
    c['deadline'] = time.time() + seconds
    try:
        _save_locked()
    except Exception:
        if previous_deadline is None:
            del c['deadline']
        else:
            c['deadline'] = previous_deadline
        raise
    if c.get('_timer'):
        c['_timer'].cancel()
    _start_timer_locked(c)


@router.post('/crash/start')
def crash_start(req: CrashStart):
    crash_id = str(req.client_event_id)
    with LOCK:
        # Keep only recent resolved sessions; unresolved sessions have a bounded timer.
        for old_id, old in list(CRASHES.items()):
            if (old['status'] != 'pending' and not old.get('delivery_pending')
                    and time.time() - old['time'] > 86400):
                del CRASHES[old_id]
        if crash_id in CRASHES:
            existing = CRASHES[crash_id]
            if any(existing.get(key) != value for key, value in req.model_dump(mode='json').items()):
                raise HTTPException(409, 'This check-in ID already belongs to a different event payload.')
        if crash_id not in CRASHES:
            if len(CRASHES) >= MAX_SESSIONS:
                completed = sorted((old for old in CRASHES.values()
                                    if old['status'] != 'pending' and not old.get('delivery_pending')),
                                   key=lambda old: old['time'])
                for old in completed[:len(CRASHES) - MAX_SESSIONS + 1]:
                    del CRASHES[old['id']]
            if len(CRASHES) >= MAX_SESSIONS:
                raise HTTPException(503, 'Too many active check-ins. Try again shortly.')
            c = {**req.model_dump(mode='json'), 'id': crash_id, 'time': time.time(),
                 'status': 'pending', 'alerted': False, 'armed': False}
            CRASHES[crash_id] = c
            try:
                _schedule(c, 60)  # Fallback if the phone disconnects before the prompt finishes.
            except Exception:
                del CRASHES[crash_id]
                raise
    return {'crash_id': crash_id, 'checkin_audio_url': f'/crash/{crash_id}/checkin.mp3',
            'dry_run': CRASHES[crash_id]['dry_run']}


@router.post('/crash/{crash_id}/arm')
def arm(crash_id: str):
    c = _get(crash_id)
    with LOCK:
        if c['status'] == 'pending' and not c['armed']:
            c['armed'] = True
            try:
                _schedule(c, 20)  # Starts after playback, not while the prompt is talking.
            except Exception:
                c['armed'] = False
                raise
        return _public(c)


@router.get('/crash/{crash_id}/checkin.mp3')
def crash_checkin(crash_id: str):
    c = _get(crash_id)
    return _audio('checkin', c['lang'])


def _send_photon_alert(c: dict) -> dict:
    with LOCK:
        if 'alert_contacts' not in c:
            # The agent binds recipients to the crash ID. Persist their snapshot
            # before the first request so restart retries cannot change it.
            c['alert_contacts'] = contacts.confirmed_contacts()
            try:
                _save_locked()
            except Exception:
                del c['alert_contacts']
                raise
        recipients = c['alert_contacts']
    if not recipients:
        return {'sent': 0, 'failed': [], 'error': 'No confirmed emergency contacts.'}
    payload = {'crashId': c['id'], 'lat': c['lat'], 'lon': c['lon'],
               'speedMph': c['speed_mph'], 'time': c['time'], 'status': c['status'],
               'transcript': c.get('transcript', ''), 'contacts': recipients}
    return _agent('/alert', payload)


def _resolve(c: dict, status: str, transcript: str = '', expected_deadline: float | None = None) -> dict:
    # The lock serializes manual buttons, transcription, and the server timer.
    # Once decided, a retry returns the same result and never sends another alert.
    with LOCK:
        if c['status'] != 'pending' or (expected_deadline is not None
                                       and c.get('deadline') != expected_deadline):
            return _public(c)
        before = c.copy()
        c['status'], c['transcript'] = status, transcript
        c['sent'] = 0
        c['failed'] = []
        c['failed_count'] = 0
        c['alert_error'] = None
        c['delivery_pending'] = status != 'ok' and not c['dry_run']
        if not c['delivery_pending']:
            c['ack_audio_url'] = f"/crash/{c['id']}/ack.mp3"
        try:
            _save_locked()
        except Exception:
            c.clear()
            c.update(before)
            raise
        c['_timer'].cancel()
        if not c['delivery_pending']:
            return _public(c)
    # A local-agent outage must not turn an active emergency into a permanent
    # failure. Polling and check-in buttons stay responsive during retries.
    worker = threading.Thread(target=_resume_delivery, args=(c,), daemon=True)
    c['_delivery_worker'] = worker
    worker.start()
    return _public(c)


def _transient_agent_error(exc: Exception) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError, OSError)):
        return True
    return (isinstance(exc, requests.HTTPError) and exc.response is not None
            and (exc.response.status_code == 429 or exc.response.status_code >= 500))


def _finish_delivery(c: dict, retry_transient: bool = False) -> dict:
    try:
        delivery = _send_photon_alert(c)
        if not isinstance(delivery, dict):
            raise ValueError('Invalid agent delivery result.')
        sent = delivery.get('sent', 0)
        failed = delivery.get('failed', [])
        if (type(sent) is not int or not 0 <= sent <= 50 or not isinstance(failed, list)
                or len(failed) > 50 or sent + len(failed) > 50
                or any(not isinstance(item, dict) or not isinstance(item.get('to'), str)
                       or not isinstance(item.get('error'), str) for item in failed)):
            raise ValueError('Invalid agent delivery result.')
        error = delivery.get('error') or ('No contact message was accepted by Photon.' if sent == 0 else None)
    except (requests.RequestException, RuntimeError, ValueError, TypeError, OSError) as exc:
        if retry_transient and _transient_agent_error(exc):
            raise
        sent, failed = 0, []
        error = 'Photon could not confirm sending. Check the agent connection.'
    with LOCK:
        if not c.get('delivery_pending'):
            return _public(c)
        before = c.copy()
        c['sent'], c['failed'], c['failed_count'] = sent, failed, len(failed)
        c['alerted'] = sent > 0
        c['alert_error'] = error
        c['delivery_pending'] = False
        c['ack_audio_url'] = f"/crash/{c['id']}/ack.mp3"
        try:
            _save_locked()
        except Exception:
            c.clear()
            c.update(before)
            raise
        return _public(c)


def _resume_delivery(c: dict):
    # Backend and agent start together. Transient outages can last longer than
    # startup, but stale incidents must not be sent as new emergency alerts.
    delay = 1.0
    while True:
        with LOCK:
            # A replaced worker or an isolated test must not keep an orphaned
            # retry alive after its session is no longer the active record.
            if CRASHES.get(c['id']) is not c:
                return
            if not c.get('delivery_pending'):
                return
            remaining = c['time'] + MAX_RECOVERY_AGE - time.time()
            if remaining <= 0:
                _expire_delivery_locked(c)
                return
        try:
            _finish_delivery(c, retry_transient=True)
            return
        except (requests.RequestException, OSError) as exc:
            if not _transient_agent_error(exc):
                raise
        time.sleep(min(delay, max(0, c['time'] + MAX_RECOVERY_AGE - time.time())))
        delay = min(delay * 2, 15)


def _expire_delivery_locked(c: dict):
    before = c.copy()
    c.update(sent=0, failed=[], failed_count=0, alerted=False, delivery_pending=False,
             alert_error='Alert acceptance is unknown after the service was unavailable too long.',
             ack_audio_url=f"/crash/{c['id']}/ack.mp3")
    try:
        _save_locked()
    except Exception:
        c.clear()
        c.update(before)
        raise


@router.get('/crash/{crash_id}/ack.mp3')
def crash_ack(crash_id: str):
    c = _get(crash_id)
    if c['status'] == 'pending' or c.get('delivery_pending'):
        raise HTTPException(409, 'Check-in or alert delivery is still pending.')
    kind = 'demo_ack' if c['dry_run'] else 'ok_ack' if c['status'] == 'ok' else 'help_ack' if c['alerted'] else 'failed_ack'
    return _audio(kind, c['lang'])


@router.post('/crash/{crash_id}/reply')
async def crash_reply(crash_id: str, audio: UploadFile = File(...)):
    c = _get(crash_id)
    if c['status'] != 'pending':
        return _public(c)
    content = await audio.read(MAX_AUDIO + 1)
    await audio.close()
    if len(content) > MAX_AUDIO:
        raise HTTPException(413, 'Reply audio exceeds 5 MB.')
    try:
        transcript = await asyncio.to_thread(voice.stt, content, 'reply.m4a') if content else ''
    except (requests.RequestException, RuntimeError):
        # Keep the server deadline active. The app can still submit a manual response.
        raise HTTPException(502, 'Transcription failed. Use a check-in button before the countdown ends.')
    return await asyncio.to_thread(_resolve, c, voice.classify_reply(transcript), transcript)


class Decision(BaseModel):
    status: Literal['ok', 'help']


@router.post('/crash/{crash_id}/respond')
def respond(crash_id: str, decision: Decision):
    return _resolve(_get(crash_id), decision.status, 'Selected on screen')


@router.post('/crash/{crash_id}/timeout')
def crash_timeout(crash_id: str):
    return _resolve(_get(crash_id), 'no_response')


@router.get('/crash/{crash_id}')
def crash_get(crash_id: str):
    return _public(_get(crash_id))


class Trip(BaseModel):
    minutes: float = Field(ge=0, le=1440, allow_inf_nan=False)
    hardBrakes: int = Field(ge=0)
    closeCalls: int = Field(ge=0)
    maxSpeedMph: float = Field(ge=0, le=220, allow_inf_nan=False)
    time: float = Field(ge=0, allow_inf_nan=False)


@router.post('/trip')
def trip_summary(trip: Trip):
    try:
        return _agent('/trip', trip.model_dump())
    except (requests.RequestException, RuntimeError):
        raise HTTPException(502, 'Trip summary could not reach the Photon agent.')
