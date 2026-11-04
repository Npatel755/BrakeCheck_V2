"""One private corrected-proposal UFLDv2 trial after the original 2 -> 3 -> 4 queue.

Waiting and snapshot preparation are CPU-only. No production review, collection,
export, original proposal, running trainer or earlier frozen dataset is changed.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

import cv2
from filelock import FileLock

from . import overnight_collections as ordered
from .bootstrap import atomic_json
from .config import load_settings
from .geometry import GeoParams, HOOD_CROP_POLICY
from .proposal_training_policy import VERSION as TRAINING_GEOMETRY_POLICY
from .teachers.base import file_sha256

SCHEMA = "repaired_proposal_followup:v1"
PIPELINE = Path(__file__).resolve().parents[1]
OPTIONS = {"epochs": 20, "batch_size": 4, "accumulation": 8,
           "learning_rate": .000625, "warmup_steps": 40, "evaluate_initial": True}
CODE_FILES = ("repaired_proposal_experiment.py", "flagged_model_trial.py", "proposal_policy.py", "proposal_training_policy.py",
              "repair_flagged_proposals.py", "geometry.py", "ufld_experiment.py", "ufld_baseline.py")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _signature(path):
    stat = Path(path).stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def queue_identity(value):
    """Mutable progress must not weaken the pinned queue's order or common base."""
    names = ("schema", "run_id", "output", "data_root", "base_run", "base_manifest_sha256",
             "base_validation_manifest_sha256", "base_checkpoint", "base_checkpoint_sha256",
             "source_model_experiment", "source_model_inventory_sha256", "epochs", "code_sha256",
             "training_options", "provisional", "auto_promote", "approved_collections_unchanged")
    return {**{key: value.get(key) for key in names},
            "jobs": [{key: job.get(key) for key in ("id", "collection_id", "output_run", "epochs")}
                     for job in value.get("jobs", [])]}


def _source_identity(value):
    return {key: value.get(key) for key in ("schema", "run_id", "base_run", "base_manifest_sha256",
                                           "inventory_sha256", "max_extra_ratio", "auto_promote")}


