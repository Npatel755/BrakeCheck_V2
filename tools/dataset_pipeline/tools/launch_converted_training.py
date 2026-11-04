"""Publish a validated converted snapshot and launch its single normal trainer."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import psutil

PIPE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPE))

from bcdata.bootstrap import atomic_json
from bcdata.local import local_settings


def main():
    settings = local_settings()
    plan = json.loads(settings.path("reports", "converted_training_plan.json").read_text())
    hold_path = settings.path("reports", "training_hold.json")
    if plan.get("hold") or (hold_path.exists() and json.loads(hold_path.read_text()).get("active")):
        raise RuntimeError("Training is on hold until the remaining conversions are cleared and the user requests a start")
    output = Path(plan["output_run"]).resolve()
    if not output.is_relative_to(settings.path("models", "ufld_baseline").resolve()):
        raise ValueError("Converted run must stay under the local baseline directory")
    snapshot = output / "dataset"
    ready = json.loads((snapshot / "export_marker.json").read_text())
    if ready.get("errors") or ready.get("validation", {}).get("ok") is not True:
        raise ValueError("The converted snapshot has not passed validation")
    raw = (snapshot / "manifest.json").read_bytes()
    manifest_sha = hashlib.sha256(raw).hexdigest()
    hashes = json.loads((snapshot / "hashes.json").read_text())
    if hashes.get("manifest.json") != manifest_sha:
        raise ValueError("Converted manifest changed after validation")
    records = json.loads(raw)
    if any(row["split"] not in ("train", "val") for row in records):
        raise ValueError("Test or held images cannot enter the training snapshot")
    # Supervisors must be stopped too; an idle queue can otherwise restart an
    # experiment while this sole requested trainer is starting.
    blocked = {"bcdata.ufld_baseline", "bcdata.overnight_collections",
               "bcdata.repaired_proposal_experiment", "bcdata.ufld_queue",
               "bcdata.annotation_training", "bcdata.dinov3_training"}
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            command = process.info.get("cmdline") or []
            if "-m" in command and command[command.index("-m") + 1] in blocked:
                raise RuntimeError("Another training worker or experiment supervisor is still active: " + str(process.pid))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    atomic_json(settings.path("reports", "converted_data.json"), {
        "schema": "converted_data:v1", "run": str(output), "manifest_sha256": manifest_sha,
        "train_images": sum(row["split"] == "train" for row in records),
        "val_images": sum(row["split"] == "val" for row in records),
        "cleared_sources_merged": True, "updated_at": now,
    })
    command = [str(PIPE / ".gpu-venv/Scripts/python.exe"), "-u", "-m", "bcdata.ufld_baseline",
               "--prepared-run", str(output), "--warm-start", plan["warm_start"],
               "--epochs", str(plan["epochs"]), "--batch-size", str(plan["batch_size"]),
               "--accumulation", str(plan["accumulation"]), "--learning-rate", str(plan["learning_rate"]),
               "--warmup-steps", str(plan["warmup_steps"]), "--gpu-lock-timeout", "0"]
    if plan.get("evaluate_initial"):
        command.append("--evaluate-initial")
    log_path = output / "training.log"
    with log_path.open("ab", buffering=0) as stream:
        child = subprocess.Popen(command, cwd=PIPE, stdin=subprocess.DEVNULL,
                                 stdout=stream, stderr=subprocess.STDOUT,
                                 env={**os.environ, "BRAKECHECK_DATA_ROOT": str(settings.root)},
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    report = settings.path("reports", "converted_training_preparation.json")
    state = json.loads(report.read_text())
    state.update(phase="gpu_worker_started", worker_pid=child.pid, output_run=str(output),
                 log_path=str(log_path), updated_at=now)
    atomic_json(report, state)
    print(json.dumps(state), flush=True)


if __name__ == "__main__":
    main()
