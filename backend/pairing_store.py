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


class DeviceStoreError(RuntimeError):
    """The existing device registry cannot be safely read or changed."""


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _load() -> dict:
    try:
        value = json.loads(STORE.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise DeviceStoreError('Paired-device storage is unavailable or damaged.') from exc
    if not isinstance(value, dict):
        raise DeviceStoreError('Paired-device storage has an invalid format.')
    ids = set()
    for digest, record in value.items():
        if (len(digest) != 64 or any(ch not in '0123456789abcdef' for ch in digest)
                or not isinstance(record, dict)
                or not isinstance(record.get('id'), str) or not record['id']
                or record['id'] in ids
                or not isinstance(record.get('name'), str) or not record['name'].strip()
                or len(record['name']) > 80
                or type(record.get('paired_at')) not in (int, float)
                or not 0 <= record['paired_at'] <= 253402300799):
            raise DeviceStoreError('Paired-device storage contains an invalid record.')
        ids.add(record['id'])
    return value


def _save(devices: dict):
    try:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        temporary = STORE.with_suffix('.tmp')
        temporary.write_text(json.dumps(devices, indent=2), encoding='utf-8')
        temporary.replace(STORE)
    except OSError as exc:
        raise DeviceStoreError('Paired-device storage could not be updated.') from exc


def has_device_token(token: str) -> bool:
    if len(token) < 32:
        return False
    digest = _digest(token)
    with LOCK:
        try:
            return digest in _load()
        except DeviceStoreError:
            return False


def add_device(token: str, name: str) -> dict:
    record = {
        'id': str(uuid.uuid4()),
        'name': name.strip()[:80] or 'iPhone',
        'paired_at': time.time(),
    }
    with LOCK:
        devices = _load()
        devices[_digest(token)] = record
        _save(devices)
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
        _save(devices)
    return True
