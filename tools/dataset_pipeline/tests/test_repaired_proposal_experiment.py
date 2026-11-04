import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_ufld_experiment import experiment_fixture

from bcdata import repaired_proposal_experiment as follow
from bcdata.bootstrap import atomic_json
from bcdata.config import load_settings
from bcdata.geometry import HOOD_CROP_POLICY
from bcdata.proposal_training_policy import VERSION as TRAINING_GEOMETRY_POLICY
from bcdata.teachers.base import file_sha256


def fixture(tmp_path):
    settings = load_settings(overrides={"data_root": str(tmp_path)})
    base = tmp_path / "models/ufld_baseline/base"
    source = tmp_path / "experiments/flagged_teacher/source"
    after = tmp_path / "experiments/overnight_collections/ordered"
    output = tmp_path / "experiments/repaired_proposals/followup"
    base.mkdir(parents=True)
    (base / "best.pt").write_bytes(b"common cleared base weights")
    rows = [{"image_id": "base-train", "split": "train"}, {"image_id": "base-val", "split": "val"}]
    atomic_json(base / "dataset/manifest.json", rows)
    atomic_json(base / "dataset/hashes.json", {"manifest.json": file_sha256(base / "dataset/manifest.json")})
    atomic_json(base / "status.json", {"best_val_f1": .72, "best_short_span_val_f1": .79})
    atomic_json(source / "inventory.json", {"split_inventory": rows + [{"image_id": "held-out", "split": "test"}]})
    atomic_json(source / "copies.json", {"items": []})
    (source / "teacher").mkdir()
    (source / "teacher/best.pt").write_bytes(b"pinned adapted teacher weights")
    atomic_json(source / "teacher/teacher.json", {"checkpoint": str(source / "teacher/best.pt"),
                "checkpoint_sha256": file_sha256(source / "teacher/best.pt"), "name": "teacher", "version": "v1"})
    atomic_json(source / "experiment.json", {"schema": "flagged_teacher_trial:v1", "run_id": "source",
                "base_run": str(base), "base_manifest_sha256": file_sha256(base / "dataset/manifest.json"),
                "inventory_sha256": file_sha256(source / "inventory.json"), "max_extra_ratio": 1.0,
                "auto_promote": False, "phase": "training_ufld"})
    sim = tmp_path / "experiments/simulated_tulane"
    atomic_json(sim / "plan.json", {"source": "carlane_tulane"})
    queue = {"schema": "overnight_collection_queue:v1", "run_id": "ordered", "output": str(after),
             "data_root": str(tmp_path), "base_run": str(base), "source_model_experiment": str(source),
             "base_manifest_sha256": file_sha256(base / "dataset/manifest.json"),
             "base_checkpoint": str(base / "best.pt"), "base_checkpoint_sha256": file_sha256(base / "best.pt"),
             "base_validation_manifest_sha256": follow.ordered.digest([rows[-1]]),
             "source_model_inventory_sha256": file_sha256(source / "inventory.json"),
             "simulation_root": str(sim), "simulation_plan_sha256": file_sha256(sim / "plan.json"),
             "epochs": 20, "training_options": {key: value for key, value in follow.OPTIONS.items() if key != "epochs"},
             "code_sha256": "frozen-original-queue-code", "provisional": True, "unaudited_simulation": True,
             "auto_promote": False, "approved_collections_unchanged": True, "state": "running",
             "jobs": [{"id": job, "collection_id": collection, "epochs": 20, "state": "waiting",
                       "output_run": str(source / "ufld_run" if job == "collection_2" else after / job)}
                      for job, collection in follow.ordered.COLLECTIONS]}
    atomic_json(after / "queue.json", queue)
    python = tmp_path / "fake-python.exe"
    python.write_bytes(b"never executed by CPU fixtures")
    (tmp_path / "metadata.sqlite").write_bytes(b"human source registry stays read-only")
    request = follow.initialize(settings, source, after, output)
    return SimpleNamespace(settings=settings, base=base, source=source, after=after, output=output,
                           request=request, python=python, rows=rows)