def initialize(settings, source_experiment, after_queue, output):
    root = settings.root.resolve()
    source, after, output = (Path(path).resolve() for path in (source_experiment, after_queue, output))
    if source.parent != (root / "experiments/flagged_teacher").resolve() or not source.is_dir():
        raise ValueError("Source must be an existing isolated flagged teacher run")
    if after.parent != (root / "experiments/overnight_collections").resolve() or not after.is_dir():
        raise ValueError("Predecessor must be an existing overnight collection queue")
    if output.parent != (root / "experiments/repaired_proposals").resolve() or output.exists():
        raise ValueError("Output must be a new directory directly under experiments/repaired_proposals")
    queue, experiment = read(after / "queue.json"), read(source / "experiment.json")
    ordered.validate_queue(queue, root, source)
    if queue.get("epochs") != 20 or queue.get("training_options") != {key: value for key, value in OPTIONS.items() if key != "epochs"}:
        raise ValueError("Original queue does not use the common fixed 20-epoch comparison")
    base = Path(queue["base_run"]).resolve()
    if Path(experiment["base_run"]).resolve() != base:
        raise ValueError("Source and ordered queue have different cleared bases")
    if experiment.get("schema") != "flagged_teacher_trial:v1" or experiment.get("auto_promote") is not False:
        raise ValueError("Source is not an isolated provisional teacher experiment")
    inventory, copies, teacher = source / "inventory.json", source / "copies.json", source / "teacher/teacher.json"
    if file_sha256(inventory) != experiment["inventory_sha256"] or experiment["inventory_sha256"] != queue["source_model_inventory_sha256"]:
        raise ValueError("Original frozen split inventory changed")
    provenance = read(teacher)
    checkpoint = Path(provenance["checkpoint"]).resolve()
    if not checkpoint.is_relative_to((source / "teacher").resolve()):
        raise ValueError("Teacher checkpoint is outside the isolated source experiment")
    if file_sha256(checkpoint) != provenance["checkpoint_sha256"]:
        raise ValueError("Teacher checkpoint changed")
    base_rows = read(base / "dataset/manifest.json")
    if ordered.digest([row for row in base_rows if row["split"] == "val"]) != queue["base_validation_manifest_sha256"]:
        raise ValueError("Common validation cohort changed")
    params = GeoParams.from_cfg(settings.get("geometry", {})); params.threshold = .5
    pins = {str(path): file_sha256(path) for path in (
        inventory, copies, teacher, checkpoint, base / "dataset/manifest.json", base / "dataset/hashes.json", base / "best.pt")}
    pins.update({str(PIPELINE / "bcdata" / name): file_sha256(PIPELINE / "bcdata" / name) for name in CODE_FILES})
    repair_contract = {"policy": HOOD_CROP_POLICY, "geometry_code_sha256": pins[str(PIPELINE / "bcdata/geometry.py")],
                       "geometry_params": asdict(params), "precision": "float32", "device": "cpu",
                       "input_width": 1024, "input_height": 512, "amp": False, "model_forwards": 2,
                       "teacher_provenance_sha256": pins[str(teacher)], "copies_manifest_sha256": pins[str(copies)]}
    request = {"schema": SCHEMA, "output": str(output), "data_root": str(root),
               "source_experiment": str(source), "after_queue": str(after), "created_at": time.time(),
               "source_identity": _source_identity(experiment), "queue_identity": queue_identity(queue),
               "base_run": str(base), "base_manifest_sha256": queue["base_manifest_sha256"],
               "base_validation_manifest_sha256": queue["base_validation_manifest_sha256"],
               "base_checkpoint": str(base / "best.pt"), "base_checkpoint_sha256": queue["base_checkpoint_sha256"],
               "teacher_checkpoint_sha256": provenance["checkpoint_sha256"], "pins": pins,
               "geometry_params": asdict(params), "hood_policy": HOOD_CROP_POLICY,
               "repair_policy_sha256": ordered.digest(repair_contract),
               "training_geometry_policy": TRAINING_GEOMETRY_POLICY,
               "proposal_crop_config": settings.get("export.ufld.crop", {}),
               "training_options": OPTIONS, "max_extra_ratio": 1.0, "candidate_only": True,
               "auto_promote": False, "manual_approval_required": False,
               "current_ufld_snapshot_unchanged": True, "approved_collections_unchanged": True,
               "validation_images_used_for_training": 0, "test_images_used_for_training": 0,
               "initialization": "same unchanged cleared-base best.pt; new optimizer and schedule",
               "scope": "one optional corrected-proposal run after all original collection jobs complete"}
    output.mkdir(parents=True)
    shutil.copy2(__file__, output / "orchestrator_code.py")
    atomic_json(output / "request.json", request)
    return request


def _validate_request(request, settings, request_path):
    output, root = Path(request["output"]).resolve(), settings.root.resolve()
    if (request.get("schema") != SCHEMA or Path(request["data_root"]).resolve() != root
            or Path(request_path).resolve() != output / "request.json"
            or output.parent != (root / "experiments/repaired_proposals").resolve()
            or request.get("auto_promote") is not False or request.get("candidate_only") is not True
            or request.get("training_options") != OPTIONS or request.get("max_extra_ratio") != 1.0):
        raise ValueError("Invalid isolated corrected-proposal experiment request")
    if Path(request["source_experiment"]).resolve().parent != (root / "experiments/flagged_teacher").resolve():
        raise ValueError("Pinned source path changed")
    if Path(request["after_queue"]).resolve().parent != (root / "experiments/overnight_collections").resolve():
        raise ValueError("Pinned queue path changed")
    if (request.get("hood_policy") != HOOD_CROP_POLICY
            or request.get("training_geometry_policy") != TRAINING_GEOMETRY_POLICY):
        raise ValueError("Pinned repair policy changed")
    params = GeoParams.from_cfg(settings.get("geometry", {})); params.threshold = .5
    if (asdict(params) != request.get("geometry_params")
            or settings.get("export.ufld.crop", {}) != request.get("proposal_crop_config")):
        raise ValueError("Pinned proposal geometry/crop settings changed")
    return output


