"""Authenticated voice/check-in endpoints. State and timers are process-local.
One worker only. Dry-run sessions never call the Photon agent.
"""
import asyncio
import os
import threading
import time
import uuid
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
    if c.get('_timer'):
        c['_timer'].cancel()
    c['deadline'] = time.time() + seconds
    timer = threading.Timer(seconds, lambda: _resolve(c, 'no_response'))
    timer.daemon = True
    c['_timer'] = timer
    timer.start()


@router.post('/crash/start')
def crash_start(req: CrashStart):
    crash_id = str(req.client_event_id)
    with LOCK:
        # Keep only recent resolved sessions; unresolved sessions have a bounded timer.
        for old_id, old in list(CRASHES.items()):
            if old['status'] != 'pending' and time.time() - old['time'] > 86400:
                del CRASHES[old_id]
        if crash_id not in CRASHES:
            c = {**req.model_dump(mode='json'), 'id': crash_id, 'time': time.time(),
                 'status': 'pending', 'alerted': False, 'armed': False}
            CRASHES[crash_id] = c
            _schedule(c, 60)  # Fallback if the phone disconnects before the prompt finishes.
    return {'crash_id': crash_id, 'checkin_audio_url': f'/crash/{crash_id}/checkin.mp3',
            'dry_run': CRASHES[crash_id]['dry_run']}


@router.post('/crash/{crash_id}/arm')
def arm(crash_id: str):
    c = _get(crash_id)
    with LOCK:
        if c['status'] == 'pending' and not c['armed']:
            c['armed'] = True
            _schedule(c, 20)  # Starts after playback, not while the prompt is talking.
        return _public(c)


@router.get('/crash/{crash_id}/checkin.mp3')
def crash_checkin(crash_id: str):
    c = _get(crash_id)
    return _audio('checkin', c['lang'])


def _send_photon_alert(c: dict) -> dict:
    recipients = contacts.confirmed_contacts()
    if not recipients:
        return {'sent': 0, 'failed': [], 'error': 'No confirmed emergency contacts.'}
    payload = {'crashId': c['id'], 'lat': c['lat'], 'lon': c['lon'],
               'speedMph': c['speed_mph'], 'time': c['time'], 'status': c['status'],
               'transcript': c.get('transcript', ''), 'contacts': recipients}
    try:
        return _agent('/alert', payload)
    except (requests.Timeout, requests.ConnectionError):
        # The agent deduplicates this immutable crash ID. A lost HTTP response
        # can be recovered without scheduling the recipients a second time.
        return _agent('/alert', payload)


def _resolve(c: dict, status: str, transcript: str = '') -> dict:
    # The lock serializes manual buttons, transcription, and the server timer.
    # Once decided, a retry returns the same result and never sends another alert.
    with LOCK:
        if c['status'] != 'pending':
            return _public(c)
        c['status'], c['transcript'] = status, transcript
        c['_timer'].cancel()
        c['sent'] = 0
        c['failed'] = []
        c['failed_count'] = 0
        c['alert_error'] = None
        c['delivery_pending'] = status != 'ok' and not c['dry_run']
        if not c['delivery_pending']:
            c['ack_audio_url'] = f"/crash/{c['id']}/ack.mp3"
            return _public(c)
    # Slow provider calls run outside the global session lock. Polling and
    # other check-ins remain responsive and expose a pending delivery state.
    try:
        delivery = _send_photon_alert(c)
        if not isinstance(delivery, dict):
            raise ValueError('Invalid agent delivery result.')
        sent = delivery.get('sent', 0)
        failed = delivery.get('failed', [])
        if type(sent) is not int or sent < 0 or not isinstance(failed, list):
            raise ValueError('Invalid agent delivery result.')
        error = delivery.get('error') or ('No contact message was accepted by Photon.' if sent == 0 else None)
    except (requests.RequestException, RuntimeError, ValueError, TypeError):
        sent, failed = 0, []
        error = 'Photon could not confirm sending. Check the agent connection.'
    with LOCK:
        c['sent'], c['failed'], c['failed_count'] = sent, failed, len(failed)
        c['alerted'] = sent > 0
        c['alert_error'] = error
        c['delivery_pending'] = False
        c['ack_audio_url'] = f"/crash/{c['id']}/ack.mp3"
        return _public(c)


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