def finish_repair(f):
    atomic_json(f.source / "proposal_repair.json", {"phase": "complete", "completed": 10, "total": 10,
                "failures": 0, "policy": HOOD_CROP_POLICY, "checkpoint_sha256": f.request["teacher_checkpoint_sha256"],
                "policy_sha256": f.request["repair_policy_sha256"],
                "geometry_code_sha256": f.request["pins"][str(follow.PIPELINE / "bcdata/geometry.py")]})


def finish_queue(f):
    queue = follow.read(f.after / "queue.json")
    queue["state"] = "complete"
    for job in queue["jobs"]:
        job["state"] = "complete"
        run = Path(job["output_run"])
        atomic_json(run / "dataset/manifest.json", f.rows)
        atomic_json(run / "config.json", {**{key: value for key, value in follow.OPTIONS.items() if key != "evaluate_initial"},
                    "initialization": {"warm_start": {"sha256": f.request["base_checkpoint_sha256"],
                        "manifest_sha256": f.request["base_manifest_sha256"]}}})
    atomic_json(f.after / "queue.json", queue)


def ready(*_):
    return True, "ready", {"verified_predecessors": [{"id": "collection_4"}]}


def fake_snapshot(settings, request):
    run = Path(request["output"]) / "ufld_run"
    atomic_json(run / "dataset/experiment.json", {"base_train_images": 1, "extra_train_images": 1,
                "train_images": 2, "val_images": 1})
    atomic_json(run / "config.json", {**{key: value for key, value in follow.OPTIONS.items() if key != "evaluate_initial"},
                "initialization": {"warm_start": {"sha256": request["base_checkpoint_sha256"],
                    "manifest_sha256": request["base_manifest_sha256"]}}})
    return {"base_train_images": 1, "extra_train_images": 1, "train_images": 2, "val_images": 1}


def test_initialization_pins_inputs_order_commonbase_without_mutating_production(tmp_path):
    f = fixture(tmp_path)
    assert f.request["manual_approval_required"] is False
    assert f.request["auto_promote"] is False and f.request["approved_collections_unchanged"]
    assert f.request["base_checkpoint"] == str(f.base / "best.pt")
    assert f.request["training_geometry_policy"] == TRAINING_GEOMETRY_POLICY
    assert str(follow.PIPELINE / "bcdata/proposal_training_policy.py") in f.request["pins"]
    assert f.request["base_validation_manifest_sha256"] == follow.ordered.digest([f.rows[-1]])
    assert [job["id"] for job in f.request["queue_identity"]["jobs"]] == ["collection_2", "collection_3", "collection_4"]
    assert (tmp_path / "metadata.sqlite").read_bytes() == b"human source registry stays read-only"
    assert not (f.output / "ufld_run").exists()
    with pytest.raises(ValueError, match="new directory"):
        follow.initialize(f.settings, f.source, f.after, f.output)


def test_incomplete_repair_waits_without_snapshot_or_model_launch(tmp_path):
    f = fixture(tmp_path)
    atomic_json(f.source / "proposal_repair.json", {"phase": "repairing", "completed": 9, "total": 10})
    forbidden = lambda *args, **kwargs: pytest.fail("An incomplete repair cannot build or launch")
    result = follow.worker(f.settings, f.output / "request.json", python=f.python,
                           freezer=forbidden, launcher=forbidden, max_ticks=1)
    assert result["phase"] == "waiting_repair" and result["launches"] == 0
    assert not list(tmp_path.rglob("large-models.lock"))


def test_full_original_queue_must_finish_not_just_collection2(tmp_path):
    f = fixture(tmp_path)
    finish_repair(f)
    queue = follow.read(f.after / "queue.json")
    queue["jobs"][0]["state"] = "complete"
    atomic_json(f.after / "queue.json", queue)
    ready_value, phase, _ = follow.prerequisites(f.request)
    assert not ready_value and phase == "waiting_ordered_queue"
    assert queue == follow.read(f.after / "queue.json"), "Waiting cannot modify the original queue"
    queue["state"] = "error"
    atomic_json(f.after / "queue.json", queue)
    with pytest.raises(RuntimeError, match="can overtake"):
        follow.prerequisites(f.request)


