import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bcdata import overnight_collections as queue


def write(path, value):
    queue.atomic_json(path, value)


def fixture(tmp_path):
    base = tmp_path / "models/ufld_baseline/base"
    base.mkdir(parents=True)
    (base / "best.pt").write_bytes(b"trusted local checkpoint fixture")
    write(base / "dataset/manifest.json", [{"image_id": "base-train", "split": "train"},
                                          {"image_id": "base-val", "split": "val"}])
    source = tmp_path / "experiments/flagged_teacher/existing"
    write(source / "experiment.json", {"schema": "flagged_teacher_trial:v1", "auto_promote": False,
          "base_run": str(base), "base_manifest_sha256": queue.file_sha256(base / "dataset/manifest.json"),
          "inventory_sha256": "not-read-large-original-inventory", "phase": "training_teacher"})
    sim = tmp_path / "experiments/simulated_tulane"
    write(sim / "plan.json", {"source": "carlane_tulane", "dataset_version": 2, "pilot": False})
    write(sim / "import.json", {"state": "downloading", "converted": 100, "total_expected": 26400})
    fake_python = tmp_path / "python.exe"
    fake_python.write_bytes(b"fixture is never executed")
    state = queue.initialize_queue(tmp_path, source)
    return state, source, sim, fake_python


def finish_import(sim):
    (sim / "manifest.jsonl").write_bytes(b"{\"split\":\"train\",\"synthetic\":true}\n")
    write(sim / "audits.json", {"groups": [{"group_key": "pending", "sample_ids": ["sim"]}]})
    write(sim / "import.json", {"schema": "tulane_simulation_import:v1", "source": "carlane_tulane", "domain": "synthetic",
          "state": "awaiting_audit", "converted": 26400, "total_expected": 26400,
          "manifest_sha256": queue.file_sha256(sim / "manifest.jsonl"), "audits_sha256": queue.file_sha256(sim / "audits.json")})


def test_initialization_pins_common_base_and_never_opens_original_registry(tmp_path):
    production = tmp_path / "registry.sqlite"
    production.write_bytes(b"unchanged production sentinel")
    state, source, sim, _ = fixture(tmp_path)
    assert state["schema"] == "overnight_collection_queue:v1"
    assert state["unaudited_simulation"] is True and state["auto_promote"] is False
    assert state["approved_collections_unchanged"] is True
    assert [job["collection_id"] for job in state["jobs"]] == [row[1] for row in queue.COLLECTIONS]
    assert state["jobs"][0]["output_run"] == str(source / "ufld_run")
    assert all(job["epochs"] == 20 for job in state["jobs"])
    assert production.read_bytes() == b"unchanged production sentinel"
    assert not (source / "inventory.json").exists()  # 2.6 GB inventory is never needed here.


def test_waits_for_import_without_launching_models(tmp_path):
    state, _, _, python = fixture(tmp_path)
    def forbid(*args):
        raise AssertionError("No launch or build while import is incomplete")
    result = queue.run_queue(state, python=python, launcher=forbid, builder=forbid, loader=forbid, max_ticks=1)
    assert result["phase"] == "waiting_import"
    assert result["jobs"][0]["state"] == "waiting_import"
    assert queue.read(Path(state["output"]).parent / "active.json")["import_progress"]["converted"] == 100


def test_simulation_permission_ignores_pending_human_audits_but_requires_exact_hashes(tmp_path):
    state, _, sim, _ = fixture(tmp_path)
    finish_import(sim)
    assert queue.simulation_ready(state)
    assert state["simulation_manifest_sha256"] == queue.file_sha256(sim / "manifest.jsonl")
    (sim / "manifest.jsonl").write_bytes(b"changed")
    with pytest.raises(ValueError, match="SHA256"):
        queue.simulation_ready(state)


def test_live_existing_pipeline_is_never_restarted(tmp_path, monkeypatch):
    state, _, sim, python = fixture(tmp_path)
    finish_import(sim)
    monkeypatch.setattr(queue, "lock_held", lambda path: True)
    monkeypatch.setattr(queue, "matching_processes", lambda module, output: [])
    def forbid(*args):
        raise AssertionError("Existing live worker must not restart")
    queue.run_queue(state, python=python, launcher=forbid, max_ticks=1)
    assert state["jobs"][0]["state"] == "waiting_proposals"
    assert state["jobs"][0]["restarts"] == 0


def test_dead_existing_pipeline_resumes_at_most_twice(tmp_path, monkeypatch):
    state, _, sim, python = fixture(tmp_path)
    finish_import(sim)
    monkeypatch.setattr(queue, "lock_held", lambda path: False)
    monkeypatch.setattr(queue, "matching_processes", lambda module, output: [])
    calls = []
    def launcher(command, prefix, data):
        calls.append(command)
        return SimpleNamespace(pid=123)
    for _ in range(2):
        queue.run_queue(state, python=python, launcher=launcher, max_ticks=1)
    assert len(calls) == 2
    assert all("bcdata.flagged_model_trial" in call and "--resume" in call for call in calls)
    with pytest.raises(RuntimeError, match="two resume attempts"):
        queue.run_queue(state, python=python, launcher=launcher, max_ticks=1)
    assert state["state"] == "error" and state["jobs"][0]["state"] == "error"


