"""Voice-cache tests use disposable files and mocked provider audio."""
import sys
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voice


@pytest.fixture(autouse=True)
def cache(monkeypatch, tmp_path):
    monkeypatch.setattr(voice, 'CACHE_DIR', tmp_path)
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'offline-key')
    return tmp_path


def test_concurrent_phrase_requests_share_one_generation(monkeypatch, cache):
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Response:
        status_code = 200
        content = b'complete-mp3'

    def generate(*args, **kwargs):
        calls.append(kwargs['json']['text'])
        entered.set()
        assert release.wait(3)
        return Response()

    monkeypatch.setattr(voice.requests, 'post', generate)
    with ThreadPoolExecutor(max_workers=6) as pool:
        first = pool.submit(voice.tts, 'Brake now!')
        try:
            assert entered.wait(1)
            others = [pool.submit(voice.tts, 'Brake now!') for _ in range(5)]
            assert not list(cache.glob('*.mp3'))
        finally:
            release.set()
        assert first.result(timeout=2) == b'complete-mp3'
        assert all(item.result(timeout=2) == b'complete-mp3' for item in others)
    assert calls == ['Brake now!']
    assert len(list(cache.glob('*.mp3'))) == 1
    assert not list(cache.glob('*.tmp'))


def test_empty_provider_audio_is_never_cached(monkeypatch, cache):
    class Response:
        status_code = 200
        content = b''

    monkeypatch.setattr(voice.requests, 'post', lambda *args, **kwargs: Response())
    with pytest.raises(RuntimeError, match='empty audio'):
        voice.tts('Brake now!')
    assert not list(cache.iterdir())


def test_empty_cache_is_regenerated_and_existing_cache_works_offline(monkeypatch, cache):
    class Response:
        status_code = 200
        content = b'audio'

    monkeypatch.setattr(voice.requests, 'post', lambda *args, **kwargs: Response())
    assert voice.tts('Brake now!') == b'audio'
    cached = next(cache.glob('*.mp3'))
    cached.write_bytes(b'')
    assert voice.tts('Brake now!') == b'audio'
    monkeypatch.delenv('ELEVENLABS_API_KEY')
    def forbidden(*args, **kwargs):
        raise AssertionError('Cached playback must not call the provider')
    monkeypatch.setattr(voice.requests, 'post', forbidden)
    assert voice.tts('Brake now!') == b'audio'


def test_failed_cache_publication_leaves_no_partial_audio(monkeypatch, cache):
    class Response:
        status_code = 200
        content = b'audio'

    monkeypatch.setattr(voice.requests, 'post', lambda *args, **kwargs: Response())
    def unavailable(*args, **kwargs):
        raise PermissionError('Cache is unavailable')
    monkeypatch.setattr(Path, 'replace', unavailable)
    with pytest.raises(PermissionError):
        voice.tts('Brake now!')
    assert not list(cache.iterdir())
