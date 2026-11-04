"""Remaining-import orchestration uses disposable state and mocked workers."""
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace

from filelock import FileLock, Timeout
import pytest
from requests.exceptions import ReadTimeout

from bcdata import import_audi, import_audi_remaining as remaining


def fixture(settings, tmp_path, monkeypatch, count=9000, behavior=None):
    items = []
    for index in range(count):
        item = {"img": f"camera_lidar_semantic/fixture/image-{index}.png",
                "session": "a2d2:fixture", "split": "train"}
        for key in ("img", "mask", "metadata", "palette", "calibration"):
            item[key + "_meta"] = {"key": f"camera_lidar_semantic/fixture/{key}-{index}.png",
                                   "size": 10, "etag": "a" * 32}
        items.append(item)
    plan = {"schema": "a2d2_import_plan:v1", "source": "a2d2", "camera": "front_center",
            "base_url": import_audi.a2d2.BASE_URL, "selected": count, "items": items,
            "items_sha256": import_audi.canonical_sha(items)}
    plan["plan_sha256"] = import_audi.canonical_sha(plan)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan))
    calls = []

    def result(limit, state="ready", **extra):
        return {"plan_sha256": plan["plan_sha256"],
                "selection_sha256": import_audi.canonical_sha(plan["items"][:limit]),
                "total": limit, "state": state, "failed": 0, "downloaded": limit,
                "published": limit - 1, "audit_counts": {"sample": 3, "awaiting_audit": max(0, limit - 4)}, **extra}

    def worker(settings, path, approved_gb, **kwargs):
        assert settings.get("teachers.device") == "cpu" and settings.get("teachers.amp") is False
        assert approved_gb == 100. and Path(path) == plan_file
        limit = kwargs["limit"]
        calls.append(limit)
        return behavior(limit, len(calls), result) if behavior else result(limit)

    backend = SimpleNamespace(read_plan=import_audi.read_plan, canonical_sha=import_audi.canonical_sha,
                              a2d2=import_audi.a2d2, run=worker)
    monkeypatch.setattr(remaining, "_importer", lambda: backend)
    monkeypatch.setattr(remaining, "_space_required", lambda *args: {
        "missing_raw_bytes": 1_000_000_000, "estimated_new_derived_bytes": 2_200_000_000,
        "reserve_bytes": 20_000_000_000, "required_free_bytes": 23_200_000_000})
    monkeypatch.setattr(remaining.shutil, "disk_usage", lambda _: SimpleNamespace(free=50_000_000_000))
    progress = settings.path("reports", "a2d2_remaining-20261018", "progress.json")
    return plan_file, plan, backend, calls, progress, result


def saved_progress(progress, plan, completed=4000, current=8000, phase="importing"):
    value = {"schema": remaining.SCHEMA, "plan_sha256": plan["plan_sha256"],
             "items_sha256": plan["items_sha256"], "target": len(plan["items"]),
             "wave_size": 4000, "completed_limit": completed, "current_limit": current,
             "phase": phase, "completed_waves": [], "rows": 0, "published": 0, "audits": {}}
    progress.parent.mkdir(parents=True, exist_ok=True)
    progress.write_text(json.dumps(value))
    return value


def test_all_27448_items_run_as_cumulative_waves(settings, tmp_path, monkeypatch):
    path, plan, _, calls, progress, _ = fixture(settings, tmp_path, monkeypatch, count=27448)
    result = remaining.run(settings, path)
    assert calls == [4000, 8000, 12000, 16000, 20000, 24000, 27448]
    assert result["phase"] == "complete"
    assert result["completed_limit"] == result["current_limit"] == 27448
    assert result["plan_sha256"] == plan["plan_sha256"]
    assert len(result["completed_waves"]) == 7
    assert json.loads(progress.read_text())["audits"] == result["audits"]


def test_restart_resumes_exact_incomplete_selection(settings, tmp_path, monkeypatch):
    path, plan, _, calls, progress, _ = fixture(settings, tmp_path, monkeypatch)
    saved_progress(progress, plan, completed=4000, current=8000)
    result = remaining.run(settings, path)
    assert calls == [8000, 9000]
    assert result["phase"] == "complete"


def test_sealed_ready_wave_recovers_without_import_or_space_wait(settings, tmp_path, monkeypatch):
    path, plan, backend, calls, progress, status = fixture(settings, tmp_path, monkeypatch, count=8000)
    saved_progress(progress, plan, completed=4000, current=8000)
    wave_path, _ = remaining._wave_file(settings, backend, plan, 8000)
    wave_path.parent.mkdir(parents=True)
    wave_path.write_text(json.dumps(status(8000)))
    monkeypatch.setattr(remaining, "_space_required", lambda *args: pytest.fail("finished wave must not wait for space"))
    result = remaining.run(settings, path)
    assert calls == [] and result["completed_limit"] == 8000 and result["phase"] == "complete"


