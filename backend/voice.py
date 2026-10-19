"""ElevenLabs voice for Brake Check (MLH Best Use of ElevenLabs).

- tts(): spoken in-drive alerts and the post-crash check-in, cached on disk so repeat alerts are instant.
- stt(): transcribes the driver's spoken answer to "Are you okay?".
- classify_reply(): turns that transcript into ok / help / no_response (plain keyword rules, no LLM).

Uses the ElevenLabs REST API directly (no SDK), key in ELEVENLABS_API_KEY.
"""
import hashlib
import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

API = "https://api.elevenlabs.io/v1"
DEFAULT_VOICE = "JBFqnCBsd6RMkjVDRZzb"


def _voice_env(name: str) -> str:
    v = os.getenv(name, "").strip()
    return "" if not v or v.startswith("PASTE_") else v


# Two voices: ALERT for in-drive warnings (urgent), CALM for the crash check-in and follow-ups.
# Until an alert voice is chosen, alerts fall back to the calm voice with urgent delivery settings.
CALM_VOICE_ID = _voice_env("ELEVENLABS_CALM_VOICE_ID") or _voice_env("ELEVENLABS_VOICE_ID") or DEFAULT_VOICE
ALERT_VOICE_ID = _voice_env("ELEVENLABS_ALERT_VOICE_ID") or CALM_VOICE_ID

# Delivery per role. Lower stability and higher style = more expressive; speed range is 0.7 to 1.2.
VOICE_SETTINGS = {
    "alert": {"stability": 0.3, "similarity_boost": 0.8, "style": 0.6, "use_speaker_boost": True, "speed": 1.1},
    "calm": {"stability": 0.7, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True, "speed": 0.95},
}
ROLE_OF = {"brake": "alert", "checkin": "calm", "ok_ack": "calm", "help_ack": "calm", "noresp_ack": "calm", "demo_ack": "calm", "failed_ack": "calm"}
TTS_MODEL = os.getenv("ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2")
STT_MODEL = os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2")
CACHE_DIR = Path(os.getenv("VOICE_CACHE_DIR", Path(__file__).parent / "voice_cache"))

# Standard phrases. eleven_multilingual_v2 speaks whatever language the text is in.
PHRASES = {
    "en": {
        "demo_ack": "Demo complete. No emergency messages were sent.",
        "failed_ack": "I could not confirm that any emergency contact was notified. If you need urgent help, call emergency services.",
        "brake": "Brake now!",
        "checkin": "I detected a possible impact. Are you okay? Say I'm okay, or say help.",
        "ok_ack": "Glad you're okay. Check saved incidents for available footage.",
        "help_ack": "Your alert was sent to at least one emergency contact.",
        "noresp_ack": "I didn't hear you, so I'm texting your emergency contacts your location now.",
    },
    "es": {
        "demo_ack": "Demostración completa. No se enviaron mensajes de emergencia.",
        "failed_ack": "No pude confirmar el envío de la alerta. Si necesitas ayuda urgente, llama a emergencias.",
        "brake": "¡Frena ya!",
        "checkin": "Detecté un posible impacto. ¿Estás bien? Di estoy bien, o di ayuda.",
        "ok_ack": "Me alegra que estés bien. Revisa los incidentes guardados para ver el video disponible.",
        "help_ack": "Tu alerta fue enviada a por lo menos un contacto de emergencia.",
        "noresp_ack": "No te escuché, así que estoy enviando tu ubicación a tus contactos de emergencia.",
    },
}


def _key() -> str:
    key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set (see .env.example)")
    return key


def phrase(kind: str, lang: str = "en") -> str:
    return PHRASES.get(lang, PHRASES["en"]).get(kind) or PHRASES["en"][kind]


def say(kind: str, lang: str = "en") -> bytes:
    """MP3 for a standard phrase, in the voice and delivery that phrase's role calls for."""
    return tts(phrase(kind, lang), role=ROLE_OF[kind])


def tts(text: str, role: str = "calm", voice_id: str | None = None) -> bytes:
    """Return MP3 bytes for text in the given role ('alert' or 'calm'), from the disk cache when possible."""
    settings = VOICE_SETTINGS[role]
    voice_id = voice_id or (ALERT_VOICE_ID if role == "alert" else CALM_VOICE_ID)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(f"{voice_id}|{TTS_MODEL}|{role}|{sorted(settings.items())}|{text}".encode()).hexdigest()[:16]
    cached = CACHE_DIR / f"{digest}.mp3"
    if cached.exists():
        return cached.read_bytes()

    r = requests.post(
        f"{API}/text-to-speech/{voice_id}",
        params={"output_format": "mp3_44100_128"},
        headers={"xi-api-key": _key(), "Content-Type": "application/json"},
        json={"text": text, "model_id": TTS_MODEL, "voice_settings": settings},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs TTS failed: HTTP {r.status_code} {r.text[:300]}")
    cached.write_bytes(r.content)
    return r.content


def stt(audio: bytes, filename: str = "reply.m4a") -> str:
    """Transcribe the driver's reply. Returns the text ('' if nothing was said)."""
    r = requests.post(
        f"{API}/speech-to-text",
        headers={"xi-api-key": _key()},
        files={"file": (filename, audio)},
        data={"model_id": STT_MODEL},
        timeout=12,
    )
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs STT failed: HTTP {r.status_code} {r.text[:300]}")
    return (r.json().get("text") or "").strip()


_HELP = re.compile(r"\b(help|hurt|injured|ambulance|call|911|no|not okay|not ok|bleeding|pain|ayuda|herido|no estoy bien)\b")
_OK = re.compile(r"\b(okay|ok|fine|good|alright|all right|yes|yeah|i'm okay|im okay|estoy bien|bien|sí|si)\b")


def classify_reply(text: str) -> str:
    """'help' wins over 'ok' so "I'm not okay" counts as help. Empty or unclear counts as no_response."""
    t = text.lower().strip()
    if not t:
        return "no_response"
    if _HELP.search(t):
        return "help"
    if _OK.search(t):
        return "ok"
    return "no_response"