class PinVerifier:
    """Hash large immutable inputs once per worker; rehash only on stat drift."""
    def __init__(self, pins):
        self.pins, self.signatures = dict(pins), {}

    def verify(self):
        for filename, expected in self.pins.items():
            path = Path(filename)
            signature = _signature(path)
            if self.signatures.get(filename) != signature:
                if file_sha256(path) != expected:
                    raise ValueError("Pinned input changed: " + filename)
                self.signatures[filename] = signature


def prerequisites(request, *, processes=ordered.matching_processes, lock_held=ordered.lock_held,
                  completed=None, experiment_loader=None, collection_loader=None):
    source, after = Path(request["source_experiment"]), Path(request["after_queue"])
    experiment, queue = read(source / "experiment.json"), read(after / "queue.json")
    if _source_identity(experiment) != request["source_identity"] or queue_identity(queue) != request["queue_identity"]:
        raise ValueError("Pinned source or predecessor queue identity changed")
    path = source / "proposal_repair.json"
    if not path.is_file():
        return False, "waiting_repair", {"detail": "Waiting for the isolated CPU proposal repair"}
    repair = read(path)
    progress = {key: repair.get(key) for key in ("phase", "completed", "total", "failures", "eligible", "updated_at")}
    if repair.get("phase") in {"failed", "complete_with_failures"}:
        raise RuntimeError("CPU repair requires attention; existing repaired outputs are preserved")
    if repair.get("phase") != "complete":
        return False, "waiting_repair", {"repair_progress": progress}
    total = repair.get("total")
    if (type(total) is not int or total <= 0 or repair.get("completed") != total or repair.get("failures") != 0
            or repair.get("policy") != request["hood_policy"]
            or repair.get("checkpoint_sha256") != request["teacher_checkpoint_sha256"]
            or repair.get("policy_sha256") != request["repair_policy_sha256"]
            or repair.get("geometry_code_sha256") != request["pins"][str(PIPELINE / "bcdata/geometry.py")]):
        raise ValueError("Completed repair does not match pinned policy/checkpoint or batch totals")
    if queue.get("state") == "error":
        raise RuntimeError("Original ordered queue requires attention; no follow-up can overtake it")
    first = queue["jobs"][0]
    stopped_two = (first.get("state") == "stopped"
                   and first.get("stop_reason") == "user_requested_advance_to_collection_3"
                   and first.get("completed_20_epochs") is False)
    terminal = all(job.get("state") == "complete" or (index == 0 and stopped_two)
                   for index, job in enumerate(queue["jobs"]))
    if queue.get("state") != "complete" or not terminal:
        return False, "waiting_ordered_queue", {"repair_progress": progress,
            "predecessor_progress": {"state": queue.get("state"), "current_job": queue.get("current_job"),
                                      "jobs": [{"id": job["id"], "state": job.get("state")} for job in queue["jobs"]]}}
    if lock_held(after.parent / "queue.worker.lock"):
        return False, "waiting_ordered_queue_exit", {"repair_progress": progress}
    for job in queue["jobs"]:
        if processes("bcdata.ufld_baseline", job["output_run"]):
            return False, "waiting_predecessor_exit", {"predecessor_job": job["id"]}
    if completed is None:
        from .ufld_queue import completed_run
        completed = completed_run
    if experiment_loader is None:
        from .ufld_experiment import load_experiment_export
        experiment_loader = load_experiment_export
    if collection_loader is None:
        from .ufld_collection_experiment import load_collection_export
        collection_loader = load_collection_export
    evidence = []
    for index, job in enumerate(queue["jobs"]):
        if index == 0 and stopped_two:
            # The user explicitly ended collection 2 early. Its partial run is
            # retained for inspection, never counted as a completed baseline.
            evidence.append({"id": job["id"], "state": "stopped",
                             "stop_reason": job["stop_reason"],
                             "completed_20_epochs": False})
            continue
        run = Path(job["output_run"])
        result = completed(run)
        if not result:
            raise ValueError("Predecessor completion is missing a verified trained checkpoint")
        _, records, _ = (experiment_loader if index == 0 else collection_loader)(run)
        if ordered.digest([row for row in records if row["split"] == "val"]) != request["base_validation_manifest_sha256"]:
            raise ValueError("Predecessor validation differs from the common cleared base")
        config = read(run / "config.json")
        warm = (config.get("initialization") or {}).get("warm_start") or {}
        if (warm.get("sha256") != request["base_checkpoint_sha256"]
                or warm.get("manifest_sha256") != request["base_manifest_sha256"]
                or any(config.get(key) != value for key, value in OPTIONS.items() if key != "evaluate_initial")):
            raise ValueError("Predecessor initialization or training settings differ from the comparison")
        evidence.append({"id": job["id"], "checkpoint_sha256": result["checkpoint_sha256"],
                         "manifest_sha256": file_sha256(run / "dataset/manifest.json")})
    return True, "ready", {"repair_progress": progress, "verified_predecessors": evidence}


