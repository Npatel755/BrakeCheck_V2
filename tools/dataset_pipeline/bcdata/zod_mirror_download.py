"""Pinned, resumable acquisition of all public ZOD mirror assets."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time

import requests
from filelock import FileLock

from .bootstrap import atomic_json

REPO = "8bits-ai/ZOD-Mini-2D-Road-Scenes"
REVISION = "244c5a10859c6951830112fdb97072b83dddf92e"
FILES = {
    "annotations.tar.gz": (1459474554, "570129652a4079b8f4f569abc4997c560007b8eb8e8bb3cded4d96e932d4970c"),
    "1280x720_images.tar.gz": (21593491983, "66be4e0910f593931649b094d423e5271151aad882cbbd94f2a39e928ae0be1a"),
    "640x360_images.tar.gz": (6512809432, "2b94e8ca2826b18d041e6663d12cf5c5c90622cfa18e2d3950861055bc328a98"),
}


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(root):
    root = Path(root).resolve()
    upstream = root / "upstream"
    upstream.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    state = {"schema": "zod_mirror_download:v1", "pid": os.getpid(), "phase": "downloading",
             "repo": REPO, "revision": REVISION, "started_at": time.time(),
             "total_bytes": sum(value[0] for value in FILES.values()), "files": {},
             "auto_promote": False, "pool": "share_alike"}

    def update(name, **values):
        with lock:
            state["files"].setdefault(name, {}).update(values)
            state["downloaded_bytes"] = sum(item.get("bytes", 0) for item in state["files"].values())
            state["updated_at"] = time.time()
            atomic_json(root / "download_status.json", state)

    def acquire(name):
        size, expected = FILES[name]
        destination = upstream / name
        partial = destination.with_suffix(destination.suffix + ".part")
        if destination.exists():
            if destination.stat().st_size != size or digest(destination) != expected:
                raise ValueError("Existing ZOD archive failed integrity: " + name)
            update(name, bytes=size, total_bytes=size, phase="verified", sha256=expected)
            return
        for attempt in range(20):
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > size:
                raise ValueError("Oversized partial archive: " + name)
            if offset == size:
                break
            if shutil.disk_usage(root).free < size - offset + 20 * 2**30:
                raise OSError("Insufficient free space for ZOD transfer and 20 GiB reserve")
            update(name, bytes=offset, total_bytes=size, phase="downloading", attempt=attempt + 1)
            headers = {"Accept-Encoding": "identity", "User-Agent": "BrakeCheck-ZOD-Import/1.0"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            # A fresh resolve request obtains a new signed URL after a retry.
            url = f"https://huggingface.co/datasets/{REPO}/resolve/{REVISION}/{name}?download=true"
            try:
                with requests.get(url, headers=headers, stream=True, timeout=(30, 90)) as response:
                    response.raise_for_status()
                    if offset:
                        if response.status_code != 206 or response.headers.get("Content-Range") != f"bytes {offset}-{size-1}/{size}":
                            raise ValueError("Mirror did not honor exact resume range: " + name)
                    elif response.status_code not in (200, 206):
                        raise ValueError("Unexpected download response")
                    last = time.monotonic()
                    with partial.open("ab" if offset else "wb") as stream:
                        for chunk in response.iter_content(2 * 1024 * 1024):
                            if offset + len(chunk) > size:
                                raise ValueError("Archive exceeds pinned size: " + name)
                            stream.write(chunk)
                            offset += len(chunk)
                            if time.monotonic() - last >= 3:
                                stream.flush()
                                update(name, bytes=offset, total_bytes=size, phase="downloading")
                                last = time.monotonic()
                if offset != size:
                    raise requests.ConnectionError("Truncated archive")
                break
            except requests.RequestException as error:
                update(name, bytes=partial.stat().st_size if partial.exists() else 0,
                       phase="retrying", error_type=type(error).__name__)
                if attempt == 19:
                    raise RuntimeError("Archive transport exhausted retries: " + name) from None
                time.sleep(min(30, 2 ** min(attempt, 5)))
        update(name, bytes=size, total_bytes=size, phase="verifying")
        if digest(partial) != expected:
            raise ValueError("Archive SHA256 mismatch; partial preserved: " + name)
        os.replace(partial, destination)
        update(name, phase="verified", sha256=expected)

    with FileLock(str(root / "download.lock"), timeout=0):
        response = requests.get(f"https://huggingface.co/api/datasets/{REPO}/revision/{REVISION}",
                                params={"blobs": "true"}, timeout=30)
        response.raise_for_status()
        metadata = response.json()
        if metadata["sha"] != REVISION or metadata.get("gated") not in (False, None):
            raise ValueError("Unexpected mirror identity or access policy")
        entries = {item["rfilename"]: item for item in metadata["siblings"]}
        for name, (size, expected) in FILES.items():
            entry = entries[name]
            if entry.get("size") != size or entry.get("lfs", {}).get("sha256") != expected:
                raise ValueError("Pinned archive metadata changed: " + name)
            update(name, bytes=0, total_bytes=size, phase="pending")
        atomic_json(upstream / "repository.json", metadata)
        try:
            with ThreadPoolExecutor(max_workers=3) as workers:
                list(workers.map(acquire, FILES))
            # Preserve small repository assets as provenance, never execute them.
            for name, entry in entries.items():
                if name in FILES:
                    continue
                if Path(name).name != name or entry.get("size", 0) > 2 * 1024 * 1024:
                    raise ValueError("Unexpected extra repository asset")
                response = requests.get(f"https://huggingface.co/datasets/{REPO}/resolve/{REVISION}/{name}", timeout=30)
                response.raise_for_status()
                (upstream / name).write_bytes(response.content)
            state.update(phase="download_complete", completed_at=time.time(), updated_at=time.time())
            atomic_json(root / "download_status.json", state)
            atomic_json(upstream / "verified_archives.json", {"repo": REPO, "revision": REVISION,
                        "files": {name: {"bytes": size, "sha256": sha} for name, (size, sha) in FILES.items()}})
        except Exception as error:
            state.update(phase="download_error", updated_at=time.time(), error=str(error))
            atomic_json(root / "download_status.json", state)
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    download(parser.parse_args().output)
