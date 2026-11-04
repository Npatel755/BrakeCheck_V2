"""CPU-only, resumable hood-crop repair of isolated flagged proposal copies.

Original proposals, conversion labels, review decisions and frozen training
snapshots are read-only. Repairs are OPTIONAL future-experiment proposals.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

import cv2
import numpy as np
from filelock import FileLock

from . import geometry as geometry_module
from .config import load_settings
from .flagged_model_trial import proposal_quality
from .geometry import GeoParams, HOOD_CROP_POLICY, extract_lanes
from .quality import read_rgb
from .teachers.base import file_sha256

SCHEMA = "flagged_proposal_hood_repair:v1"
CPU_THREADS = 2


class FatalRepairError(Exception):
    """A run-wide pin/initialization failure; retrying every image cannot fix it."""


def _signature(path):
    stat = Path(path).stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _verify_pins(signatures, hashes):
    for path, signature in list(signatures.items()):
        current = _signature(path)
        if current != signature:
            if file_sha256(path) != hashes[path]:
                raise FatalRepairError("Frozen teacher/checkpoint/copy inventory changed during repair: " + path.name)
            signatures[path] = current


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _atomic_json(path, value, *, replace=True):
    """Flush before atomic publication; a repair output is never replaced."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            # Same-volume hard-link publication is atomic and cannot clobber an
            # existing result, on either Windows NTFS or POSIX filesystems.
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def needs_repair(proposal):
    """Only the known empty, horizon-clamped failure mode is in this repair."""
    value = proposal.get("geometry")
    if not isinstance(value, dict) or value.get("lanes") != [] or proposal.get("error"):
        return False
    horizon, bottom = value.get("horizon_y"), value.get("valid_bottom")
    if any(isinstance(number, bool) or not isinstance(number, (int, float))
           or not math.isfinite(number) for number in (horizon, bottom)):
        return False
    return 0 <= bottom <= horizon + 10


def _copy_path(run, item):
    copy_id = item.get("copy_id")
    if not isinstance(copy_id, str) or not re.fullmatch(r"copy-[A-Za-z0-9_-]+", copy_id):
        raise ValueError("Invalid isolated copy ID")
    relative = Path(item["image_path"])
    if relative.is_absolute():
        raise ValueError("Replica image path must be relative to the experiment")
    image = (run / relative).resolve()
    if not image.is_relative_to((run / "copies/images").resolve()):
        raise ValueError("Replica image path escapes isolated copies/images")
    return image


def make_cpu_teacher(settings, checkpoint):
    # The CLI also sets this before importing torch. Never acquire the GPU-wide
    # model lock, enumerate CUDA devices, or touch an active training process.
    os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
    os.environ["OMP_NUM_THREADS"] = str(CPU_THREADS)
    os.environ["MKL_NUM_THREADS"] = str(CPU_THREADS)
    import torch
    from .annotation_training import cached_model
    from .teachers.comma_segnet import CommaSegnetTeacher
    torch.set_num_threads(CPU_THREADS)
    cv2.setNumThreads(1)
    model, _ = cached_model()
    saved = torch.load(checkpoint, map_location="cpu", weights_only=False)
    model.load_state_dict(saved["model"], strict=True)
    del saved
    teacher = CommaSegnetTeacher(settings)
    teacher.torch, teacher.device = torch, "cpu"
    teacher.model = model.float().to("cpu").eval()
    return teacher


def _validate_parent(proposal, item, checkpoint_sha):
    if (proposal.get("copy_id") != item["copy_id"]
            or proposal.get("original_image_id") != item["original_image_id"]
            or proposal.get("input_sha256") != item["copy_sha256"]
            or proposal.get("original_sha256") != item["original_sha256"]
            or proposal.get("teacher_provenance", {}).get("checkpoint_sha256") != checkpoint_sha):
        raise ValueError("Original proposal identity/input/checkpoint provenance mismatch")


def _validate_result(result, item, expected, parent):
    provenance = result.get("repair_provenance", {})
    for key, value in expected.items():
        if provenance.get(key) != value:
            raise ValueError("Existing repaired proposal provenance mismatch: " + key)
    for key, value in parent.items():
        if key not in {"geometry", "quality", "repair_provenance"} and result.get(key) != value:
            raise ValueError("Repaired proposal changed original metadata: " + key)
    geometry = result.get("geometry", {})
    if (geometry.get("hood_crop_policy") != HOOD_CROP_POLICY
            or geometry.get("width") != item["width"] or geometry.get("height") != item["height"]
            or not isinstance(geometry.get("lanes"), list)):
        raise ValueError("Invalid repaired geometry or hood policy")
    quality = result.get("quality", {})
    if (type(quality.get("eligible_for_experiment")) is not bool
            or quality.get("human_approved") is not False or result.get("error")):
        raise ValueError("Repaired inference must be a valid pseudo-proposal, never an error negative")
    # Also reject NaN/Infinity in resumed JSON, rather than returning malformed
    # coordinates that a reader could mistake for a reviewed negative.
    json.dumps(result, allow_nan=False)