def freeze_snapshot(settings, request, *, selector=None, builder=None, loader=None):
    from .flagged_model_trial import assigned_split_inventory, selected_extras
    from .proposal_policy import preferred_proposal_bytes
    from .ufld_experiment import build_experiment, load_experiment_export
    selector, builder, loader = selector or selected_extras, builder or build_experiment, loader or load_experiment_export
    output, source = Path(request["output"]), Path(request["source_experiment"])
    run, plan_path = output / "ufld_run", output / "snapshot_inputs.json"
    plan_pin = output / "snapshot_inputs.sha256.json"
    if plan_path.exists() and (not plan_pin.is_file() or read(plan_pin).get("sha256") != file_sha256(plan_path)):
        raise ValueError("Frozen snapshot selection changed or its atomic preparation was interrupted")
    if not run.exists():
        partials = list(output.glob("ufld_run.preparing-*"))
        if partials:
            raise RuntimeError("Interrupted snapshot staging is preserved for inspection: " + partials[0].name)
        if plan_path.exists():
            plan = read(plan_path)
            if plan.get("request_sha256") != file_sha256(output / "request.json"):
                raise ValueError("Frozen snapshot selection belongs to a different request")
            extras = plan["extras"]
        else:
            extras, selection = selector(settings, source)
            if selection.get("training_geometry_policy") != request["training_geometry_policy"]:
                raise ValueError("Selected proposal geometry policy differs from the immutable request")
            chosen = {row["record"]["image_id"] for row in extras}
            proposals = []
            for item in read(source / "copies.json")["items"]:
                if item["original_image_id"] in chosen:
                    raw = preferred_proposal_bytes(source, item["copy_id"])
                    proposal = json.loads(raw)
                    proposals.append({"copy_id": item["copy_id"], "original_image_id": item["original_image_id"],
                                      "proposal_sha256": hashlib.sha256(raw).hexdigest(),
                                      "hood_repaired": "repair_provenance" in proposal})
            # A frozen plan is written before the heavyweight snapshot build.
            # A restart reuses its exact descriptors instead of changing review
            # decisions or selecting a different proposal set midway through.
            plan = {"request_sha256": file_sha256(output / "request.json"), "extras": extras,
                    "selection": selection, "selected_proposals": proposals,
                    "repair_completion": read(source / "proposal_repair.json"), "frozen_at": time.time()}
            atomic_json(plan_path, plan)
            atomic_json(plan_pin, {"sha256": file_sha256(plan_path)})
        inventory, excluded = assigned_split_inventory(read(source / "inventory.json")["split_inventory"])
        atomic_json(output / "selection.json", {**plan["selection"], "unassigned_rejected_inputs": excluded,
                    "selected_proposals": plan["selected_proposals"], "manual_approval_required": False,
                    "snapshot_inputs_sha256": file_sha256(plan_path)})
        builder(request["base_run"], run, extras, split_inventory=inventory,
                teacher=read(source / "teacher/teacher.json"), max_extra_ratio=request["max_extra_ratio"])
    snapshot, records, _ = loader(run)
    base = read(Path(request["base_run"]) / "dataset/manifest.json")
    report = read(snapshot / "experiment.json")
    if (records[:len(base)] != base or report.get("base_manifest_sha256") != request["base_manifest_sha256"]
            or ordered.digest([row for row in records if row["split"] == "val"]) != request["base_validation_manifest_sha256"]
            or any(row["split"] != "train" for row in records[len(base):])):
        raise ValueError("Corrected proposal snapshot changed cleared data or held-out validation")
    plan = read(plan_path)
    selected = {row["original_image_id"]: row for row in plan["selected_proposals"]}
    accepted = [row["image_id"] for row in records[len(base):]]
    return {**report, "manifest_sha256": file_sha256(snapshot / "manifest.json"),
            "hash_inventory_sha256": file_sha256(snapshot / "hashes.json"),
            "snapshot_inputs_sha256": file_sha256(plan_path),
            "training_geometry_policy": request["training_geometry_policy"],
            "selection_excluded_counts": plan["selection"]["excluded"],
            "selected_repaired_proposals": sum(row["hood_repaired"] for row in selected.values()),
            "selected_original_proposals": sum(not row["hood_repaired"] for row in selected.values()),
            "trained_repaired_proposals": sum(selected[image_id]["hood_repaired"] for image_id in accepted),
            "trained_original_proposals": sum(not selected[image_id]["hood_repaired"] for image_id in accepted)}


