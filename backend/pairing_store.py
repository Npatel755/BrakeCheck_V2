"""Persist paired-device token hashes; the phone's bearer token is never stored."""
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

STORE = Path(os.getenv('PAIRED_DEVICES_FILE', Path(__file__).with_name('paired_devices.json')))
LOCK = threading.RLock()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _load() -> dict:
    if not STORE.exists():
        return {}
    try:
        value = json.loads(STORE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def has_device_token(token: str) -> bool:
    if len(token) < 32:
        return False
    digest = _digest(token)
    with LOCK:
        return digest in _load()


def add_device(token: str, name: str) -> dict:
    record = {
        'id': str(uuid.uuid4()),
        'name': name.strip()[:80] or 'iPhone',
        'paired_at': time.time(),
    }
    with LOCK:
        devices = _load()
        devices[_digest(token)] = record
        STORE.parent.mkdir(parents=True, exist_ok=True)
        temporary = STORE.with_suffix('.tmp')
        temporary.write_text(json.dumps(devices, indent=2), encoding='utf-8')
        temporary.replace(STORE)
    return record


def list_devices() -> list[dict]:
    with LOCK:
        return list(_load().values())


def revoke_device(device_id: str) -> bool:
    with LOCK:
        devices = _load()
        matches = [key for key, item in devices.items() if item.get('id') == device_id]
        if not matches:
            return False
        for key in matches:
            del devices[key]
        STORE.parent.mkdir(parents=True, exist_ok=True)
        temporary = STORE.with_suffix('.tmp')
        temporary.write_text(json.dumps(devices, indent=2), encoding='utf-8')
        temporary.replace(STORE)
    return True