def repair_run(settings, output, *, teacher_factory=None, limit=None):
    """Repair selected copies. Injected teachers are for CPU-only fixture tests.

    `limit` bounds successful repairs/resumes in this invocation for inspection;
    totals still describe the whole selected batch. An invalid existing repair
    is preserved and reported, and can never be silently overwritten on resume.
    """
    run, data_root = Path(output).resolve(), settings.root.resolve()
    if not run.is_dir() or run.parent != (data_root / "experiments/flagged_teacher").resolve():
        raise ValueError("Use an existing run directly under --data-root/experiments/flagged_teacher")
    if limit is not None and limit < 1:
        raise ValueError("Repair limit must be positive")
    # Avoid local_settings/ensure_layout: this worker creates only its own report,
    # lock and repair outputs inside the supplied, existing experiment directory.
    with FileLock(str(run / "proposal_repair.lock"), timeout=0):
        status_path = run / "proposal_repair.json"
        state = {"schema": SCHEMA, "phase": "validating", "pid": os.getpid(),
                 "started_at": time.time(), "policy": HOOD_CROP_POLICY,
                 "device": "cpu", "precision": "float32", "torch_threads": CPU_THREADS,
                 "opencv_threads": 1, "cuda_visible_devices": "-1", "completed": 0,
                 "total": 0, "repaired": 0, "resumed": 0, "failures": 0,
                 "eligible": 0, "processed": 0, "failure_examples": [],
                 "current_ufld_snapshot_unchanged": True,
                 "scope": "optional future experiments only; original proposals/conversions/reviews unchanged"}

        def update(**values):
            state.update(values, updated_at=time.time())
            _atomic_json(status_path, state)

        def failure(copy_id, error):
            state["failures"] += 1
            if len(state["failure_examples"]) < 100:
                state["failure_examples"].append({"copy_id": copy_id, "error": str(error)[:900]})

        teacher = None
        update()
        try:
            teacher_path, copies_path = run / "teacher/teacher.json", run / "copies.json"
            teacher_sha, copies_sha = file_sha256(teacher_path), file_sha256(copies_path)
            provenance = _read(teacher_path)
            checkpoint = Path(provenance["checkpoint"]).resolve()
            if not checkpoint.is_relative_to((run / "teacher").resolve()):
                raise ValueError("Pinned checkpoint escapes this isolated teacher experiment")
            checkpoint_sha = provenance["checkpoint_sha256"]
            if file_sha256(checkpoint) != checkpoint_sha:
                raise ValueError("Pinned teacher checkpoint changed")
            pinned_signatures = {path: _signature(path) for path in (teacher_path, copies_path, checkpoint)}
            pinned_hashes = {teacher_path: teacher_sha, copies_path: copies_sha, checkpoint: checkpoint_sha}
            settings = deepcopy(settings)
            settings.cfg["teachers"]["device"] = "cpu"
            settings.cfg["teachers"]["amp"] = False
            settings.cfg["teachers"]["comma_segnet"].update(input_width=1024, input_height=512)
            params = GeoParams.from_cfg(settings.get("geometry", {}))
            params.threshold = .5
            geometry_sha = file_sha256(Path(geometry_module.__file__))
            contract = {"policy": HOOD_CROP_POLICY, "geometry_code_sha256": geometry_sha,
                        "geometry_params": asdict(params), "precision": "float32", "device": "cpu",
                        "input_width": 1024, "input_height": 512, "amp": False, "model_forwards": 2,
                        "teacher_provenance_sha256": teacher_sha, "copies_manifest_sha256": copies_sha}
            contract["policy_sha256"] = _digest(contract)
            items = _read(copies_path)["items"]
            identifiers = [item["copy_id"] for item in items]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError("Duplicate isolated copy IDs")
            selected = []
            update(phase="selecting", original_proposals_total=len(items))
            for index, item in enumerate(items):
                try:
                    _copy_path(run, item)
                    path = run / "proposals" / (item["copy_id"] + ".json")
                    raw = path.read_bytes()
                    proposal = json.loads(raw)
                    if needs_repair(proposal):
                        selected.append((item, path, hashlib.sha256(raw).hexdigest()))
                except (KeyError, ValueError, OSError) as error:
                    failure(item.get("copy_id", "unknown"), error)
                if index % 250 == 0:
                    update(scanned=index + 1, total=len(selected))
            update(total=len(selected), original_proposals_total=len(items),
                   checkpoint_sha256=checkpoint_sha, geometry_code_sha256=geometry_sha,
                   policy_sha256=contract["policy_sha256"], phase="repairing")

            for item, parent_path, parent_sha in selected:
                if limit is not None and state["completed"] >= limit:
                    break
                try:
                    if file_sha256(Path(geometry_module.__file__)) != geometry_sha:
                        raise FatalRepairError("Geometry implementation changed during repair; restart for a new policy pin")
                    _verify_pins(pinned_signatures, pinned_hashes)
                    raw = parent_path.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != parent_sha:
                        raise ValueError("Original proposal changed after repair selection")
                    parent = json.loads(raw)
                    _validate_parent(parent, item, checkpoint_sha)
                    image_path = _copy_path(run, item)
                    if file_sha256(image_path) != item["copy_sha256"]:
                        raise ValueError("Replica image changed before inference/resume")
                    expected = {**contract, "original_proposal_sha256": parent_sha,
                                "input_sha256": item["copy_sha256"], "checkpoint_sha256": checkpoint_sha}
                    destination = run / "proposals_hood_repaired" / (item["copy_id"] + ".json")
                    if destination.exists():
                        result = _read(destination)
                        _validate_result(result, item, expected, parent)
                        state["resumed"] += 1
                    else:
                        if teacher is None:
                            update(phase="loading_cpu_teacher")
                            try:
                                teacher = (teacher_factory or make_cpu_teacher)(settings, checkpoint)
                                if file_sha256(checkpoint) != checkpoint_sha:
                                    raise ValueError("Pinned checkpoint changed during CPU model initialization")
                            except Exception as error:
                                raise FatalRepairError("Cannot initialize pinned CPU FP32 teacher: " + str(error)) from error
                            update(phase="repairing")
                        rgb = read_rgb(image_path)
                        if rgb is None or rgb.shape[:2] != (item["height"], item["width"]):
                            raise ValueError("Replica is unreadable or its dimensions changed")
                        prediction = teacher.predict(rgb, item)
                        flipped = teacher.predict(np.ascontiguousarray(rgb[:, ::-1]), item).lane[:, ::-1]
                        for value in (prediction.lane, flipped, prediction.extra.get("my_car"), prediction.extra.get("movable")):
                            if (value is None or value.shape != rgb.shape[:2]
                                    or not np.isfinite(value).all() or np.any(value < 0) or np.any(value > 1)):
                                raise ValueError("Teacher returned invalid or missing probabilities")
                        geometry = extract_lanes(prediction.lane, params, prediction.extra["my_car"], prediction.extra["movable"])
                        geometry["lanes"] = [lane for lane in geometry["lanes"] if lane.get("slot") in {1, 2, 3, 4}]
                        result = {**deepcopy(parent), "geometry": geometry,
                                  "quality": proposal_quality(geometry, prediction.lane, flipped),
                                  "repair_provenance": {**expected, "repaired_at": time.time()}}
                        _validate_result(result, item, expected, parent)
                        # Detect an input/parent/checkpoint change while inference
                        # ran before publishing any apparently valid repair.
                        _verify_pins(pinned_signatures, pinned_hashes)
                        if (file_sha256(image_path) != item["copy_sha256"]
                                or file_sha256(parent_path) != parent_sha):
                            raise ValueError("Pinned input/proposal/checkpoint changed during inference")
                        _atomic_json(destination, result, replace=False)
                        state["repaired"] += 1
                    state["completed"] += 1
                    state["eligible"] += int(result["quality"]["eligible_for_experiment"])
                except (ValueError, OSError, RuntimeError, KeyError, TypeError) as error:
                    failure(item["copy_id"], error)
                state["processed"] += 1
                update(last_copy_id=item["copy_id"])
            if state["processed"] < len(selected):
                phase = "limited"
            else:
                phase = "complete_with_failures" if state["failures"] else "complete"
            update(phase=phase, finished_at=time.time())
            return state
        except Exception as error:
            failure("run", error)
            update(phase="failed", last_error=str(error)[:900], finished_at=time.time())
            raise
        finally:
            if teacher is not None:
                teacher.unload()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Existing isolated flagged teacher run")
    parser.add_argument("--limit", type=int, help="Bound successful repairs/resumes for inspection")
    args = parser.parse_args(argv)
    os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
    os.environ["OMP_NUM_THREADS"] = str(CPU_THREADS)
    os.environ["MKL_NUM_THREADS"] = str(CPU_THREADS)
    cv2.setNumThreads(1)
    settings = load_settings(overrides={"data_root": str(args.data_root.resolve())})
    result = repair_run(settings, args.output, limit=args.limit)
    print(json.dumps({key: value for key, value in result.items() if key != "failure_examples"}))
    return 0 if result["failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
