"""Single-driver contacts, confirmed only by inbound YES; STOP revokes access."""
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from config import require_agent, require_client

router = APIRouter()
STORE = Path(os.getenv('CONTACTS_FILE', Path(__file__).with_name('contacts.json')))
LOCK = threading.RLock()
API = 'https://spectrum.photon.codes'
TEST_MESSAGE_COOLDOWN = 60


def _load():
    return json.loads(STORE.read_text(encoding='utf-8')) if STORE.exists() else {}


def _save(data):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STORE.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
    temporary.replace(STORE)


def confirmed_contacts():
    with LOCK:
        return [p for p, c in _load().items() if c.get('confirmed')]


def _photon(method, path, **kwargs):
    pid, secret = os.getenv('SPECTRUM_PROJECT_ID', ''), os.getenv('SPECTRUM_PROJECT_SECRET', '')
    if not pid or not secret:
        raise HTTPException(503, 'Photon credentials are not configured.')
    try:
        r = requests.request(method, f'{API}/projects/{pid}{path}', auth=(pid, secret), timeout=20, **kwargs)
        if method == 'DELETE' and r.status_code == 404:
            return {}
        r.raise_for_status()
        return (r.json().get('data') or {}) if r.content else {}
    except (requests.RequestException, ValueError):
        raise HTTPException(502, 'Photon could not update this contact. Check credentials and project user limits.')


def _invite(c):
    url = f"{API}/users/{c['photon_user_id']}/redirect?msg=YES"
    text = (f"Hi {c['first_name']}, I added you as an emergency contact on BrakeCheck. "
            f"If I may have been in a crash, it can text you my location. Tap to accept: {url}")
    return {**c, 'invite_url': url, 'invite_text': text, 'sms_url': f"sms:{c['phone']}&body={quote(text)}"}


def _send_test_message(contact):
    """Ask the local Photon agent to send one location-free test to this contact."""
    key = os.getenv('AGENT_SHARED_KEY', '')
    if len(key) < 24:
        raise HTTPException(503, 'Photon messaging is not configured.')
    agent_url = os.getenv('PHOTON_AGENT_URL', 'http://127.0.0.1:8787').rstrip('/')
    try:
        response = requests.post(
            f'{agent_url}/test',
            headers={'X-Agent-Key': key},
            json={
                'phone': contact['phone'],
                'first_name': contact['first_name'],
                'driver_name': os.getenv('DRIVER_NAME', 'Harrison'),
            },
            timeout=25,
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError):
        raise HTTPException(502, 'Photon did not confirm whether the test message was accepted.')
    if result.get('accepted') is not True:
        raise HTTPException(502, 'Photon did not accept the test message.')
    return {'accepted': True}


class NewContact(BaseModel):
    phone: str = Field(pattern=r'^\+[1-9]\d{7,14}$')
    first_name: str = Field(min_length=1, max_length=60)


@router.post('/contacts', dependencies=[Depends(require_client)])
def add_contact(req: NewContact):
    with LOCK:
        data = _load()
        if req.phone in data:
            data[req.phone]['first_name'] = req.first_name.strip() or 'Contact'
        else:
            user = _photon('POST', '/users/', json={'type': 'shared', 'phoneNumber': req.phone, 'firstName': req.first_name})
            if not user.get('id'):
                raise HTTPException(502, 'Photon did not return a user ID.')
            data[req.phone] = {'phone': req.phone, 'first_name': req.first_name.strip() or 'Contact',
                              'photon_user_id': user['id'], 'assigned_line': user.get('assignedPhoneNumber'),
                              'confirmed': False, 'added_at': time.time()}
        _save(data)
        return _invite(data[req.phone])


@router.get('/contacts', dependencies=[Depends(require_client)])
def list_contacts():
    with LOCK:
        return [_invite(c) for c in _load().values()]


class Confirm(BaseModel):
    phone: str
    confirmed: bool = True


@router.post('/contacts/confirm', dependencies=[Depends(require_agent)])
def confirm_contact(req: Confirm):
    with LOCK:
        data = _load()
        if req.phone not in data:
            return {'known': False}
        data[req.phone]['confirmed'] = req.confirmed
        _save(data)
        return {'known': True, 'first_name': data[req.phone]['first_name'],
                'confirmed': req.confirmed, 'driver_name': os.getenv('DRIVER_NAME', 'Harrison')}


@router.get('/contacts/authorized', dependencies=[Depends(require_agent)])
def authorized(phone: str):
    return {'authorized': phone in confirmed_contacts()}


@router.post('/contacts/{phone}/test', dependencies=[Depends(require_client)])
def test_contact(phone: str):
    """Send a clearly labeled test message, never an emergency payload or location."""
    with LOCK:
        data = _load()
        contact = data.get(phone)
        if not contact:
            raise HTTPException(404, 'Unknown contact.')
        if not contact.get('confirmed'):
            raise HTTPException(409, 'This contact must reply YES before receiving messages.')
        now = time.time()
        last_test = float(contact.get('last_test_message_at', 0))
        if now - last_test < TEST_MESSAGE_COOLDOWN:
            remaining = max(1, int(TEST_MESSAGE_COOLDOWN - (now - last_test)))
            raise HTTPException(429, f'Please wait {remaining} seconds before testing this contact again.')
        # Keep this reservation even on an ambiguous provider error; the message may have
        # reached the contact even if the response was lost.
        contact['last_test_message_at'] = now
        _save(data)

    return _send_test_message(contact)


@router.delete('/contacts/{phone}', dependencies=[Depends(require_client)])
def remove_contact(phone: str):
    with LOCK:
        data = _load()
        c = data.get(phone)
        if not c:
            raise HTTPException(404, 'Unknown contact.')
        # Revoke locally even if remote deletion fails; retain entry so deletion can be retried.
        c['confirmed'] = False
        _save(data)
        _photon('DELETE', f"/users/{c['photon_user_id']}/")
        del data[phone]
        _save(data)
        return {'removed': phone}