def test_completion_requires_checkpoint_validation_common_init_and_exited_workers(tmp_path):
    f = fixture(tmp_path)
    finish_repair(f); finish_queue(f)
    completed = lambda path: {"checkpoint_sha256": "a" * 64}
    loader = lambda path: (Path(path) / "dataset", f.rows, {})
    checks = dict(completed=completed, experiment_loader=loader, collection_loader=loader,
                  processes=lambda *args: [], lock_held=lambda path: False)
    assert follow.prerequisites(f.request, **checks)[0]
    assert not follow.prerequisites(f.request, **{**checks, "lock_held": lambda path: True})[0]
    assert not follow.prerequisites(f.request, **{**checks, "processes": lambda *args: [{"pid": 123}]})[0]
    with pytest.raises(ValueError, match="verified trained checkpoint"):
        follow.prerequisites(f.request, **{**checks, "completed": lambda run: None})
    changed_rows = [f.rows[0], {"image_id": "different-validation", "split": "val"}]
    with pytest.raises(ValueError, match="validation differs"):
        follow.prerequisites(f.request, **{**checks, "collection_loader": lambda path: (path, changed_rows, {})})
    third = f.after / "collection_3/config.json"
    value = follow.read(third)
    value["initialization"]["warm_start"]["sha256"] = "incorrect-predecessor-weights"
    atomic_json(third, value)
    with pytest.raises(ValueError, match="initialization"):
        follow.prerequisites(f.request, **checks)


def test_user_stopped_collection_two_is_recorded_but_later_completed_jobs_gate_followup(tmp_path):
    f = fixture(tmp_path)
    finish_repair(f); finish_queue(f)
    queue = follow.read(f.after / "queue.json")
    queue["jobs"][0].update(state="stopped", stop_reason="user_requested_advance_to_collection_3",
                            completed_20_epochs=False)
    atomic_json(f.after / "queue.json", queue)
    def completed(run):
        assert Path(run) != f.source / "ufld_run"
        return {"checkpoint_sha256": "a" * 64}
    loader = lambda path: (Path(path) / "dataset", f.rows, {})
    ready, phase, detail = follow.prerequisites(f.request, completed=completed,
        experiment_loader=lambda path: pytest.fail("Stopped run must not be treated as complete"),
        collection_loader=loader, processes=lambda *args: [], lock_held=lambda path: False)
    assert ready and phase == "ready"
    assert detail["verified_predecessors"][0]["state"] == "stopped"
    queue["jobs"][1]["state"] = "waiting"
    atomic_json(f.after / "queue.json", queue)
    assert follow.prerequisites(f.request)[1] == "waiting_ordered_queue"


@pytest.mark.parametrize("target", ["base_checkpoint", "base_manifest", "teacher", "inventory"])
def test_pinned_input_changes_fail_without_launching(tmp_path, target):
    f = fixture(tmp_path)
    path = {"base_checkpoint": f.base / "best.pt", "base_manifest": f.base / "dataset/manifest.json",
            "teacher": f.source / "teacher/teacher.json", "inventory": f.source / "inventory.json"}[target]
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="Pinned input changed"):
        follow.worker(f.settings, f.output / "request.json", python=f.python,
                      checker=lambda *args: pytest.fail("Pins must be checked first"), max_ticks=1)
    assert follow.read(f.output / "status.json")["state"] == "error"


def test_command_uses_same_cleared_base_not_predecessor_and_only_resume_restores_state(tmp_path):
    f = fixture(tmp_path)
    command = follow.training_command(f.request, f.python)
    assert all(flag in command for flag in ("--experimental-data", "--warm-start", "--evaluate-initial"))
    for name, expected in (("--epochs", "20"), ("--batch-size", "4"), ("--accumulation", "8"),
                           ("--learning-rate", "0.000625"), ("--warmup-steps", "40"), ("--gpu-lock-timeout", "-1")):
        assert command[command.index(name) + 1] == expected
    assert command[command.index("--warm-start") + 1] == str(f.base / "best.pt")
    assert command[command.index("--status-report") + 1] == str(f.output / "ufld_run/training_status.json")
    run = f.output / "ufld_run"
    run.mkdir(); (run / "last.pt").write_bytes(b"owned resume checkpoint")
    command = follow.training_command(f.request, f.python)
    assert "--resume-run" in command and "--warm-start" not in command and "--evaluate-initial" not in command


