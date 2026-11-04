"""Official snapshot transfer resumes safely and never publishes bad weights."""
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest


def prepare(monkeypatch, tmp_path, *, body=b"abcdefgh", status=206, filename="model.safetensors"):
    import huggingface_hub
    import huggingface_hub.utils
    import requests
    from bcdata import dinov3_download as download

    expected = b"abcdefgh"
    sibling = SimpleNamespace(rfilename=filename, size=len(expected),
                              lfs={"sha256": hashlib.sha256(expected).hexdigest()})
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda: SimpleNamespace(
        model_info=lambda *a, **k: SimpleNamespace(siblings=[sibling])))
    monkeypatch.setattr(huggingface_hub.utils, "build_hf_headers", lambda **k: {})
    target = tmp_path / ("models--" + download.MODEL.replace("/", "--")) / "snapshots" / download.REVISION
    target.mkdir(parents=True)
    temporary = target / "model.safetensors.incomplete"
    temporary.write_bytes(expected[:3])
    calls = []

    class Response:
        status_code = status
        headers = {"Content-Range": "bytes 3-7/8"}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, size):
            yield body[3:] if status == 206 else body

    def get(*args, **kwargs):
        calls.append(kwargs["headers"])
        return Response()

    monkeypatch.setattr(requests, "get", get)
    return download, target, temporary, calls


@pytest.mark.parametrize("status", [200, 206])
def test_transfer_resume_or_safe_restart(monkeypatch, tmp_path, status):
    download, target, temporary, calls = prepare(monkeypatch, tmp_path, status=status)
    assert download.download(tmp_path) == target
    assert (target / "model.safetensors").read_bytes() == b"abcdefgh"
    assert calls[0]["Range"] == "bytes=3-"
    assert not temporary.exists()
    assert (target / "verified_manifest.json").exists()


def test_bad_checksum_never_published(monkeypatch, tmp_path):
    download, target, temporary, _ = prepare(monkeypatch, tmp_path, body=b"XXXXXXXX", status=200)
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        download.download(tmp_path)
    assert not (target / "model.safetensors").exists()
    assert temporary.exists()
    assert not (target / "verified_manifest.json").exists()


def test_unexpected_snapshot_path_refused(monkeypatch, tmp_path):
    download, target, _, calls = prepare(monkeypatch, tmp_path, filename="../../bad.safetensors")
    with pytest.raises(ValueError, match="filename"):
        download.download(tmp_path)
    assert not calls
