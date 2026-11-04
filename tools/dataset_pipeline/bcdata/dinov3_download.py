"""Download the pinned official DINOv3 snapshot with resumable HTTP and hashes."""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
from pathlib import Path
import shutil
import time

from .bootstrap import atomic_json

MODEL = "facebook/dinov3-vit7b16-pretrain-lvd1689m"
REVISION = "b80367753773648a6793235ab9c65cdbb029506f"


def download(cache_dir=None, progress=None):
    import requests
    from filelock import FileLock
    from huggingface_hub import HfApi, hf_hub_url
    from huggingface_hub.constants import HF_HUB_CACHE
    from huggingface_hub.utils import build_hf_headers

    root = Path(cache_dir or HF_HUB_CACHE).resolve()
    target = root / ("models--" + MODEL.replace("/", "--")) / "snapshots" / REVISION
    target.mkdir(parents=True, exist_ok=True)
    manifest = []
    with FileLock(str(target / "download.lock"), timeout=0):
        files = [f for f in HfApi().model_info(MODEL, revision=REVISION, files_metadata=True).siblings
                 if f.rfilename.endswith(".safetensors") or f.rfilename in
                 ("config.json", "model.safetensors.index.json", "preprocessor_config.json")]
        files.sort(key=lambda f: f.rfilename)
        for item in files:
            path = target / item.rfilename
            if Path(item.rfilename).name != item.rfilename or not path.resolve().is_relative_to(root):
                raise ValueError("Unexpected model filename")
            expected = (item.lfs.get("sha256") if isinstance(item.lfs, dict)
                        else getattr(item.lfs, "sha256", None)) if item.lfs else None
            if item.rfilename.endswith(".safetensors") and not expected:
                raise RuntimeError("Official weight checksum metadata is missing")
            def digest(file):
                with file.open("rb") as stream:
                    return hashlib.file_digest(stream, "sha256").hexdigest()
            if path.exists() and path.stat().st_size == item.size:
                actual = digest(path)
                if not expected or actual == expected:
                    manifest.append({"name": item.rfilename, "bytes": item.size, "sha256": actual})
                    continue
            temporary = path.with_name(path.name + ".incomplete")
            for attempt in range(6):
                offset = temporary.stat().st_size if temporary.exists() else 0
                if offset > item.size:
                    raise RuntimeError("Oversized partial model file; inspect before retrying")
                if shutil.disk_usage(target).free < item.size - offset + 8 * 2**30:
                    raise RuntimeError("Free more disk space for model download plus 8 GiB reserve")
                if offset == item.size:
                    break
                headers = build_hf_headers(token=True)
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                if progress:
                    progress({"phase": "downloading", "file": item.rfilename,
                              "file_bytes": offset, "file_total_bytes": item.size})
                try:
                    with requests.get(hf_hub_url(MODEL, item.rfilename, revision=REVISION),
                                      headers=headers, stream=True, timeout=(30, 90)) as response:
                        response.raise_for_status()
                        if offset and response.status_code == 200:
                            offset = 0
                        elif offset and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                            raise RuntimeError("Unexpected download resume response")
                        last = time.monotonic()
                        with temporary.open("ab" if offset else "wb") as output:
                            for chunk in response.iter_content(1024 * 1024):
                                output.write(chunk)
                                offset += len(chunk)
                                if offset > item.size:
                                    raise RuntimeError("Model download exceeded expected size")
                                if progress and time.monotonic() - last > 5:
                                    progress({"phase": "downloading", "file": item.rfilename,
                                              "file_bytes": offset, "file_total_bytes": item.size})
                                    last = time.monotonic()
                    if temporary.stat().st_size == item.size:
                        break
                except requests.RequestException as error:
                    if attempt == 5:
                        # Do not log signed download URLs or credential-bearing exceptions.
                        raise RuntimeError("Model transfer failed: " + type(error).__name__) from None
                    time.sleep(min(10, 2 ** attempt))
            if not temporary.exists() or temporary.stat().st_size != item.size:
                raise RuntimeError("Incomplete model download; rerun to resume")
            actual = digest(temporary)
            if expected and actual != expected:
                raise RuntimeError("Model checksum mismatch; partial file retained for inspection")
            os.replace(temporary, path)
            manifest.append({"name": item.rfilename, "bytes": item.size, "sha256": actual})
            if progress:
                progress({"phase": "downloading", "file": item.rfilename,
                          "file_bytes": item.size, "file_total_bytes": item.size,
                          "completed_files": len(manifest), "total_files": len(files)})
        atomic_json(target / "verified_manifest.json", {"model": MODEL, "revision": REVISION, "files": manifest})
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    logging.getLogger("httpx").setLevel(logging.ERROR)
    state = {"model": MODEL, "revision": REVISION, "pid": os.getpid()}
    def progress(update):
        state.update(update, updated_at=time.time())
        atomic_json(args.report, state)
    try:
        path = download(args.cache_dir, progress)
        progress({"phase": "download_complete", "snapshot": str(path)})
        print("Verified DINOv3 snapshot:", path)
    except Exception as error:
        progress({"phase": "download_error", "error_type": type(error).__name__})
        raise SystemExit(type(error).__name__ + ": inspect the download report; no credentials logged") from None


if __name__ == "__main__":
    main()