def test_orphaned_live_trainer_is_monitored_without_duplicate_launch(tmp_path):
    f = fixture(tmp_path)
    result = follow.worker(f.settings, f.output / "request.json", python=f.python, checker=ready,
        freezer=fake_snapshot, processes=lambda *args: [{"pid": 123, "created_at": 1}],
        launcher=lambda *args: pytest.fail("A live trainer must not be duplicated"), max_ticks=1)
    assert result["state"] == "running" and result["trainer_processes"][0]["pid"] == 123
    assert result["launches"] == 0


def test_launch_completion_and_worker_resume_are_idempotent_and_preserve_originals(tmp_path):
    f = fixture(tmp_path)
    originals = {path: path.read_bytes() for directory in (f.base, f.source, f.after)
                 for path in directory.rglob("*") if path.is_file()}
    calls = []

    def launcher(command, log, root):
        calls.append(command)
        atomic_json(f.output / "ufld_run/status.json", {"phase": "trained_awaiting_review", "epoch": 20, "epochs": 20})
        return SimpleNamespace(pid=123, poll=lambda: 0)

    completed = lambda run: {"checkpoint_sha256": "b" * 64, "best_val_f1": .73, "epoch": 20, "epochs": 20}
    options = dict(python=f.python, checker=ready, freezer=fake_snapshot, launcher=launcher,
                   processes=lambda *args: [], completed=completed, sleep=lambda seconds: None)
    result = follow.worker(f.settings, f.output / "request.json", **options)
    assert result["state"] == "complete" and len(calls) == 1
    result = follow.worker(f.settings, f.output / "request.json", **options)
    assert result["state"] == "complete" and len(calls) == 1
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert (tmp_path / "metadata.sqlite").read_bytes() == b"human source registry stays read-only"
    comparison = follow.read(f.output / "comparison.json")
    assert comparison["validation_images_used_for_training"] == comparison["test_images_used_for_training"] == 0
    assert comparison["warm_start_checkpoint_sha256"] == f.request["base_checkpoint_sha256"]


def test_at_most_two_checkpoint_resume_attempts_and_no_restart_without_checkpoint(tmp_path):
    f = fixture(tmp_path)
    calls = []

    def launcher(command, *args):
        calls.append(command)
        (f.output / "ufld_run/last.pt").write_bytes(b"interrupted checkpoint")
        return SimpleNamespace(pid=100 + len(calls), poll=lambda: 1)

    with pytest.raises(RuntimeError, match="Two checkpoint resume attempts"):
        follow.worker(f.settings, f.output / "request.json", python=f.python, checker=ready,
                      freezer=fake_snapshot, launcher=launcher, processes=lambda *args: [], sleep=lambda seconds: None)
    assert len(calls) == 3
    assert "--warm-start" in calls[0] and all("--resume-run" in command for command in calls[1:])
    assert follow.read(f.output / "status.json")["launches"] == 3


def test_snapshot_partial_is_preserved_and_requires_inspection(tmp_path):
    f = fixture(tmp_path)
    partial = f.output / "ufld_run.preparing-old"
    partial.mkdir(); sentinel = partial / "FAILED.json"; sentinel.write_text("preserve incomplete data")
    with pytest.raises(RuntimeError, match="staging is preserved"):
        follow.freeze_snapshot(f.settings, f.request)
    assert sentinel.read_text() == "preserve incomplete data"


def test_failed_or_mismatched_repair_and_changed_queue_order_fail_closed(tmp_path):
    f = fixture(tmp_path)
    finish_repair(f)
    repair_path = f.source / "proposal_repair.json"
    value = follow.read(repair_path); value["policy_sha256"] = "wrong-settings"
    atomic_json(repair_path, value)
    with pytest.raises(ValueError, match="Completed repair"):
        follow.prerequisites(f.request)
    value["phase"] = "complete_with_failures"
    atomic_json(repair_path, value)
    with pytest.raises(RuntimeError, match="requires attention"):
        follow.prerequisites(f.request)
    queue = follow.read(f.after / "queue.json")
    queue["jobs"].reverse(); atomic_json(f.after / "queue.json", queue)
    with pytest.raises(ValueError, match="identity changed"):
        follow.prerequisites(f.request)


