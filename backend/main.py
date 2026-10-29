"""Run one worker: python -m uvicorn main:app --host 0.0.0.0 --port 8000"""
import logging
import os
from contextlib import asynccontextmanager

import config  # Loads backend/.env before importing services.
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
import contacts
import crash
import pairing
from discovery import BonjourAdvertiser
from pairing_store import DeviceStoreError


@asynccontextmanager
async def lifespan(app: FastAPI):
    crash.recover()
    advertiser = None
    if os.getenv('BRAKECHECK_DISABLE_MDNS') != '1':
        candidate = BonjourAdvertiser()
        try:
            candidate.start()
            advertiser = candidate
        except Exception as exc:
            logging.getLogger(__name__).warning('Bonjour discovery unavailable: %s', exc)
            candidate.close()
    try:
        yield
    finally:
        if advertiser is not None:
            advertiser.close()


app = FastAPI(title='BrakeCheck', version='0.4.0', lifespan=lifespan)


@app.exception_handler(DeviceStoreError)
async def unavailable_device_store(request, exc):
    return JSONResponse(status_code=503, content={
        'detail': 'Paired-device storage is unavailable or damaged. Restore it on the backend before pairing again.'
    })


app.include_router(crash.router)
app.include_router(contacts.router)
app.include_router(pairing.router)


@app.get('/health', dependencies=[Depends(config.require_client)])
def health():
    return {'ok': True, 'name': 'BrakeCheck', 'voice_configured': bool(crash.voice._voice_env('ELEVENLABS_API_KEY')),
            'photon_configured': bool(config.os.getenv('SPECTRUM_PROJECT_ID') and config.os.getenv('SPECTRUM_PROJECT_SECRET')),
            'confirmed_contacts': len(contacts.confirmed_contacts())}


@app.get('/healthz')
def healthz():
    """Minimal unauthenticated platform probe; exposes no configuration or user data."""
    return {'ok': True}