def test_completed_collection_two_is_reused_then_three_and_four_run_in_order(tmp_path, monkeypatch):
    state, source, sim, python = fixture(tmp_path)
    finish_import(sim)
    write(source / "ufld_run/status.json", {"phase": "trained_awaiting_review", "best_val_f1": .73, "train_images": 4})
    write(source / "ufld_run/dataset/split_inventory.json", [{"image_id": "reserved", "split": "test"}])
    monkeypatch.setattr(queue, "lock_held", lambda path: False)
    monkeypatch.setattr(queue, "matching_processes", lambda module, output: [])
    monkeypatch.setattr(queue, "validate_collection_two", lambda state, job: None)
    calls, builds = [], []
    def builder(base, output, **options):
        assert (options["model_run"] is not None) == (options["collection_id"] == "converted_plus_proposals_plus_simulated")
        builds.append((output, options))
        write(output / "dataset/experiment.json", {"train_images": 5, "extra_train_images": 4})
        return {"train_images": 5, "extra_train_images": 4}
    def launcher(command, prefix, data):
        calls.append(command)
        run = Path(command[command.index("--prepared-run") + 1])
        write(run / "status.json", {"phase": "trained_awaiting_review", "best_val_f1": .74, "train_images": 5})
        return SimpleNamespace(pid=123)
    queue.run_queue(state, python=python, builder=builder, loader=lambda path: None,
                    launcher=launcher, sleep=lambda seconds: None)
    assert state["state"] == "complete"
    assert len(calls) == 2 and len(builds) == 2
    assert [options["collection_id"] for _, options in builds] == [queue.COLLECTIONS[1][1], queue.COLLECTIONS[2][1]]
    assert [options["model_run"] for _, options in builds] == [None, str(source / "ufld_run")]
    assert all(options["sim_root"] == str(sim) for _, options in builds)
    assert all("--warm-start" in command and "--evaluate-initial" in command for command in calls)
    assert all(command[command.index("--warm-start") + 1] == state["base_checkpoint"] for command in calls)
    assert all("reports/ufld_training.json" not in " ".join(command) for command in calls)
    report = queue.read(Path(state["output"]) / "comparison.json")
    assert report["test_images_used_for_training"] == 0
    assert len(report["jobs"]) == 3 and report["auto_promote"] is False
    assert "matched-budget" in report["limitation"]


def test_training_resume_uses_last_checkpoint_without_reinitializing(tmp_path):
    state, _, _, python = fixture(tmp_path)
    job = state["jobs"][1]
    run = Path(job["output_run"])
    run.mkdir()
    (run / "last.pt").write_bytes(b"fixture")
    command = queue.training_command(state, job, python)
    assert "--resume-run" in command
    assert "--warm-start" not in command and "--evaluate-initial" not in command
    assert command[command.index("--learning-rate") + 1] == "0.000625"
    assert command[command.index("--gpu-lock-timeout") + 1] == "-1"


def test_user_stop_records_unfinished_collection_two_and_preserves_checkpoint(tmp_path, monkeypatch):
    state, source, sim, python = fixture(tmp_path)
    finish_import(sim)
    run = source / "ufld_run"
    write(run / "dataset/experiment.json", {"train_images": 5})
    write(run / "dataset/split_inventory.json", [])
    write(run / "status.json", {"phase": "training", "epoch": 11, "best_val_f1": .72})
    (run / "last.pt").write_bytes(b"unfinished checkpoint")
    monkeypatch.setattr(queue, "lock_held", lambda path: False)
    monkeypatch.setattr(queue, "matching_processes", lambda module, output: [])
    result = queue.stop_collection_two(state)
    assert result["jobs"][0]["state"] == "stopped"
    assert result["jobs"][0]["completed_20_epochs"] is False
    assert result["jobs"][0]["progress"]["epoch"] == 11
    assert (run / "last.pt").read_bytes() == b"unfinished checkpoint"
    assert queue.read(Path(state["output"]) / "queue.json")["current_job"] == "collection_3"
    calls = []
    def builder(base, output, **options):
        calls.append(options["collection_id"])
        write(output / "dataset/experiment.json", {"train_images": 5})
        return {"train_images": 5}
    def launcher(command, prefix, data):
        calls.append("launched")
        return SimpleNamespace(pid=123)
    queue.run_queue(state, python=python, builder=builder, loader=lambda path: None,
                    launcher=launcher, max_ticks=1)
    assert calls == ["converted_plus_simulated", "launched"]
    assert state["jobs"][0]["state"] == "stopped"


def test_user_stop_refuses_live_collection_two_processes(tmp_path, monkeypatch):
    state, source, _, _ = fixture(tmp_path)
    run = source / "ufld_run"
    write(run / "dataset/experiment.json", {})
    write(run / "dataset/split_inventory.json", [])
    monkeypatch.setattr(queue, "lock_held", lambda path: False)
    monkeypatch.setattr(queue, "matching_processes", lambda module, output: [1] if module == "bcdata.ufld_baseline" else [])
    with pytest.raises(RuntimeError, match="processes must exit"):
        queue.stop_collection_two(state)
    assert state["jobs"][0]["state"] == "waiting_import"