def test_pin_verifier_hashes_only_once_until_stat_changes(tmp_path, monkeypatch):
    path = tmp_path / "big-immutable-inventory.json"
    path.write_bytes(b"unchanged frozen inventory")
    expected = file_sha256(path)
    calls = []
    original = follow.file_sha256
    monkeypatch.setattr(follow, "file_sha256", lambda candidate: calls.append(candidate) or original(candidate))
    verifier = follow.PinVerifier({str(path): expected})
    verifier.verify(); verifier.verify(); verifier.verify()
    assert len(calls) == 1
    path.write_bytes(path.read_bytes())
    verifier.verify()
    assert len(calls) == 2
    path.write_bytes(b"different")
    with pytest.raises(ValueError, match="Pinned input changed"):
        verifier.verify()


def test_real_snapshot_keeps_all_cleared_base_fixed_val_and_rejects_heldout_without_manual_review(experiment_fixture):
    f = experiment_fixture
    settings = load_settings(overrides={"data_root": str(f.temp)})
    source = f.temp / "experiments/flagged_teacher/source"
    extras = [f.extra(0), f.extra(1, split="test"), f.extra(2, session_id="held-test-session")]
    copies = []
    for index, extra in enumerate(extras):
        record = extra["record"]
        copy_id = "copy-" + record["image_id"]
        image_sha = file_sha256(extra["image_path"])
        item = {"copy_id": copy_id, "original_image_id": record["image_id"], "record": record,
                "image_path": str(extra["image_path"]), "copy_sha256": image_sha, "original_sha256": image_sha,
                "width": 1640, "height": 590, "split": record["split"], "source": "fixture"}
        copies.append(item)
        original = {"copy_id": copy_id, "input_sha256": image_sha,
                    "teacher_provenance": {"checkpoint_sha256": f.teacher["checkpoint_sha256"]},
                    "geometry": {"width": 1640, "height": 590, "lanes": []},
                    "quality": {"eligible_for_experiment": False}}
        path = source / "proposals" / (copy_id + ".json")
        atomic_json(path, original)
        repaired = {**original, "geometry": {**extra["geometry"], "width": 1640, "height": 590,
                                            "hood_crop_policy": HOOD_CROP_POLICY},
                    "quality": {"eligible_for_experiment": True, "human_approved": False, "reasons": []},
                    "repair_provenance": {"policy": HOOD_CROP_POLICY,
                        "original_proposal_sha256": file_sha256(path), "input_sha256": image_sha,
                        "checkpoint_sha256": f.teacher["checkpoint_sha256"]}}
        atomic_json(source / "proposals_hood_repaired" / (copy_id + ".json"), repaired)
    atomic_json(source / "copies.json", {"items": copies})
    atomic_json(source / "inventory.json", {"split_inventory": f.inventory})
    atomic_json(source / "teacher/teacher.json", f.teacher)
    atomic_json(source / "proposal_repair.json", {"phase": "complete", "completed": 3, "total": 3})
    output = f.temp / "followup"
    atomic_json(output / "request.json", {"fixture": "immutable request"})
    request = {"output": str(output), "source_experiment": str(source), "base_run": str(f.base),
               "base_manifest_sha256": file_sha256(f.dataset / "manifest.json"), "max_extra_ratio": 1.0,
               "base_validation_manifest_sha256": follow.ordered.digest([row for row in f.records if row["split"] == "val"]),
               "training_geometry_policy": TRAINING_GEOMETRY_POLICY}
    before = {path: path.read_bytes() for folder in (f.base, source) for path in folder.rglob("*") if path.is_file()}
    report = follow.freeze_snapshot(settings, request)
    from bcdata.ufld_experiment import load_experiment_export
    snapshot, records, _ = load_experiment_export(output / "ufld_run")
    assert records[:len(f.records)] == f.records
    assert report["base_train_images"] == 2 and report["base_val_images"] == report["val_images"] == 1
    assert report["extra_train_images"] == 1 and report["trained_repaired_proposals"] == 1
    assert report["trained_original_proposals"] == 0
    assert report["test_images_used"] == 0 and report["training_geometry_policy"] == TRAINING_GEOMETRY_POLICY
    assert report["selection_excluded_counts"]["held_out_comparison_only"] == 1
    assert report["rejection_counts"]["extra_is_held_out_image_or_session"] == 1
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert not (source / "review.sqlite").exists(), "No individual approval is required for automatic QC passes"
    assert not (snapshot / "images/test").exists()
    assert follow.freeze_snapshot(settings, request) == report
