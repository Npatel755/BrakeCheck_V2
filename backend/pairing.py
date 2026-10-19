"""Short-lived QR pairing codes and device-token exchange."""
import hashlib
import secrets
import threading
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import config
from pairing_store import add_device, list_devices, revoke_device

router = APIRouter(prefix='/pairing', tags=['pairing'])
CODE_TTL_SECONDS = 300


class PairingCodes:
    def __init__(self):
        self._lock = threading.RLock()
        self._pending: dict[str, float] = {}

    @staticmethod
    def _hash(code: str) -> str:
        return hashlib.sha256(code.encode('utf-8')).hexdigest()

    def issue(self) -> str:
        now = time.time()
        code = secrets.token_urlsafe(24)
        with self._lock:
            self._pending = {key: expires for key, expires in self._pending.items() if expires > now}
            self._pending[self._hash(code)] = now + CODE_TTL_SECONDS
        return code

    def consume(self, code: str) -> bool:
        digest = self._hash(code)
        with self._lock:
            expires = self._pending.pop(digest, None)
        return expires is not None and expires >= time.time()


PAIRING_CODES = PairingCodes()


class ExchangeRequest(BaseModel):
    code: str = Field(min_length=32, max_length=128)
    device_name: str = Field(default='iPhone', min_length=1, max_length=80)


@router.post('/issue', dependencies=[Depends(config.require_admin)])
def issue_pairing_code():
    return {'code': PAIRING_CODES.issue(), 'expires_in': CODE_TTL_SECONDS}


@router.post('/exchange')
def exchange_pairing_code(req: ExchangeRequest):
    if not PAIRING_CODES.consume(req.code):
        raise HTTPException(401, 'Pairing QR code is expired or has already been used. Create a new one on the computer.')
    device_token = secrets.token_urlsafe(32)
    record = add_device(device_token, req.device_name)
    return {
        'device_token': device_token,
        'device_id': record['id'],
        'device_name': record['name'],
    }


@router.get('/devices', dependencies=[Depends(config.require_admin)])
def paired_devices():
    return list_devices()


@router.delete('/devices/{device_id}', dependencies=[Depends(config.require_admin)])
def unpair_device(device_id: str):
    if not revoke_device(device_id):
        raise HTTPException(404, 'Paired device not found.')
    return {'removed': device_id}
