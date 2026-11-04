"""Durable isolated teacher -> flagged replicas -> cleared-plus-pseudo UFLDv2 trial.

The main registry is opened in SQLite read-only mode, once for a new snapshot.
No production queue, annotation, export or model status is written by this worker.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

import cv2
import numpy as np

from .bootstrap import atomic_json
from .local import local_settings
from .proposal_policy import proposal_approval_policy, preferred_proposal_bytes
from .proposal_training_policy import VERSION as TRAINING_GEOMETRY_POLICY, training_geometry_hold
from .quality import read_rgb
from .teachers.base import file_sha256

SCHEMA = "flagged_teacher_trial:v1"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


class ReadOnlyRegistry:
    def __init__(self, path):
        self.conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only=ON")

    def images(self, where="1=1", params=()):
        return self.conn.execute("SELECT * FROM images WHERE " + where + " ORDER BY image_id", params).fetchall()

    def annotation(self, image_id, kind, producer=None, model_version=None):
        query = "SELECT * FROM annotations WHERE image_id=? AND kind=?"
        params = [image_id, kind]
        for column, value in (("producer", producer), ("model_version", model_version)):
            if value:
                query += " AND " + column + "=?"
                params.append(value)
        return self.conn.execute(query + " ORDER BY created_at DESC,id DESC LIMIT 1", params).fetchone()

    def close(self):
        self.conn.close()


def _table_fingerprints(connection):
    tables = ["images", "annotations", "qc", "reviews", "review_versions", "review_events",
              "source_conversion_checks", "source_validation_groups", "review_deferrals"]
    result = {}
    for table in tables:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        hasher, count = hashlib.sha256(), 0
        for row in connection.execute("SELECT * FROM " + table + " ORDER BY rowid"):
            hasher.update(json.dumps(dict(row), sort_keys=True, separators=(",", ":")).encode())
            hasher.update(b"\n"); count += 1
        result[table] = {"rows": count, "sha256": hasher.hexdigest()}
    return result


def freeze_inventory(settings, output, base_run, parent_checkpoint, max_extra_ratio=1.0):
    """Freeze current UI-equivalent pending flags, splits and original geometry."""
    output, base_run, parent_checkpoint = map(lambda p: Path(p).resolve(), (output, base_run, parent_checkpoint))
    if output.exists():
        raise FileExistsError("Use --resume for an existing experiment")
    base = read_json(base_run / "dataset/manifest.json")
    output.mkdir(parents=True)
    source = ReadOnlyRegistry(settings.db_path)
    try:
        with sqlite3.connect(output / "registry_snapshot.sqlite") as destination:
            source.conn.backup(destination)
    finally:
        source.close()
    reg = ReadOnlyRegistry(output / "registry_snapshot.sqlite")
    try:
        images = {r["image_id"]: dict(r) for r in reg.images()}
        latest_source, latest_geometry = {}, {}
        for row in reg.conn.execute("SELECT * FROM annotations WHERE kind='source_label' OR (kind='geometry' AND producer='geometry') ORDER BY created_at,id"):
            (latest_source if row["kind"] == "source_label" else latest_geometry)[row["image_id"]] = dict(row)
        items = []
        for row in reg.conn.execute("SELECT * FROM source_conversion_checks WHERE state='flagged' ORDER BY image_id"):
            check = dict(row)
            image = images[check["image_id"]]
            annotation, geometry = latest_source.get(image["image_id"]), latest_geometry.get(image["image_id"])
            if (image["status"] != "curated" or image["review_status"] in {"approved", "corrected", "rejected"}
                    or not annotation or not geometry or check["source_id"] != annotation["id"]
                    or check["geometry_id"] != geometry["id"]
                    or check["created_at"] < max(annotation["created_at"], geometry["created_at"])):
                continue
            raw = Path(geometry["path"]).read_bytes()
            items.append({"record": image, "original_conversion_check": check,
                          "source_annotation": annotation, "geometry_annotation": geometry,
                          "original_geometry": json.loads(raw),
                          "original_geometry_sha256": hashlib.sha256(raw).hexdigest(),
                          "original_geometry_bytes_hex": raw.hex()})
        if not items:
            raise ValueError("No current pending flagged images to copy")
        inventory = [{**r, "source_sha256": r["sha256"]} for r in images.values()]
        atomic_json(output / "inventory.json", {"schema": "flagged_trial_inventory:v1", "items": items,
                    "split_inventory": inventory, "main_table_fingerprints": _table_fingerprints(reg.conn),
                    "registry_snapshot_sha256": file_sha256(output / "registry_snapshot.sqlite")})
    finally:
        reg.close()
    config = {"schema": SCHEMA, "run_id": output.name, "phase": "prepared", "started_at": time.time(),
              "base_run": str(base_run), "base_manifest_sha256": file_sha256(base_run / "dataset/manifest.json"),
              "parent_checkpoint": str(parent_checkpoint), "parent_checkpoint_sha256": file_sha256(parent_checkpoint),
              "base_train_images": sum(r["split"] == "train" for r in base),
              "base_val_images": sum(r["split"] == "val" for r in base),
              "flagged_images": len(items), "flagged_by_split": dict(Counter(i["record"]["split"] for i in items)),
              "inventory_sha256": file_sha256(output / "inventory.json"), "max_extra_ratio": max_extra_ratio,
              "candidate_only": True, "auto_promote": False, "test_images_used_for_training": 0,
              "originals_write_policy": "read-only SQLite snapshot; no original annotation/export writes",
              "copy_policy": "independent full-resolution JPEG q97 replicas; original bytes stay unchanged",
              "teacher_target_policy": "original human semantic masks from cleared base; no pseudo-label self-fitting",
              "extra_target_policy": "image-only teacher paint; no model-empty negatives; train-only QC-passing additions"}
    atomic_json(output / "experiment.json", config)
    return config


def materialize_copies(output, update):
    output = Path(output).resolve()
    inventory = read_json(output / "inventory.json")
    items = []
    for index, original in enumerate(inventory["items"]):
        row = original["record"]
        copy_id = "copy-" + row["image_id"]
        relative = "copies/images/" + copy_id + ".jpg"
        metadata = output / "copies/metadata" / (copy_id + ".json")
        destination = output / relative
        if metadata.exists():
            item = read_json(metadata)
            if not destination.is_file() or file_sha256(destination) != item["copy_sha256"]:
                raise ValueError("Existing isolated copy changed: " + copy_id)
            geometry_copy = output / item["original_geometry_path"]
            if (not geometry_copy.is_file() or file_sha256(geometry_copy) != item["original_geometry_sha256"]
                    or item["original_geometry_sha256"] != original["original_geometry_sha256"]):
                raise ValueError("Frozen original geometry copy changed: " + copy_id)
            items.append(item)
            continue
        if shutil.disk_usage(output).free < 5 * 2**30:
            raise RuntimeError("Less than5GiB free; copied originals preserved; free space then --resume")
        source = Path(row["path"])
        if file_sha256(source) != row["sha256"]:
            raise ValueError("Original image changed since frozen registry: " + row["image_id"])
        rgb = read_rgb(source)
        if rgb is None:
            raise ValueError("Original image unreadable: " + row["image_id"])
        h, w = rgb.shape[:2]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix.lower() in {".jpg", ".jpeg"}:
            shutil.copy2(source, destination)
            transform = "identity_byte_copy"
        else:
            ok, encoded = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 97])
            if not ok:
                raise ValueError("Cannot encode isolated replica")
            temporary = destination.with_suffix(".tmp")
            temporary.write_bytes(encoded.tobytes()); os.replace(temporary, destination)
            transform = "full_resolution_jpeg_q97"
        geometry_path = "copies/original_annotations/" + copy_id + ".json"
        annotation_path = output / geometry_path
        annotation_path.parent.mkdir(parents=True, exist_ok=True)
        annotation_path.write_bytes(bytes.fromhex(original["original_geometry_bytes_hex"]))
        copied_sha = file_sha256(destination)
        item = {"copy_id": copy_id, "original_image_id": row["image_id"], "split": row["split"],
                "source": row["source"], "session_id": row["session_id"], "width": w, "height": h,
                "image_path": relative, "original_geometry_path": geometry_path,
                "original_geometry_sha256": original["original_geometry_sha256"],
                "original_sha256": row["sha256"], "copy_sha256": copied_sha,
                "copy_provenance": {"original_sha256": row["sha256"], "copy_sha256": copied_sha,
                                    "transform": transform, "width": w, "height": h},
                "record": {**row, "source_sha256": row["sha256"]}}
        atomic_json(metadata, item)
        items.append(item)
        if index % 25 == 0:
            update(phase="copying", copies_done=len(items), copies_total=len(inventory["items"]))
        if index % 250 == 0:
            atomic_json(output / "copies.json", {"schema": "flagged_model_copies:v1", "items": items})
    atomic_json(output / "copies.json", {"schema": "flagged_model_copies:v1", "items": items})
    update(copies_done=len(items), copies_total=len(items))
    return items


def proposal_quality(geometry, paint, flipped_paint, *, min_confidence=.8, min_consistency=.65):
    """Conservative image-only checks; no source conversion is used as a target."""
    h, w = paint.shape
    reasons, lanes = [], []
    for lane in geometry.get("lanes", []):
        if lane.get("slot") not in {1, 2, 3, 4}:
            continue
        lanes.append(lane)
        points = np.asarray([p[:2] for p in lane.get("points", [])], dtype=float)
        if (points.ndim != 2 or points.shape[1:] != (2,) or len(points) < 2
                or not np.isfinite(points).all() or np.any(points < 0)
                or np.any(points[:, 0] >= w) or np.any(points[:, 1] >= h)):
            reasons.append("invalid_or_outside_image_points")
        if float(lane.get("mean_confidence", 0)) < min_confidence:
            reasons.append("low_paint_confidence")
        if set(lane.get("flags", [])) & {"crossing", "implausible_curve", "invalid_coordinates", "merge_or_split"}:
            reasons.append("ambiguous_lane_geometry")
        if float(lane.get("y_bottom", 0)) - float(lane.get("y_top", 0)) < .08 * h:
            reasons.append("short_observed_paint_span")
    if not lanes:
        reasons.append("no_teacher_paint_not_a_verified_negative")
    a, b = paint >= .5, flipped_paint >= .5
    union = int(np.count_nonzero(a | b))
    consistency = float(np.count_nonzero(a & b) / union) if union else 0.
    if consistency < min_consistency:
        reasons.append("flip_prediction_disagreement")
    if len({lane["slot"] for lane in lanes}) != len(lanes):
        reasons.append("duplicate_lane_slots")
    return {"eligible_for_experiment": not reasons, "reasons": sorted(set(reasons)),
            "paint_flip_iou": round(consistency, 5), "min_lane_confidence": min_confidence,
            "min_flip_iou": min_consistency, "label_origin": "teacher_pseudo", "human_approved": False}


def generate_proposals(settings, output, update):
    from filelock import FileLock
    import torch
    from .annotation_training import cached_model
    from . import geometry as geometry_module
    from .geometry import GeoParams, HOOD_CROP_POLICY, extract_lanes
    from .teachers.comma_segnet import CommaSegnetTeacher

    output = Path(output).resolve()
    items = read_json(output / "copies.json")["items"]
    provenance = read_json(output / "teacher/teacher.json")
    checkpoint = Path(provenance["checkpoint"])
    if file_sha256(checkpoint) != provenance["checkpoint_sha256"]:
        raise ValueError("Selected teacher checkpoint changed")
    settings.cfg["teachers"].update(device="cuda", amp=True)
    settings.cfg["teachers"]["comma_segnet"].update(input_width=1024, input_height=512)
    torch.set_num_threads(4); cv2.setNumThreads(1)
    completed, eligible, failed = 0, 0, 0
    with FileLock(str(settings.path("logs", "large-models.lock")), timeout=-1):
        pipeline_provenance = {
            "geometry_code_sha256": file_sha256(Path(geometry_module.__file__)),
            "hood_crop_policy": HOOD_CROP_POLICY, "device": "cuda", "amp": True,
            "precision": "bfloat16" if torch.cuda.is_bf16_supported() else "float16",
            "model_input": [1024, 512], "input_range": [0, 255],
            "torch": str(torch.__version__), "opencv": cv2.__version__}
        model, _ = cached_model()
        saved = torch.load(checkpoint, map_location="cpu", weights_only=False)
        model.load_state_dict(saved["model"], strict=True)
        teacher = CommaSegnetTeacher(settings)
        teacher.torch, teacher.device, teacher.model = torch, "cuda", model.to("cuda").eval()
        params = GeoParams.from_cfg(settings.get("geometry", {}))
        params.threshold = .5
        try:
            for item in items:
                path = output / "proposals" / (item["copy_id"] + ".json")
                if path.exists():
                    old = read_json(path)
                    if (old.get("teacher_provenance", {}).get("checkpoint_sha256") != provenance["checkpoint_sha256"]
                            or old.get("input_sha256") != item["copy_sha256"]):
                        raise ValueError("Existing proposal provenance changed")
                    completed += 1; eligible += int(old["quality"]["eligible_for_experiment"])
                    failed += int(bool(old.get("error")))
                    continue
                try:
                    image_path = output / item["image_path"]
                    if file_sha256(image_path) != item["copy_sha256"]:
                        raise ValueError("Replica image changed")
                    rgb = read_rgb(image_path)
                    if rgb is None:
                        raise ValueError("Replica image unreadable")
                    prediction = teacher.predict(rgb, item)
                    flipped = teacher.predict(np.ascontiguousarray(rgb[:, ::-1]), item).lane[:, ::-1]
                    geometry = extract_lanes(prediction.lane, params, prediction.extra.get("my_car"), prediction.extra.get("movable"))
                    geometry["lanes"] = [lane for lane in geometry["lanes"] if lane.get("slot") in {1, 2, 3, 4}]
                    quality = proposal_quality(geometry, prediction.lane, flipped)
                    proposal = {"geometry": geometry, "quality": quality}
                except (ValueError, OSError) as error:
                    failed += 1
                    proposal = {"geometry": {"width": item["width"], "height": item["height"], "lanes": []},
                                "quality": {"eligible_for_experiment": False, "reasons": ["prediction_error"]},
                                "error": str(error)[:700]}
                proposal.update(copy_id=item["copy_id"], original_image_id=item["original_image_id"],
                                input_sha256=item["copy_sha256"], original_sha256=item["original_sha256"],
                                pipeline_provenance=pipeline_provenance,
                                teacher_provenance={"name": provenance["name"], "version": provenance["version"],
                                    "checkpoint_sha256": provenance["checkpoint_sha256"], "source_guided": False,
                                    "reference_inputs": [], "model_forwards": 2,
                                    "full_provenance_path": "teacher/teacher.json"})
                atomic_json(path, proposal)
                completed += 1; eligible += int(proposal["quality"]["eligible_for_experiment"])
                if completed % 10 == 0:
                    update(phase="predicting", proposals_done=completed, proposals_total=len(items),
                           quality_pass=eligible, prediction_failures=failed)
        finally:
            teacher.unload()
    update(phase="proposals_complete", proposals_done=completed, proposals_total=len(items),
           quality_pass=eligible, prediction_failures=failed)


def selected_extras(settings, output):
    output = Path(output).resolve()
    decisions = {}
    review_db = output / "review.sqlite"
    if review_db.exists():
        conn = sqlite3.connect(review_db.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            decisions = {row["copy_id"]: dict(row) for row in conn.execute("SELECT * FROM decisions")}
        finally:
            conn.close()
    extras, excluded = [], Counter()
    automatically_selected = human_override_selected = 0
    crop_cfg = settings.get("export.ufld.crop", {})
    for item in read_json(output / "copies.json")["items"]:
        if item["split"] != "train":
            excluded["held_out_comparison_only"] += 1; continue
        decision = decisions.get(item["copy_id"], {})
        if decision.get("action") in {"exclude", "skip"}:
            excluded["experimental_reviewer_excluded_or_skipped"] += 1; continue
        proposal_bytes = preferred_proposal_bytes(output, item["copy_id"])
        proposal = json.loads(proposal_bytes)
        policy = proposal_approval_policy(proposal, width=item.get("width"), height=item.get("height"))
        training_geometry = training_geometry_hold(proposal.get("geometry"), item.get("width"), item.get("height"))
        automatic = (proposal.get("quality", {}).get("eligible_for_experiment") is True
                     and training_geometry["eligible_for_training"])
        human_override = (decision.get("action") == "approve"
                          and decision.get("proposal_sha256") == hashlib.sha256(proposal_bytes).hexdigest())
        if not policy["approval_allowed"]:
            excluded["proposal_quality_failed"] += 1; continue
        if set(training_geometry["reasons"]) - {"probable_same_boundary_multiple_slots"}:
            excluded["proposal_quality_failed"] += 1; continue
        if not training_geometry["eligible_for_training"] and not human_override:
            excluded["proposal_geometry_ambiguous"] += 1; continue
        if not (automatic or human_override):
            excluded["proposal_quality_failed"] += 1; continue
        if automatic:
            automatically_selected += 1
        else:
            human_override_selected += 1
        extras.append({"record": item["record"], "image_path": str(output / item["image_path"]),
                       "image_sha256": item["copy_sha256"],
                       "copy_provenance": item["copy_provenance"] if item["copy_sha256"] != item["original_sha256"] else None,
                       "geometry": proposal["geometry"],
                       "crop_config": crop_cfg.get(item["source"], crop_cfg.get("default", {}))})
    return extras, {"decisions": decisions, "excluded": dict(excluded), "candidate_train_extras": len(extras),
                    "manual_approval_required": False, "automatically_selected": automatically_selected,
                    "human_override_selected": human_override_selected,
                    "training_geometry_policy": TRAINING_GEOMETRY_POLICY,
                    "frozen_at": time.time()}


def assigned_split_inventory(records):
    """Project the frozen registry to its assigned splits without changing it.

    Rejected inputs can legitimately predate split assignment (e.g. black frames).
    They never participated in training or evaluation. Keep every assigned row,
    including held-out/rejected rows, and fail for any other missing split so an
    eligible image cannot silently lose its leakage protection.
    """
    assigned, excluded = [], []
    for record in records:
        if record.get("split") in {"train", "val", "test"}:
            assigned.append(record)
        elif record.get("split") in (None, "") and record.get("status") == "rejected":
            excluded.append({"image_id": record["image_id"], "status": "rejected",
                             "reason": "rejected_before_split_assignment"})
        else:
            raise ValueError("Missing or invalid split for eligible inventory image: "
                             + str(record.get("image_id")))
    return assigned, excluded


def recover_partial_teacher(output):
    """Preserve an interrupted, uncommitted preparation before a fresh attempt."""
    output = Path(output).resolve()
    teacher = output / "teacher"
    if (teacher / "experiment.json").exists():
        return None
    if not any((teacher / name).exists() for name in ("source_cohort", "cohort", "parent_initial.pt")):
        return None
    archive = output / ("teacher-attempt-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    if not teacher.resolve().is_relative_to(output) or not archive.resolve().is_relative_to(output):
        raise ValueError("Partial teacher paths must stay inside the isolated experiment")
    teacher.rename(archive)
    return str(archive)


def preservation_report(settings, output):
    """Report external drift without ever undoing legitimate concurrent reviews."""
    output = Path(output).resolve()
    frozen = read_json(output / "inventory.json")
    registry = ReadOnlyRegistry(settings.db_path)
    try:
        now = _table_fingerprints(registry.conn)
        differences = [name for name, value in frozen["main_table_fingerprints"].items() if now.get(name) != value]
        changed_geometry = []
        for item in frozen["items"]:
            path = Path(item["geometry_annotation"]["path"])
            if not path.is_file() or file_sha256(path) != item["original_geometry_sha256"]:
                changed_geometry.append(item["record"]["image_id"])
        report = {"main_registry_open_mode": "ro/query_only", "main_registry_writes_by_experiment": 0,
                  "original_annotation_writes_by_experiment": 0, "checked_at": time.time(),
                  "table_fingerprint_matches": not differences, "tables_with_external_changes": differences,
                  "flagged_original_geometry_files_checked": len(frozen["items"]),
                  "original_geometry_matches": not changed_geometry, "externally_changed_geometry_ids": changed_geometry,
                  "interpretation": "Drift may be concurrent human review/import work. Never restore or overwrite original records."}
        atomic_json(output / "preservation.json", report)
        return report
    finally:
        registry.close()


def run_pipeline(settings, output):
    from filelock import FileLock
    from .flagged_teacher_experiment import prepare_teacher, train_teacher
    from .ufld_experiment import build_experiment, load_experiment_export

    output = Path(output).resolve()
    state = read_json(output / "experiment.json")
    if file_sha256(output / "inventory.json") != state["inventory_sha256"]:
        raise ValueError("Frozen inventory changed")
    if file_sha256(Path(state["base_run"]) / "dataset/manifest.json") != state["base_manifest_sha256"]:
        raise ValueError("Cleared base changed")
    if not (output / "teacher/experiment.json").exists() and file_sha256(state["parent_checkpoint"]) != state["parent_checkpoint_sha256"]:
        raise ValueError("Pinned teacher parent changed before preparation")
    def update(**changes):
        state.update(changes, pid=os.getpid(), updated_at=time.time())
        atomic_json(output / "experiment.json", state)
    with FileLock(str(output / "worker.lock"), timeout=0):
        try:
            teacher_dir = output / "teacher"
            if not (teacher_dir / "experiment.json").exists():
                archived = recover_partial_teacher(output)
                if archived:
                    update(archived_partial_teacher=archived)
                update(phase="preparing_teacher")
                registry = ReadOnlyRegistry(output / "registry_snapshot.sqlite")
                try:
                    prepare_teacher(settings, registry, Path(state["base_run"]), teacher_dir,
                                    Path(state["parent_checkpoint"]),
                                    progress=lambda changes: update(phase="preparing_teacher", teacher_progress=changes))
                finally:
                    registry.close()
            if not (teacher_dir / "teacher.json").exists():
                update(phase="training_teacher")
                def teacher_progress(changes):
                    update(phase="training_teacher", teacher_progress=changes)
                train_teacher(teacher_dir, progress=teacher_progress)
            if not (output / "copies.json").exists() or len(read_json(output / "copies.json")["items"]) != state["flagged_images"]:
                update(phase="copying")
                materialize_copies(output, update)
            if state.get("proposals_done") != state["flagged_images"]:
                update(phase="predicting")
                generate_proposals(settings, output, update)
            ufld_run = output / "ufld_run"
            if not ufld_run.exists():
                update(phase="building_training_snapshot", error=None, error_type=None,
                       manual_approval_required=False)
                extras, selection = selected_extras(settings, output)
                split_inventory, unassigned_rejections = assigned_split_inventory(
                    read_json(output / "inventory.json")["split_inventory"])
                selection.update(manual_approval_required=False,
                                 unassigned_rejected_inputs=unassigned_rejections)
                atomic_json(output / "selection.json", selection)
                report = build_experiment(state["base_run"], ufld_run, extras,
                    split_inventory=split_inventory,
                    teacher=read_json(teacher_dir / "teacher.json"), max_extra_ratio=state["max_extra_ratio"])
                update(training_dataset=report, manual_approval_required=False,
                       error=None, error_type=None)
            else:
                load_experiment_export(ufld_run)
            training_status = read_json(ufld_run / "status.json") if (ufld_run / "status.json").exists() else {}
            if training_status.get("phase") != "trained_awaiting_review":
                update(phase="training_ufld", ufld_run=str(ufld_run))
                command = [sys.executable, "-m", "bcdata.ufld_baseline", "--experimental-data",
                           "--status-report", str(output / "ufld_training.json"), "--epochs", "20",
                           "--batch-size", "4", "--accumulation", "8", "--learning-rate", "0.000625",
                           "--warmup-steps", "40", "--gpu-lock-timeout", "-1"]
                if (ufld_run / "last.pt").exists():
                    command += ["--resume-run", str(ufld_run)]
                else:
                    command += ["--prepared-run", str(ufld_run), "--warm-start", str(Path(state["base_run"]) / "best.pt"),
                                "--evaluate-initial"]
                environment = dict(os.environ, BRAKECHECK_DATA_ROOT=str(settings.root), PYTHONUNBUFFERED="1")
                with (output / "ufld.stdout.log").open("a", encoding="utf-8") as log:
                    subprocess.run(command, cwd=Path(__file__).resolve().parents[1], env=environment,
                                   stdout=log, stderr=subprocess.STDOUT, check=True)
            result = read_json(ufld_run / "status.json")
            before = read_json(Path(state["base_run"]) / "status.json")
            comparison = {"same_validation_manifest": True, "base_train_images": state["base_train_images"],
                          "base_best_val_f1": before.get("best_val_f1"), "experiment_best_val_f1": result.get("best_val_f1"),
                          "extra_train_images": read_json(ufld_run / "dataset/experiment.json")["extra_train_images"],
                          "test_images_used": 0, "auto_promote": False,
                          "limitation": "Additional optimization as well as pseudo-data can affect scores; not a matched-budget causal comparison"}
            atomic_json(output / "comparison.json", comparison)
            preserved = preservation_report(settings, output)
            update(phase="complete_awaiting_comparison_review", comparison=comparison,
                   preservation=preserved, finished_at=time.time())
        except Exception as error:
            update(phase="error", error_type=type(error).__name__, error=str(error)[:1500])
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-run", type=Path)
    parser.add_argument("--parent-checkpoint", type=Path)
    parser.add_argument("--max-extra-ratio", type=float, default=1.0)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    settings = local_settings(args.data_root)
    if not args.resume:
        if not args.base_run or not args.parent_checkpoint:
            parser.error("New experiments require --base-run and --parent-checkpoint")
        if not np.isfinite(args.max_extra_ratio) or args.max_extra_ratio < 0:
            parser.error("--max-extra-ratio must be finite and nonnegative")
        freeze_inventory(settings, args.output, args.base_run, args.parent_checkpoint, args.max_extra_ratio)
    run_pipeline(settings, args.output)


if __name__ == "__main__":
    main()