def training_command(request, python):
    return ordered.training_command({"base_checkpoint": request["base_checkpoint"]},
                                    {"output_run": str(Path(request["output"]) / "ufld_run"), "epochs": 20}, python)


def _verify_training_initialization(request, run):
    config = read(Path(run) / "config.json")
    warm = (config.get("initialization") or {}).get("warm_start") or {}
    if (warm.get("sha256") != request["base_checkpoint_sha256"]
            or warm.get("manifest_sha256") != request["base_manifest_sha256"]
            or any(config.get(key) != value for key, value in OPTIONS.items() if key != "evaluate_initial")):
        raise ValueError("Follow-up training initialization/settings differ from the common cleared base")


def worker(settings, request_path, *, python=None, poll_seconds=15, sleep=time.sleep, max_ticks=None,
           checker=prerequisites, freezer=freeze_snapshot, processes=ordered.matching_processes,
           launcher=ordered.launch, completed=None):
    request_path = Path(request_path).resolve()
    request, request_sha = read(request_path), file_sha256(request_path)
    output = _validate_request(request, settings, request_path)
    python = Path(python) if python else PIPELINE / ".gpu-venv/Scripts/python.exe"
    if not python.is_file() or not 10 <= poll_seconds <= 60:
        raise ValueError("Use the existing GPU Python environment and a 10–60 second poll interval")
    if completed is None:
        from .ufld_queue import completed_run
        completed = completed_run
    cv2.setNumThreads(1)
    verifier = PinVerifier({**request["pins"], str(request_path): request_sha})
    with FileLock(str(output / "worker.lock"), timeout=0):
        status_path, run = output / "status.json", output / "ufld_run"
        state = read(status_path) if status_path.exists() else {}
        if state and state.get("request_sha256") != request_sha:
            raise ValueError("Follow-up status belongs to a different immutable request")
        state = {"schema": SCHEMA, "request_sha256": request_sha, "output": str(output),
                 "source_experiment": request["source_experiment"], "after_queue": request["after_queue"],
                 "state": "waiting", "phase": "validating", "launches": 0, "epochs": 20,
                 "candidate_only": True, "auto_promote": False, "manual_approval_required": False,
                 "current_ufld_snapshot_unchanged": True, "approved_collections_unchanged": True,
                 "initialization": request["initialization"], **state}

        def update(**changes):
            state.update(changes, pid=os.getpid(), updated_at=time.time())
            atomic_json(status_path, state)

        child, gated, snapshot_checked, ticks = None, False, False, 0
        try:
            with ordered.keep_awake(state):
                while True:
                    ticks += 1
                    verifier.verify()
                    if not gated:
                        ready, phase, details = checker(request)
                        if not ready:
                            update(state="waiting", phase=phase, **details)
                            if max_ticks is not None and ticks >= max_ticks: return state
                            sleep(poll_seconds); continue
                        gated = True
                        update(state="waiting", phase="predecessors_verified", **details)
                    if not snapshot_checked:
                        if shutil.disk_usage(output).free < ordered.RESERVE_BYTES:
                            update(state="waiting", phase="waiting_space", detail="Waiting for 28 GiB reserve; no files are deleted")
                            if max_ticks is not None and ticks >= max_ticks: return state
                            sleep(poll_seconds); continue
                        update(state="building", phase="building_training_snapshot")
                        report = freezer(settings, request)
                        verifier.verify()
                        snapshot_checked = True
                        update(dataset=report)
                    status = read(run / "status.json") if (run / "status.json").exists() else {}
                    alive = processes("bcdata.ufld_baseline", run)
                    if child is not None and child.poll() is None:
                        alive = alive or [{"pid": child.pid}]
                    if status.get("phase") == "trained_awaiting_review" and not alive:
                        _verify_training_initialization(request, run)
                        result = completed(run)
                        if not result: raise ValueError("Follow-up completion has no verified checkpoint")
                        comparison = {"schema": SCHEMA, "base_manifest_sha256": request["base_manifest_sha256"],
                            "common_validation_manifest_sha256": request["base_validation_manifest_sha256"],
                            "warm_start_checkpoint_sha256": request["base_checkpoint_sha256"],
                            "base": read(Path(request["base_run"]) / "status.json"), "result": result,
                            "dataset": state["dataset"], "auto_promote": False,
                            "test_images_used_for_training": 0, "validation_images_used_for_training": 0,
                            "limitation": "Equal epochs are not a matched-budget causal comparison; candidate remains unpromoted."}
                        atomic_json(output / "comparison.json", comparison)
                        update(state="complete", phase="complete_awaiting_comparison_review", training=result, finished_at=time.time())
                        return state
                    if alive:
                        update(state="running", phase="training_ufld", trainer_processes=alive, training=status)
                    else:
                        launches = state.get("launches", 0)
                        if launches >= 3:
                            raise RuntimeError("Two checkpoint resume attempts exhausted; snapshots and checkpoints are preserved")
                        if launches and not (run / "last.pt").is_file():
                            raise RuntimeError("Previous launch exited without a resume checkpoint; snapshot preserved for inspection")
                        if (run / "last.pt").is_file():
                            _verify_training_initialization(request, run)
                        if shutil.disk_usage(output).free < ordered.RESERVE_BYTES:
                            update(state="waiting", phase="waiting_space")
                            if max_ticks is not None and ticks >= max_ticks: return state
                            sleep(poll_seconds); continue
                        command = training_command(request, python)
                        verifier.verify()
                        update(state="launching", phase="launching_training", launches=launches + 1,
                               resume_attempts=launches, launch_intent={"command": command, "at": time.time()})
                        child = launcher(command, output / "training", settings.root)
                        update(state="running", phase="training_ufld", last_launch_pid=child.pid,
                               last_launch_at=time.time(), training=status)
                    if max_ticks is not None and ticks >= max_ticks: return state
                    sleep(poll_seconds)
        except BaseException as error:
            update(state="error", phase="error", error=str(error)[:1800], error_type=type(error).__name__)
            raise
        finally:
            update()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--source-experiment", type=Path, required=True)
    parser.add_argument("--after-queue", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    settings = load_settings(overrides={"data_root": str(args.data_root.resolve())})
    path = args.output.resolve() / "request.json"
    if args.resume:
        request = read(path)
        if (Path(request["source_experiment"]).resolve() != args.source_experiment.resolve()
                or Path(request["after_queue"]).resolve() != args.after_queue.resolve()):
            parser.error("Resume targets differ from the immutable follow-up request")
    else:
        initialize(settings, args.source_experiment, args.after_queue, args.output)
    worker(settings, path)


if __name__ == "__main__":
    main()