def test_insufficient_space_waits_without_starting_import(settings, tmp_path, monkeypatch):
    path, _, _, calls, progress, _ = fixture(settings, tmp_path, monkeypatch, count=4000)
    usage = iter([SimpleNamespace(free=22_000_000_000), SimpleNamespace(free=30_000_000_000)])
    monkeypatch.setattr(remaining.shutil, "disk_usage", lambda _: next(usage))
    sleeps = []
    def sleep(seconds):
        assert calls == []
        saved = json.loads(progress.read_text())
        assert saved["phase"] == "waiting_for_space"
        assert saved["completed_limit"] == 0 and saved["current_limit"] == 4000
        sleeps.append(seconds)
    monkeypatch.setattr(remaining.time, "sleep", sleep)
    result = remaining.run(settings, path)
    assert sleeps == [30.] and calls == [4000] and result["phase"] == "complete"


def test_partial_failure_does_not_advance_and_restart_retries_same_limit(settings, tmp_path, monkeypatch):
    def behavior(limit, attempt, status):
        return status(limit, "partial_ready", failed=1, errors={"image": {"error_type": "RuntimeError", "error": "fixture model failure"}})
    path, plan, backend, calls, progress, status = fixture(settings, tmp_path, monkeypatch, behavior=behavior)
    result = remaining.run(settings, path)
    assert calls == [4000] and result["phase"] == "partial_ready"
    assert result["completed_limit"] == 0 and result["current_limit"] == 4000
    backend.run = lambda settings, path, approved_gb, **kwargs: (calls.append(kwargs["limit"]) or status(kwargs["limit"]))
    resumed = remaining.run(settings, path)
    assert calls == [4000, 4000, 8000, 9000]
    assert resumed["phase"] == "complete"


def test_network_retries_twice_at_same_limit(settings, tmp_path, monkeypatch):
    def behavior(limit, attempt, status):
        if attempt < 3:
            raise ReadTimeout("temporary fixture timeout")
        return status(limit)
    path, _, _, calls, _, _ = fixture(settings, tmp_path, monkeypatch, count=4000, behavior=behavior)
    waits = []
    monkeypatch.setattr(remaining.time, "sleep", waits.append)
    result = remaining.run(settings, path)
    assert calls == [4000, 4000, 4000] and waits == [30., 30.]
    assert result["phase"] == "complete"


def test_failed_retries_stop_instead_of_skipping_wave(settings, tmp_path, monkeypatch):
    def behavior(limit, attempt, status):
        raise ReadTimeout("persistent fixture timeout")
    path, _, _, calls, _, _ = fixture(settings, tmp_path, monkeypatch, behavior=behavior)
    monkeypatch.setattr(remaining.time, "sleep", lambda _: None)
    result = remaining.run(settings, path)
    assert calls == [4000, 4000, 4000]
    assert result["phase"] == "error" and result["completed_limit"] == 0 and result["current_limit"] == 4000


def test_different_active_plan_is_rejected_without_overwriting_progress(settings, tmp_path, monkeypatch):
    path, plan, _, calls, progress, _ = fixture(settings, tmp_path, monkeypatch)
    previous = saved_progress(progress, plan)
    previous["plan_sha256"] = "a" * 64
    progress.write_text(json.dumps(previous))
    before = progress.read_bytes()
    with pytest.raises(ValueError, match="different plan"):
        remaining.run(settings, path)
    assert calls == [] and progress.read_bytes() == before


def test_lifetime_lock_prevents_parallel_remaining_workers(settings, tmp_path, monkeypatch):
    path, _, _, calls, _, _ = fixture(settings, tmp_path, monkeypatch)
    lock = settings.path("logs", "a2d2-remaining-import.lock")
    with FileLock(str(lock), timeout=0):
        with pytest.raises(Timeout):
            remaining.run(settings, path)
    assert calls == []


def test_cpu_import_disables_cuda_before_loading_worker(monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    assert remaining._importer() is import_audi
    assert os.environ["CUDA_VISIBLE_DEVICES"] == ""


def test_raw_and_derived_space_budget_uses_missing_files_only(settings, tmp_path):
    items=[]
    for index in range(3):
        items.append({"img_meta": {"key": f"camera_lidar_semantic/fixture/{index}.png", "size": 10}})
    existing = import_audi.a2d2.safe_path(settings.path("raw", "a2d2"), items[0]["img_meta"]["key"])
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"0123456789")
    required = remaining._space_required(settings, import_audi, {"items": items}, 1, 3, {})
    assert required["missing_raw_bytes"] == 20
    assert required["estimated_new_derived_bytes"] == 2 * remaining.DERIVED_BYTES_PER_NEW_IMAGE
    assert required["required_free_bytes"] == 20_000_000_000 + 20 + 2 * remaining.DERIVED_BYTES_PER_NEW_IMAGE
