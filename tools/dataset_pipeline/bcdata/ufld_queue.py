"""Run a frozen UFLDv2 benchmark after a successful predecessor, without overlap.

Requests are immutable. Their status and the latest queue report are separate
from the active trainer's ufld_training.json report. No review decisions or
dataset exports are changed by this worker.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from filelock import FileLock, Timeout

from .bootstrap import atomic_json
from .local import local_settings
from .teachers.base import file_sha256

CODE_ROOT = Path(__file__).resolve().parents[1]
ACTIVE = {"queued", "waiting", "running"}


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _python(environment):
    root = CODE_ROOT / environment
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _spawn(command, settings, log):
    env = {**os.environ, "BRAKECHECK_DATA_ROOT": str(settings.root), "PYTHONUNBUFFERED": "1"}
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab", buffering=0) as stream:
        return subprocess.Popen(command, cwd=CODE_ROOT, env=env, stdin=subprocess.DEVNULL,
                                stdout=stream, stderr=subprocess.STDOUT, close_fds=True,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def _write_status(settings, request, **changes):
    path = Path(request["request_path"]).with_name("status.json")
    state = {**_read(path), **request, **changes, "updated_at": time.time()}
    atomic_json(path, state)
    # Never let a historical worker replace the latest request's report.
    latest = _read(settings.path("reports", "ufld_queue.json"))
    if not latest or latest.get("request_id") == request["request_id"]:
        atomic_json(settings.path("reports", "ufld_queue.json"), state)
    return state


def _prepared_metadata(run):
    dataset = run / "dataset"
    hashes = _read(dataset / "hashes.json")
    records = _read(dataset / "manifest.json")
    if not hashes or not isinstance(records, list) or not records:
        raise ValueError("Prepared run must contain a frozen dataset and hashes")
    if "manifest.json" not in hashes or file_sha256(dataset / "manifest.json") != hashes["manifest.json"]:
        raise ValueError("Prepared dataset manifest does not match its frozen hash")
    splits = Counter(record["split"] for record in records)
    if set(splits) != {"train", "val"}:
        raise ValueError("Prepared benchmark must contain train and validation data only")
    if (run / "status.json").exists() or (run / "best.pt").exists() or (run / "last.pt").exists():
        raise ValueError("Prepared run has already been started")
    return {"train_images": splits["train"], "val_images": splits["val"],
            "frozen_hashes_sha256": file_sha256(dataset / "hashes.json"),
            "manifest_sha256": hashes["manifest.json"]}


def enqueue(settings, after_run: Path, prepared_run: Path, epochs=20, launch=True, *, warm_start=False):
    """Persist one request and optionally start a hidden, CPU-only waiting worker.

    Repeating the same request returns its existing ID. A different active
    request is rejected instead of silently scheduling another GPU run. With
    warm_start=True, the completed predecessor's verified best weights initialize
    the new dataset; the trainer starts a new optimizer and schedule.
    """
    after_run, prepared_run = Path(after_run).resolve(), Path(prepared_run).resolve()
    if not isinstance(warm_start, bool):
        raise ValueError("Warm-start mode must be a boolean")
    if after_run == prepared_run or epochs < 1:
        raise ValueError("Use a different prepared output and a positive epoch count")
    if not (after_run / "status.json").is_file():
        raise ValueError("Predecessor run status is missing")
    queue_root = settings.path("reports", "ufld_queue")
    queue_root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(queue_root / "enqueue.lock"), timeout=30):
        for path in sorted(queue_root.glob("*/request.json")):
            previous = _read(path)
            status = _read(path.with_name("status.json"))
            same = (previous.get("after_run") == str(after_run) and
                    previous.get("prepared_run") == str(prepared_run) and previous.get("epochs") == epochs and
                    previous.get("warm_start", False) == warm_start)
            if same:
                if launch and status.get("state") in ACTIVE:
                    _start_worker(settings, path)
                return status or previous
            if status.get("state") in ACTIVE:
                raise ValueError("Another UFLDv2 benchmark is already queued or running")
        metadata = _prepared_metadata(prepared_run)
        request_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        request_path = queue_root / request_id / "request.json"
        request = {"request_id": request_id, "request_path": str(request_path), "after_run": str(after_run),
                   "prepared_run": str(prepared_run), "epochs": epochs, "warm_start": warm_start,
                   "created_at": time.time(), **metadata}
        atomic_json(request_path, request)
        state = _write_status(settings, request, state="waiting", reason="Waiting for predecessor benchmark to finish")
        # A new request supersedes the previous completed queue report. Later
        # updates remain request-bound so old workers cannot overwrite it.
        atomic_json(settings.path("reports", "ufld_queue.json"), state)
        if launch:
            _start_worker(settings, request_path)
        return state


def _start_worker(settings, request_path):
    # Each worker acquires a lifetime file lock. A repeated enqueue can safely
    # restart a dead worker; a second live process immediately exits unchanged.
    return _spawn([str(_python(".venv")), "-m", "bcdata.ufld_queue", "--request", str(request_path)],
                  settings, request_path.with_name("worker.log"))


def completed_run(run):
    """Return completion evidence only after a finished, verified checkpoint."""
    state = _read(run / "status.json")
    if state.get("phase") == "error":
        raise RuntimeError("Training failed: " + state.get("error_reason", str(run)))
    if state.get("phase") != "trained_awaiting_review":
        return None
    if int(state.get("epoch", 0)) < int(state.get("epochs", 1)):
        raise ValueError("Completed training report does not contain all requested epochs")
    checkpoint = Path(state.get("checkpoint", "")).resolve()
    if not checkpoint.is_relative_to(run.resolve()) or not checkpoint.is_file() or not checkpoint.stat().st_size:
        raise ValueError("Completed run checkpoint is missing or outside its run directory")
    expected = state.get("checkpoint_sha256")
    if not expected or file_sha256(checkpoint) != expected:
        raise ValueError("Completed run checkpoint hash does not match its report")
    return state


def gpu_available(settings):
    try:
        with FileLock(str(settings.path("logs", "large-models.lock")), timeout=0):
            return True
    except Timeout:
        return False


def _process_matches(pid, prepared_run):
    """Check identity before monitoring an orphan; never terminate a process."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        if os.name == "nt":
            command = subprocess.check_output(
                ["powershell.exe", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine"],
                text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW).strip()
            return "bcdata.ufld_baseline" in command and str(prepared_run).lower() in command.lower()
        values = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
        return "bcdata.ufld_baseline" in values and str(prepared_run) in values
    except (OSError, subprocess.SubprocessError):
        return False


def _launch_training(settings, request):
    command = [str(_python(".gpu-venv")), "-m", "bcdata.ufld_baseline", "--prepared-run", request["prepared_run"],
               "--epochs", str(request["epochs"]), "--gpu-lock-timeout", "-1"]
    if request.get("warm_start", False):
        # This runtime field is bound only after completed_run has verified the
        # predecessor. It is deliberately absent from the immutable request.
        command.extend(["--warm-start", request["warm_start_checkpoint"]])
    return _spawn(command, settings, Path(request["request_path"]).with_name("training.log"))


def worker(settings, request_path, poll_seconds=15, *, launch_training=None, sleep=time.sleep):
    """Wait without GPU allocation, then monitor one frozen-data run."""
    request_path = Path(request_path).resolve()
    request = _read(request_path)
    if not request or request.get("request_path") != str(request_path):
        raise ValueError("Invalid queue request")
    if poll_seconds <= 0:
        raise ValueError("Polling interval must be positive")
    lock = FileLock(str(request_path.with_name("worker.lock")), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        return _read(request_path.with_name("status.json"))
    try:
        status = _read(request_path.with_name("status.json"))
        if status.get("state") in {"completed", "error"}:
            return status
        _write_status(settings, request, worker_pid=os.getpid())
        prepared = Path(request["prepared_run"])
        launch_marker = request_path.with_name("launch.json")
        child = None
        predecessor = None
        while True:
            if launch_marker.exists():
                result = completed_run(prepared)
                if result:
                    # Completion status is written before the training process
                    # releases its GPU lock. Waiting for child exit avoids
                    # calling the queue complete while it still owns the GPU.
                    if child is not None and child.poll() is None:
                        sleep(poll_seconds)
                        continue
                    if child is not None and child.poll() != 0:
                        raise RuntimeError(f"Queued training exited with code {child.poll()} despite a completion report")
                    return _write_status(settings, request, state="completed", reason="Benchmark completed; awaiting human review",
                                         completed_at=time.time(), training=result)
                marker = _read(launch_marker)
                pid = child.pid if child is not None else marker.get("child_pid")
                if child is not None:
                    code = child.poll()
                    if code is not None:
                        raise RuntimeError(f"Queued training exited with code {code} before successful completion")
                elif not _process_matches(pid, prepared):
                    raise RuntimeError("Queued training was interrupted; its frozen dataset and checkpoints are preserved")
                _write_status(settings, request, state="running", child_pid=pid,
                              training=_read(prepared / "status.json"), reason="Training the frozen benchmark snapshot")
                sleep(poll_seconds)
                continue
            if predecessor is None:
                predecessor = completed_run(Path(request["after_run"]))
            if predecessor is None or not gpu_available(settings):
                reason = "Waiting for predecessor benchmark to finish" if predecessor is None else "Waiting for the GPU model lock"
                _write_status(settings, request, state="waiting", reason=reason)
                sleep(poll_seconds)
                continue
            if file_sha256(prepared / "dataset" / "hashes.json") != request["frozen_hashes_sha256"]:
                raise ValueError("Prepared dataset hash inventory changed while queued")
            # Completion may have been cached while another model held the GPU.
            # Bind launch provenance to evidence verified again at launch time.
            predecessor = completed_run(Path(request["after_run"]))
            if predecessor is None:
                _write_status(settings, request, state="waiting", reason="Waiting for predecessor benchmark to finish")
                sleep(poll_seconds)
                continue
            runtime_request = dict(request)
            provenance = {"predecessor_checkpoint_sha256": predecessor["checkpoint_sha256"]}
            if request.get("warm_start", False):
                provenance.update(warm_start_checkpoint=str(Path(predecessor["checkpoint"]).resolve()),
                                  warm_start_checkpoint_sha256=predecessor["checkpoint_sha256"],
                                  warm_start_source_run=str(Path(request["after_run"]).resolve()),
                                  warm_start_source_epoch=predecessor["epoch"],
                                  optimizer_restored=False)
                runtime_request.update(provenance)
            # Write intent before spawning. A worker crash never causes a
            # second trainer to be silently started for the same request.
            marker = {"started_at": time.time(), "prepared_run": str(prepared),
                      "warm_start": request.get("warm_start", False), **provenance}
            atomic_json(launch_marker, marker)
            child = (launch_training or _launch_training)(settings, runtime_request)
            atomic_json(launch_marker, {**marker, "child_pid": child.pid})
            _write_status(settings, request, state="running", child_pid=child.pid, started_at=time.time(),
                          **provenance, reason="Training the frozen benchmark snapshot")
    except Exception as error:
        _write_status(settings, request, state="error", error_type=type(error).__name__, reason=str(error)[:700])
        raise
    finally:
        lock.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=float, default=15)
    args = parser.parse_args()
    result = worker(local_settings(), args.request, args.poll_seconds)
    print(json.dumps({"request_id": result["request_id"], "state": result["state"]}))


if __name__ == "__main__":
    main()