def test_pinned_base_checkpoint_or_output_tampering_is_rejected(tmp_path):
    state, source, _, _ = fixture(tmp_path)
    state["jobs"][1]["output_run"] = str(tmp_path / "outside")
    with pytest.raises(ValueError, match="job path"):
        queue.validate_queue(state, tmp_path, source)
    state["jobs"][1]["output_run"] = str(Path(state["output"]) / "collection_3")
    Path(state["base_checkpoint"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="initialization"):
        queue.validate_queue(state, tmp_path, source)


def test_low_disk_waits_without_deleting_or_building(tmp_path, monkeypatch):
    state, source, sim, python = fixture(tmp_path)
    finish_import(sim)
    state["jobs"][0]["state"] = "complete"
    sentinel = Path(state["output"]) / "preserve.txt"
    sentinel.write_text("keep")
    monkeypatch.setattr(queue.shutil, "disk_usage", lambda path: SimpleNamespace(free=1024))
    monkeypatch.setattr(queue, "matching_processes", lambda module, output: [])
    def forbid(*args, **kwargs):
        raise AssertionError("Low disk must wait before snapshots/training")
    queue.run_queue(state, python=python, builder=forbid, loader=forbid, launcher=forbid, max_ticks=1)
    assert state["jobs"][1]["state"] == "waiting_space"
    assert sentinel.read_text() == "keep"


def test_process_matching_requires_module_and_exact_run_path(tmp_path, monkeypatch):
    output = tmp_path / "run"
    processes = [SimpleNamespace(info={"pid": 10, "create_time": 1,
        "cmdline": ["python", "-m", "bcdata.ufld_baseline", "--resume-run", str(output)]}),
        SimpleNamespace(info={"pid": 11, "create_time": 2,
        "cmdline": ["python", "-m", "bcdata.ufld_baseline", "--resume-run", str(output) + "-other"]})]
    monkeypatch.setattr(queue.psutil, "process_iter", lambda attributes: processes)
    assert queue.matching_processes("bcdata.ufld_baseline", output) == [{"pid": 10, "created_at": 1}]


def test_resume_does_not_accept_another_source_experiment(tmp_path):
    state, source, _, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="Resume targets"):
        queue.validate_queue(state, tmp_path, source.with_name("other"))


def test_collection_two_validation_checks_frozen_validation_and_warm_start(tmp_path):
    state, source, _, _ = fixture(tmp_path)
    job = state["jobs"][0]
    output = Path(job["output_run"])
    snapshot = output / "dataset"
    rows = queue.read(Path(state["base_run"]) / "dataset/manifest.json")
    write(snapshot / "manifest.json", rows)
    write(snapshot / "experiment.json", {"base_manifest_sha256": state["base_manifest_sha256"]})
    config = {"epochs": 20, "batch_size": 4, "accumulation": 8, "learning_rate": .000625,
              "warmup_steps": 40, "initialization": {"warm_start": {"sha256": state["base_checkpoint_sha256"],
                "manifest_sha256": state["base_manifest_sha256"]}}}
    write(output / "config.json", config)
    queue.validate_collection_two(state, job, loader=lambda run: (snapshot, rows, {}))
    assert job["verified_export"]["validation_manifest_sha256"] == state["base_validation_manifest_sha256"]
    changed = [dict(row) for row in rows]
    changed[-1]["image_id"] = "different-val"
    with pytest.raises(ValueError, match="validation cohort"):
        queue.validate_collection_two(state, job, loader=lambda run: (snapshot, changed, {}))
    config["initialization"]["warm_start"]["sha256"] = "another checkpoint"
    write(output / "config.json", config)
    with pytest.raises(ValueError, match="warm-start"):
        queue.validate_collection_two(state, job, loader=lambda run: (snapshot, rows, {}))


def test_keep_awake_is_temporary_and_restores_when_work_raises():
    calls = []
    kernel = SimpleNamespace(SetThreadExecutionState=lambda value: calls.append(value) or 0x80000000)
    state = {}
    with pytest.raises(RuntimeError):
        with queue.keep_awake(state, kernel=kernel, windows=True):
            assert state["keep_awake"]["active"] is True
            raise RuntimeError("fixture")
    assert calls == [0x80000001, 0x80000000]
    assert state["keep_awake"]["active"] is False
    assert state["keep_awake"]["restored"] is True


def test_rejected_keep_awake_request_is_visible_not_claimed_success():
    calls = []
    kernel = SimpleNamespace(SetThreadExecutionState=lambda value: calls.append(value) or 0)
    state = {}
    with queue.keep_awake(state, kernel=kernel, windows=True):
        assert state["keep_awake"]["active"] is False
        assert "rejected" in state["keep_awake"]["warning"]
    assert calls == [0x80000001]
