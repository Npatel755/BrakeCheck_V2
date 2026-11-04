"""Resume the complete pinned Audi selection in cumulative CPU-only waves.

Every wave retains the same plan identity. An incomplete selection is retried
at the exact same limit; only a verified ready result advances the next wave.
Space waits are automatic and never manufacture a review decision.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import time

from filelock import FileLock

from .bootstrap import atomic_json
from .config import Settings
from .local import local_settings

SCHEMA = "a2d2_remaining_import:v1"
DERIVED_BYTES_PER_NEW_IMAGE = 550_000
NETWORK_ERRORS = {"ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout",
                  "ChunkedEncodingError", "ContentDecodingError"}


def _importer():
    # The GPU-capable environment supplies segmentation_models_pytorch, but
    # this worker must never initialize CUDA or compete with UFLDv2 training.
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    from . import import_audi
    return import_audi


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _wave_file(settings, importer, plan, limit):
    selection = importer.canonical_sha(plan["items"][:limit])
    run_id = plan["plan_sha256"][:12] + "-" + selection[:12]
    return settings.path("reports", "a2d2_import", run_id, "status.json"), selection


def _wave_status(settings, importer, plan, limit):
    path, selection = _wave_file(settings, importer, plan, limit)
    if not path.exists():
        return None
    result = _read(path)
    _verify_result(result, plan, selection, limit)
    return result


def _verify_result(result, plan, selection, limit):
    if (not isinstance(result, dict) or result.get("plan_sha256") != plan["plan_sha256"]
            or result.get("selection_sha256") != selection or result.get("total") != limit):
        raise ValueError("Audi wave status does not match the pinned plan and exact selection")


def _space_required(settings, importer, plan, completed, limit, path_cache):
    objects = {item[key + "_meta"]["key"]: item[key + "_meta"]
               for item in plan["items"][:limit]
               for key in ("img", "mask", "metadata", "palette", "calibration")
               if item.get(key + "_meta")}
    missing = 0
    for key, obj in objects.items():
        if key not in path_cache:
            path_cache[key] = importer.a2d2.safe_path(settings.path("raw", "a2d2"), key)
        path = path_cache[key]
        if not path.exists() or path.stat().st_size != int(obj["size"]):
            missing += int(obj["size"])
    reserve = int(float(settings.get("download.min_free_disk_gb", 20.)) * 1e9)
    derived = (limit - completed) * DERIVED_BYTES_PER_NEW_IMAGE
    return {"missing_raw_bytes": missing, "estimated_new_derived_bytes": derived,
            "reserve_bytes": reserve, "required_free_bytes": missing + derived + reserve}


def _retryable(result=None, error=None):
    if error is not None:
        return type(error).__name__ in NETWORK_ERRORS
    errors = (result or {}).get("errors", {})
    values = list(errors.values()) if isinstance(errors, dict) else errors
    return bool(values) and all(isinstance(item, dict) and item.get("error_type") in NETWORK_ERRORS for item in values)


def run(settings, plan_file, approved_gb=100., *, wave_size=4000, chunk_size=64,
        workers=4, wait_seconds=30., progress_file=None):
    if wave_size < 1 or chunk_size < 1 or not 1 <= workers <= 16 or wait_seconds <= 0:
        raise ValueError("Use positive wave/chunk/wait values and 1..16 workers")
    importer = _importer()
    plan_file = Path(plan_file).resolve()
    plan = importer.read_plan(plan_file)
    target = len(plan["items"])
    progress = Path(progress_file) if progress_file else settings.path("reports", "a2d2_remaining-20261018", "progress.json")
    lock = settings.path("logs", "a2d2-remaining-import.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    copied = deepcopy(settings.cfg)
    copied.setdefault("teachers", {}).update(device="cpu", amp=False)
    settings = Settings(copied, settings.config_path, settings.licenses_path)
    with FileLock(str(lock), timeout=0):
        if progress.exists():
            state = _read(progress)
            if (state.get("schema") != SCHEMA or state.get("plan_sha256") != plan["plan_sha256"]
                    or state.get("target") != target or state.get("wave_size") != wave_size):
                raise ValueError("Existing remaining-import progress belongs to a different plan or wave policy")
            completed, current = state.get("completed_limit"), state.get("current_limit")
            if (type(completed) is not int or type(current) is not int
                    or not 0 <= completed <= current <= target
                    or current > min(completed + wave_size, target)):
                raise ValueError("Invalid completed/current Audi wave limits in saved progress")
        else:
            state = {"schema": SCHEMA, "plan_path": str(plan_file), "plan_sha256": plan["plan_sha256"],
                     "items_sha256": plan["items_sha256"], "target": target,
                     "completed_limit": 0, "current_limit": min(wave_size, target),
                     "wave_size": wave_size, "started_at": time.time(), "rows": 0,
                     "published": 0, "audits": {}, "completed_waves": []}
        state.update(pid=os.getpid(), device="cpu", cuda_visible_devices="", approved_download_gb=approved_gb)

        def update(**changes):
            state.update(changes, updated_at=time.time())
            atomic_json(progress, state)

        path_cache = {}
        update(phase="checking_saved_wave")
        try:
            while state["completed_limit"] < target:
                if state["current_limit"] == state["completed_limit"]:
                    update(current_limit=min(target, state["completed_limit"] + wave_size))
                current = state["current_limit"]
                status_path, selection = _wave_file(settings, importer, plan, current)
                update(wave_status_path=str(status_path), selection_sha256=selection, phase="checking_saved_wave")
                saved = _wave_status(settings, importer, plan, current)
                attempts = 0
                result = saved if saved and saved.get("state") == "ready" and not saved.get("failed", 0) else None
                while result is None:
                    update(phase="checking_space")
                    space = _space_required(settings, importer, plan, state["completed_limit"], current, path_cache)
                    free = shutil.disk_usage(settings.root).free
                    if free < space["required_free_bytes"]:
                        update(phase="waiting_for_space", free_bytes=free, **space)
                        time.sleep(wait_seconds)
                        continue
                    update(phase="importing", free_bytes=free, **space, attempt=attempts + 1)
                    try:
                        candidate = importer.run(settings, plan_file, approved_gb,
                                                 chunk_size=chunk_size, workers=workers, limit=current)
                        _verify_result(candidate, plan, selection, current)
                    except Exception as error:
                        attempts += 1
                        if _retryable(error=error) and attempts <= 2:
                            update(phase="retrying_wave", error_type=type(error).__name__, error=str(error))
                            time.sleep(wait_seconds)
                            continue
                        update(phase="error", error_type=type(error).__name__, error=str(error), stopped_at=time.time())
                        return state
                    if candidate.get("state") == "ready" and not candidate.get("failed", 0):
                        result = candidate
                    else:
                        attempts += 1
                        if _retryable(result=candidate) and attempts <= 2:
                            update(phase="retrying_wave", last_wave_result=candidate)
                            time.sleep(wait_seconds)
                            continue
                        update(phase="partial_ready" if candidate.get("state") == "partial_ready" else "error",
                               last_wave_result=candidate, error="Wave did not finish ready; resume this exact selection",
                               stopped_at=time.time())
                        return state
                state["completed_waves"].append({"limit": current, "selection_sha256": selection,
                                                  "status_path": str(status_path), "published": result.get("published", 0),
                                                  "audit_counts": result.get("audit_counts", {})})
                update(completed_limit=current, rows=result.get("downloaded", 0),
                       published=result.get("published", 0), audits=result.get("audit_counts", {}),
                       last_wave_result=result, phase="wave_complete", error=None, error_type=None)
            update(phase="complete", current_limit=target, finished_at=time.time(),
                   meaning="Import complete; real human audit decisions are still required for source-cohort release.")
            return state
        except KeyboardInterrupt:
            update(phase="interrupted", stopped_at=time.time())
            raise
        except Exception as error:
            update(phase="error", error_type=type(error).__name__, error=str(error), stopped_at=time.time())
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-file", type=Path, required=True)
    parser.add_argument("--approve-download-gb", type=float, default=100.)
    parser.add_argument("--wave-size", type=int, default=4000)
    parser.add_argument("--chunk-size", type=int, default=64)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--wait-seconds", type=float, default=30.)
    parser.add_argument("--progress-file", type=Path)
    parser.add_argument("--data-root", type=Path)
    args = parser.parse_args()
    result = run(local_settings(args.data_root), args.plan_file, args.approve_download_gb,
                 wave_size=args.wave_size, chunk_size=args.chunk_size, workers=args.workers,
                 wait_seconds=args.wait_seconds, progress_file=args.progress_file)
    print(json.dumps(result))
    if result["phase"] != "complete":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
