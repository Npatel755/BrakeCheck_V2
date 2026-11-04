"""Persistent, private collection 2 -> 3 -> 4 UFLDv2 experiment queue.

Human audits and approved datasets remain independent. This queue's explicit
provisional simulation permission applies only to new isolated experiment runs.
It never imports a registry, changes a production export or promotes a model.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from filelock import FileLock, Timeout
import psutil

SCHEMA = "overnight_collection_queue:v1"
COLLECTIONS = (("collection_2", "converted_plus_proposals"),
               ("collection_3", "converted_plus_simulated"),
               ("collection_4", "converted_plus_proposals_plus_simulated"))
TERMINAL_JOB_STATES = {"complete", "stopped"}
PIPELINE = Path(__file__).resolve().parents[1]
RESERVE_BYTES = 28 * 1024 ** 3  # 20 GiB snapshot reserve + 8 GiB checkpoint room.


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_bytes())


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.part")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def inside(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("Experiment output leaves its isolated directory")
    return path


def lock_held(path):
    """A kernel-backed lock identifies the worker; a stale PID never does."""
    lock = FileLock(str(path))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return True
    lock.release()
    return False


def matching_processes(module, output):
    """Match module plus an exact CLI run path, avoiding PID reuse/unrelated work."""
    wanted = os.path.normcase(str(Path(output).resolve()))
    result = []
    for process in psutil.process_iter(["pid", "cmdline", "create_time"]):
        try:
            args = process.info.get("cmdline") or []
            if "-m" not in args or module not in args:
                continue
            paths = []
            for option in ("--output", "--prepared-run", "--resume-run"):
                if option in args and args.index(option) + 1 < len(args):
                    paths.append(os.path.normcase(str(Path(args[args.index(option) + 1]).resolve())))
            if wanted in paths:
                result.append({"pid": process.info["pid"], "created_at": process.info.get("create_time")})
        except (OSError, psutil.Error, ValueError):
            continue
    return result


def initialize_queue(data_root, model_experiment, *, epochs=20):
    data_root = Path(data_root).resolve()
    experiments = data_root / "experiments"
    model_experiment = inside(experiments / "flagged_teacher", model_experiment)
    source = read(model_experiment / "experiment.json")
    if source.get("schema") != "flagged_teacher_trial:v1" or source.get("auto_promote") is not False:
        raise ValueError("Expected the existing isolated flagged teacher experiment")
    base_run = inside(data_root / "models/ufld_baseline", source["base_run"])
    base_manifest = base_run / "dataset/manifest.json"
    if file_sha256(base_manifest) != source["base_manifest_sha256"]:
        raise ValueError("Cleared base manifest changed")
    checkpoint = base_run / "best.pt"
    rows = read(base_manifest)
    val_rows = [row for row in rows if row["split"] == "val"]
    if not val_rows or not any(row["split"] == "train" for row in rows):
        raise ValueError("The common base must have TRAIN and held-out VAL data")
    sim_root = experiments / "simulated_tulane"
    plan = read(sim_root / "plan.json")
    if plan.get("source") != "carlane_tulane" or plan.get("dataset_version") != 2 or plan.get("pilot") is not False:
        raise ValueError("Only the full pinned TuLane synthetic import can enter this queue")
    run_id = "run-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    output = experiments / "overnight_collections" / run_id
    if output.exists():
        raise FileExistsError("Overnight run name already exists")
    output.mkdir(parents=True)
    jobs = []
    for number, collection in COLLECTIONS:
        path = model_experiment / "ufld_run" if number == "collection_2" else output / number
        jobs.append({"id": number, "collection_id": collection, "state": "waiting_import",
                     "output_run": str(path), "epochs": epochs, "restarts": 0,
                     "provisional": True, "auto_promote": False,
                     "unaudited_simulation": number != "collection_2"})
    state = {"schema": SCHEMA, "run_id": run_id, "output": str(output), "data_root": str(data_root),
             "state": "running", "phase": "waiting_import", "created_at": now(), "updated_at": now(),
             "pid": os.getpid(), "base_run": str(base_run), "base_manifest_sha256": file_sha256(base_manifest),
             "base_validation_manifest_sha256": digest(val_rows), "base_checkpoint": str(checkpoint),
             "base_checkpoint_sha256": file_sha256(checkpoint), "source_model_experiment": str(model_experiment),
             "source_model_inventory_sha256": source["inventory_sha256"], "simulation_root": str(sim_root),
             "simulation_plan_sha256": file_sha256(sim_root / "plan.json"), "provisional": True,
             "unaudited_simulation": True, "auto_promote": False, "approved_collections_unchanged": True,
             "test_images_used_for_training": 0, "validation_images_used_for_training": 0,
             "epochs": epochs, "common_initialization": "same unchanged cleared-base best.pt for all three experiments",
             "jobs": jobs, "code_sha256": file_sha256(__file__),
             "training_options": {"batch_size": 4, "accumulation": 8, "learning_rate": .000625,
                                  "warmup_steps": 40, "evaluate_initial": True},
             "comparison_caveat": "Different training-set sizes change optimization steps; equal epochs are not a matched-budget causal comparison."}
    shutil.copy2(__file__, output / "orchestrator_code.py")
    return state


def validate_queue(state, data_root, model_experiment):
    data_root = Path(data_root).resolve()
    root = data_root / "experiments"
    if state.get("schema") != SCHEMA or state.get("auto_promote") is not False:
        raise ValueError("Invalid private queue")
    if state.get("provisional") is not True or state.get("unaudited_simulation") is not True or state.get("approved_collections_unchanged") is not True:
        raise ValueError("Private provisional permissions changed")
    if Path(state["data_root"]).resolve() != data_root or Path(state["source_model_experiment"]).resolve() != Path(model_experiment).resolve():
        raise ValueError("Resume targets differ from the pinned queue")
    output = inside(root / "overnight_collections", state["output"])
    source = inside(root / "flagged_teacher", state["source_model_experiment"])
    if Path(state["simulation_root"]).resolve() != (root / "simulated_tulane").resolve():
        raise ValueError("Simulation root changed")
    base = inside(data_root / "models/ufld_baseline", state["base_run"])
    if file_sha256(base / "dataset/manifest.json") != state["base_manifest_sha256"]:
        raise ValueError("Pinned common base manifest changed")
    if Path(state["base_checkpoint"]).resolve() != (base / "best.pt").resolve() or file_sha256(base / "best.pt") != state["base_checkpoint_sha256"]:
        raise ValueError("Pinned common initialization changed")
    if file_sha256(Path(state["simulation_root"]) / "plan.json") != state["simulation_plan_sha256"]:
        raise ValueError("Pinned simulation plan changed")
    if [(job["id"], job["collection_id"]) for job in state["jobs"]] != list(COLLECTIONS):
        raise ValueError("Collection order changed")
    for job in state["jobs"]:
        expected = source / "ufld_run" if job["id"] == "collection_2" else output / job["id"]
        if Path(job["output_run"]).resolve() != expected.resolve() or job.get("epochs") != state["epochs"]:
            raise ValueError("Pinned job path or epoch count changed")
    return output


def simulation_ready(state):
    sim_root = Path(state["simulation_root"])
    if file_sha256(sim_root / "plan.json") != state["simulation_plan_sha256"]:
        raise ValueError("Pinned simulation plan changed")
    status = read(sim_root / "import.json")
    state["import_progress"] = {key: status.get(key) for key in ("state", "converted", "total_expected", "updated_at", "errors")}
    if status.get("state") == "error":
        raise RuntimeError("Simulation importer failed: " + str(status.get("last_error", status.get("errors", []))))
    if status.get("state") not in {"awaiting_audit", "ready"}:
        return False
    if status.get("schema") != "tulane_simulation_import:v1" or status.get("source") != "carlane_tulane" or status.get("domain") != "synthetic":
        raise ValueError("Simulation import identity changed")
    if status.get("total_expected") != 26400 or status.get("converted") != 26400:
        raise ValueError("Simulation import is not the full 26,400-image source")
    for filename, field in (("manifest.jsonl", "manifest_sha256"), ("audits.json", "audits_sha256")):
        actual = file_sha256(sim_root / filename)
        if actual != status.get(field):
            raise ValueError("Completed simulation artifact SHA256 changed: " + filename)
        pinned = state.get("simulation_" + field)
        if pinned is not None and pinned != actual:
            raise ValueError("Pinned simulation artifact changed: " + filename)
        state["simulation_" + field] = actual
    # We deliberately never read audit decisions. This permission concerns only
    # these new provisional runs; the public approved collection stays gated.
    return True


def read_status(output):
    path = Path(output) / "status.json"
    return read(path) if path.is_file() else {}


def update_job_from_status(job, status):
    job["progress"] = {key: status[key] for key in ("phase", "epoch", "epochs", "step", "total_steps", "train_images", "val_images", "loss", "updated_at") if key in status}
    job["metrics"] = {key: status[key] for key in ("best_val_f1", "best_short_span_val_f1", "initial_val_f1", "initial_short_span_val_f1") if key in status}
    if "train_images" in status:
        job["train_images"] = status["train_images"]
    path = Path(job["output_run"]) / "dataset/experiment.json"
    if path.is_file():
        dataset = read(path)
        for key in ("train_images", "val_images", "extra_train_images", "base_train_images", "simulated_train_images", "model_train_images"):
            if key in dataset:
                job["extra_images" if key == "extra_train_images" else key] = dataset[key]


def validate_collection_two(state, job, *, loader=None):
    """Do not advance from a status label alone: validate frozen export and fit."""
    if loader is None:
        from .ufld_experiment import load_experiment_export
        loader = load_experiment_export
    output = Path(job["output_run"])
    snapshot, records, _ = loader(output)
    dataset = read(snapshot / "experiment.json")
    if dataset.get("base_manifest_sha256") != state["base_manifest_sha256"]:
        raise ValueError("Collection 2 does not use the pinned complete cleared base")
    validation = [row for row in records if row["split"] == "val"]
    if digest(validation) != state["base_validation_manifest_sha256"]:
        raise ValueError("Collection 2 validation cohort differs from the pinned common base")
    config = read(output / "config.json")
    warm = (config.get("initialization") or {}).get("warm_start") or {}
    if (warm.get("sha256") != state["base_checkpoint_sha256"]
            or warm.get("manifest_sha256") != state["base_manifest_sha256"]):
        raise ValueError("Collection 2 did not use the pinned common warm-start checkpoint")
    expected = {"epochs": 20, "batch_size": 4, "accumulation": 8, "learning_rate": .000625, "warmup_steps": 40}
    if any(config.get(key) != value for key, value in expected.items()):
        raise ValueError("Collection 2 training settings differ from this fixed comparison")
    source = read(Path(state["source_model_experiment"]) / "experiment.json")
    if source.get("inventory_sha256") != state["source_model_inventory_sha256"]:
        raise ValueError("Source model inventory pin changed")
    job["verified_export"] = {"manifest_sha256": file_sha256(snapshot / "manifest.json"),
                              "validation_manifest_sha256": digest(validation),
                              "config_sha256": file_sha256(output / "config.json"), "verified_at": now()}


def training_command(state, job, python):
    output = Path(job["output_run"])
    command = [str(python), "-u", "-m", "bcdata.ufld_baseline", "--experimental-data",
               "--status-report", str(output / "training_status.json"), "--epochs", str(job["epochs"]),
               "--batch-size", "4", "--accumulation", "8", "--learning-rate", "0.000625",
               "--warmup-steps", "40", "--gpu-lock-timeout", "-1"]
    if (output / "last.pt").is_file():
        command += ["--resume-run", str(output)]
    else:
        command += ["--prepared-run", str(output), "--warm-start", state["base_checkpoint"], "--evaluate-initial"]
    return command


def launch(command, log_prefix, data_root):
    prefix = Path(log_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, BRAKECHECK_DATA_ROOT=str(data_root), PYTHONUNBUFFERED="1")
    with Path(str(prefix) + ".stdout.log").open("a", encoding="utf-8") as out, Path(str(prefix) + ".stderr.log").open("a", encoding="utf-8") as err:
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        child = subprocess.Popen(command, cwd=PIPELINE, env=environment, stdout=out, stderr=err, **options)
    return child


def has_space(state, job):
    free = shutil.disk_usage(state["output"]).free
    job["disk_free_bytes"] = free
    if free < RESERVE_BYTES:
        job["state"] = "waiting_space"
        job["detail"] = "Waiting for 20 GiB reserve plus 8 GiB checkpoint working room; no files are deleted."
        return False
    job.pop("detail", None)
    return True


@contextmanager
def keep_awake(state, *, kernel=None, windows=None):
    """A temporary process-thread request; the screen may still turn off."""
    windows = os.name == "nt" if windows is None else windows
    state["keep_awake"] = {"requested": windows, "active": False,
                           "scope": "queue process only; no power-plan setting changes; display may sleep"}
    if not windows:
        yield
        return
    if kernel is None:
        import ctypes
        kernel = ctypes.windll.kernel32
    accepted = False
    try:
        accepted = bool(kernel.SetThreadExecutionState(0x80000000 | 0x00000001))
        state["keep_awake"]["active"] = accepted
        if not accepted:
            state["keep_awake"]["warning"] = "Windows rejected the temporary system-awake request; normal sleep settings still apply."
        yield
    finally:
        if accepted:
            restored = bool(kernel.SetThreadExecutionState(0x80000000))
            state["keep_awake"]["restored"] = restored
        state["keep_awake"]["active"] = False


def collection_two_tick(state, job, python, *, launcher=launch):
    source = Path(state["source_model_experiment"])
    original = read(source / "experiment.json")
    status = read_status(job["output_run"])
    update_job_from_status(job, status)
    alive = lock_held(source / "worker.lock") or bool(matching_processes("bcdata.flagged_model_trial", source))
    trainer_alive = bool(matching_processes("bcdata.ufld_baseline", job["output_run"]))
    finished = original.get("phase") == "complete_awaiting_comparison_review" or status.get("phase") == "trained_awaiting_review"
    if finished:
        if alive or trainer_alive:
            job["state"] = "running"
            job["detail"] = "Existing collection 2 worker is completing its final comparison."
            return False
        if status.get("phase") != "trained_awaiting_review":
            raise ValueError("Collection 2 reports complete without a completed UFLDv2 status")
        validate_collection_two(state, job)
        job.update(state="complete", finished_at=job.get("finished_at", now()), reused_existing_run=True)
        return True
    if alive or trainer_alive:
        job["state"] = "running" if original.get("phase") == "training_ufld" or trainer_alive else "waiting_proposals"
        job["source_phase"] = original.get("phase")
        return False
    if job.get("restarts", 0) >= 2:
        raise RuntimeError("Existing collection 2 pipeline exited; two resume attempts exhausted")
    if not has_space(state, job):
        return False
    child = launcher([str(python), "-u", "-m", "bcdata.flagged_model_trial", "--resume", "--output", str(source),
                      "--data-root", state["data_root"]], Path(state["output"]) / "collection_2_resume", state["data_root"])
    job.update(state="waiting_proposals", restarts=job.get("restarts", 0) + 1, last_launch_pid=child.pid,
               last_launch_at=now(), source_phase=original.get("phase"))
    return False


def stop_collection_two(state):
    """Record a user-requested stop after the original worker and trainer exit.

    This keeps the unfinished checkpoint and measurements without pretending the
    20-epoch comparison finished. A resumed queue advances to collection 3.
    """
    output = validate_queue(state, state["data_root"], state["source_model_experiment"])
    job = state["jobs"][0]
    if job["state"] == "stopped":
        return state
    if job["state"] == "complete":
        raise ValueError("Collection 2 already completed")
    source = Path(state["source_model_experiment"])
    run = Path(job["output_run"])
    if (lock_held(output.parent / "queue.worker.lock") or lock_held(source / "worker.lock")
            or matching_processes("bcdata.flagged_model_trial", source)
            or matching_processes("bcdata.ufld_baseline", run)):
        raise RuntimeError("Collection 2 processes must exit before recording a stop")
    if not (run / "dataset/experiment.json").is_file() or not (run / "dataset/split_inventory.json").is_file():
        raise ValueError("Collection 2 frozen training snapshot is missing")
    status = read_status(run)
    update_job_from_status(job, status)
    last = run / "last.pt"
    job.update(state="stopped", stopped_at=now(), stop_reason="user_requested_advance_to_collection_3",
               completed_20_epochs=False, last_checkpoint=str(last) if last.is_file() else None,
               last_checkpoint_bytes=last.stat().st_size if last.is_file() else 0)
    state.update(state="running", phase="collection_2_stopped", current_job="collection_3",
                 transition_code_sha256=file_sha256(__file__))
    persist(state)
    return state


def comparison(state):
    return {"schema": "overnight_collection_comparison:v1", "created_at": now(), "provisional": True,
            "unaudited_simulation": True, "auto_promote": False, "approved_collections_unchanged": True,
            "base_run": state["base_run"], "base_manifest_sha256": state["base_manifest_sha256"],
            "common_validation_manifest_sha256": state["base_validation_manifest_sha256"],
            "warm_start_checkpoint_sha256": state["base_checkpoint_sha256"], "test_images_used_for_training": 0,
            "validation_images_used_for_training": 0, "simulation_manifest_sha256": state.get("simulation_manifest_sha256"),
            "jobs": [{key: job[key] for key in ("id", "collection_id", "state", "output_run", "train_images", "extra_images", "metrics", "epochs") if key in job} for job in state["jobs"]],
            "limitation": state["comparison_caveat"]}


def persist(state):
    state.update(pid=os.getpid(), updated_at=now(), heartbeat_at=now())
    output = Path(state["output"])
    atomic_json(output / "queue.json", state)
    atomic_json(output / "comparison.json", comparison(state))
    atomic_json(output.parent / "active.json", state)


def _run_queue(state, *, python=None, poll_seconds=15, sleep=time.sleep, builder=None, loader=None, launcher=launch, max_ticks=None, _runtime=None):
    """Run ordered stages. Injection hooks make orchestration testable without GPUs."""
    python = Path(python) if python else PIPELINE / ".gpu-venv/Scripts/python.exe"
    if not python.is_file():
        raise FileNotFoundError("The existing GPU Python environment is required")
    if not 10 <= poll_seconds <= 60:
        raise ValueError("Poll interval must be between 10 and 60 seconds")
    output = validate_queue(state, state["data_root"], state["source_model_experiment"])
    output.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(output.parent / "queue.worker.lock"), timeout=0):
        if _runtime is not None:
            _runtime["owned"] = True
        state["state"] = "running"
        state.pop("error", None)
        ticks = 0
        try:
            while True:
                ticks += 1
                current = next((job for job in state["jobs"] if job["state"] not in TERMINAL_JOB_STATES), None)
                if current is None:
                    state.update(state="complete", phase="complete_awaiting_comparison_review", finished_at=now())
                    persist(state)
                    return state
                state["current_job"] = current["id"]
                if not simulation_ready(state):
                    state["phase"] = "waiting_import"
                    current["state"] = "waiting_import"
                elif current["id"] == "collection_2":
                    state["phase"] = "collection_2_existing_pipeline"
                    collection_two_tick(state, current, python, launcher=launcher)
                else:
                    state["phase"] = current["id"]
                    run = Path(current["output_run"])
                    status = read_status(run)
                    update_job_from_status(current, status)
                    alive = matching_processes("bcdata.ufld_baseline", run)
                    if status.get("phase") == "trained_awaiting_review" and not alive:
                        current.update(state="complete", finished_at=now())
                    elif alive:
                        current["state"] = "running"
                        current["worker_processes"] = alive
                    elif not has_space(state, current):
                        pass
                    else:
                        if builder is None or loader is None:
                            from .ufld_collection_experiment import build_collection_experiment, load_collection_export
                            builder = builder or build_collection_experiment
                            loader = loader or load_collection_export
                        if not (run / "dataset/experiment.json").is_file():
                            current.update(state="building", started_at=current.get("started_at", now()))
                            persist(state)
                            inventory_path = Path(state["jobs"][0]["output_run"]) / "dataset/split_inventory.json"
                            report = builder(state["base_run"], run, split_inventory=read(inventory_path),
                                             collection_id=current["collection_id"],
                                             model_run=state["jobs"][0]["output_run"] if current["id"] == "collection_4" else None,
                                             sim_root=state["simulation_root"])
                            current["dataset"] = report
                        loader(run)
                        if not has_space(state, current):
                            persist(state)
                            sleep(poll_seconds)
                            continue
                        launches = current.get("launches", 0)
                        if launches >= 3:
                            raise RuntimeError(current["id"] + " training exited; two checkpoint resume retries exhausted")
                        child = launcher(training_command(state, current, python), output / (current["id"] + ".training"), state["data_root"])
                        current.update(state="running", launches=launches + 1, restarts=max(0, launches),
                                       last_launch_pid=child.pid, last_launch_at=now(), started_at=current.get("started_at", now()))
                state["state"] = current["state"] if current["state"] in {"waiting_import", "waiting_proposals", "waiting_space"} else "running"
                persist(state)
                if max_ticks is not None and ticks >= max_ticks:
                    return state
                sleep(poll_seconds)
        except BaseException as error:
            state.update(state="error", error=str(error)[:1800], error_type=type(error).__name__)
            if current is not None:
                current.update(state="error", error=str(error)[:1800])
            persist(state)
            raise


def run_queue(state, **options):
    runtime = {}
    try:
        with keep_awake(state):
            return _run_queue(state, _runtime=runtime, **options)
    finally:
        # Record that the temporary request ended when this queue invocation
        # exits. An unexpected process kill also releases Windows' request.
        output = Path(state["output"])
        if runtime.get("owned") and (output / "queue.json").is_file():
            persist(state)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--model-experiment", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-collection-2", action="store_true",
                        help="Record a stopped collection 2 after its worker and trainer have exited")
    args = parser.parse_args(argv)
    if args.epochs != 20:
        parser.error("This fixed comparison queue requires 20 epochs for every collection")
    active = args.data_root.resolve() / "experiments/overnight_collections/active.json"
    if args.stop_collection_2:
        if args.resume:
            parser.error("Stop and resume are separate operations")
        if not active.is_file():
            parser.error("No existing overnight queue to update")
        state = read(active)
        stop_collection_two(state)
        print("Collection 2 stopped; queue ready to resume at collection 3.")
        return
    if args.resume:
        if not active.is_file():
            parser.error("No existing overnight queue to resume")
        state = read(active)
        validate_queue(state, args.data_root, args.model_experiment)
        if state.get("state") == "complete":
            print("This overnight queue is already complete.")
            return
    else:
        if active.is_file() and read(active).get("state") != "complete":
            parser.error("An unfinished queue already exists; use --resume")
        state = initialize_queue(args.data_root, args.model_experiment, epochs=args.epochs)
        persist(state)
    run_queue(state)


if __name__ == "__main__":
    main()
