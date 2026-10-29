"""Single-driver demo configuration; load only this backend's .env."""
import os
import secrets
from pathlib import Path
from dotenv import load_dotenv
from fastapi import Header, HTTPException

load_dotenv(Path(__file__).with_name('.env'))

# Load .env first because the store reads its configurable path at import time.
from pairing_store import has_device_token


def _matches_key(provided: str | None, expected: str) -> bool:
    # compare_digest(str, str) rejects non-ASCII headers with TypeError.
    return secrets.compare_digest((provided or '').encode('utf-8'), expected.encode('utf-8'))


def require_admin(x_brakecheck_key: str | None = Header(default=None, alias='X-BrakeCheck-Key')):
    expected = os.getenv('BRAKECHECK_APP_KEY', '').strip()
    if len(expected) < 24:
        raise HTTPException(503, 'Run configure.py and set BRAKECHECK_APP_KEY first.')
    if not _matches_key(x_brakecheck_key, expected):
        raise HTTPException(401, 'Pairing administration key is incorrect.')


def require_client(
    x_brakecheck_key: str | None = Header(default=None, alias='X-BrakeCheck-Key'),
    x_brakecheck_device: str | None = Header(default=None, alias='X-BrakeCheck-Device'),
):
    expected = os.getenv('BRAKECHECK_APP_KEY', '').strip()
    key_is_configured = len(expected) >= 24
    if key_is_configured and _matches_key(x_brakecheck_key, expected):
        return
    if x_brakecheck_device and has_device_token(x_brakecheck_device):
        return
    if not key_is_configured:
        raise HTTPException(503, 'Run configure.py and set BRAKECHECK_APP_KEY first.')
    raise HTTPException(401, 'Connection is not paired. Scan a new pairing QR code.')


def require_agent(x_agent_key: str | None = Header(default=None)):
    expected = os.getenv('AGENT_SHARED_KEY', '').strip()
    if len(expected) < 24 or not _matches_key(x_agent_key, expected):
        raise HTTPException(401, 'Agent key is incorrect or not configured.')
