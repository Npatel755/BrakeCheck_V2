"""Isolated adaptation of a road/paint teacher for OPTIONAL flagged copies.

No live model annotations, human decisions or source conversions are written.
Only the frozen base TRAIN images fit new weights; BASE VAL selects the teacher.
Original human semantic paint remains intact, including dashes/transverse paint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import time

from .bootstrap import atomic_json
from .teachers.base import file_sha256

SCHEMA = "flagged_teacher_experiment:v1"
NAME = "comma_segnet_flagged_experiment"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _session(row):
    return (str(row["source"]), str(row["session_id"]))


def training_records(rows):
    result = {}
    for row in rows:
        image_sha = row.get("assets", {}).get("image", {}).get("sha256") or row.get("sha256")
        if not isinstance(image_sha, str) or len(image_sha) != 64:
            raise ValueError("Teacher training record requires the original IMAGE SHA256")
        record = {"image_id": row["image_id"], "source": row["source"],
                  "session_id": row["session_id"], "split": "train", "source_sha256": image_sha}
        old = result.get(row["image_id"])
        if old is not None and old != record:
            raise ValueError("Teacher ancestry has conflicting original-image fingerprints or sessions")
        result[row["image_id"]] = record
    return [result[key] for key in sorted(result)]


def validate_training_ancestry(parent_train, base_records, reserved_test_ids=(), test_records=()):
    """Reject local teacher ancestry that trained any UFLD held-out ID/drive."""
    evaluation = [row for row in base_records if row["split"] in ("val", "test")] + list(test_records)
    heldout_ids = {row["image_id"] for row in evaluation} | set(reserved_test_ids)
    heldout_sessions = {_session(row) for row in evaluation if row.get("session_id")}
    train_ids = {row["image_id"] for row in parent_train}
    train_sessions = {_session(row) for row in parent_train if row.get("session_id")}
    if train_ids & heldout_ids or train_sessions & heldout_sessions:
        raise ValueError("Teacher training ancestry overlaps held-out UFLD images or recording sessions")
    base_train = {row["image_id"] for row in base_records if row["split"] == "train"}
    if base_train & heldout_ids:
        raise ValueError("Reserved UFLD test IDs entered the base training cohort")
    return {"training_image_ids": sorted(train_ids),
            "training_session_ids": sorted({session for _, session in train_sessions}),
            "training_sessions": [{"source": source, "session_id": session}
                                  for source, session in sorted(train_sessions)],
            "heldout_image_ids": sorted(heldout_ids),
            "heldout_sessions": [{"source": source, "session_id": session}
                                 for source, session in sorted(heldout_sessions)],
            "test_images_used": 0, "validation_training_overlap": 0}


class _BaseOnlyRegistry:
    """Read-only view over original-source records in the frozen base manifest."""
    def __init__(self, registry, records, progress=None):
        self.registry = registry
        self.progress = progress or (lambda value: None)
        self.count = 0
        self.base = {row["image_id"]: row for row in records}

    def images(self, where="1", params=()):
        selected = []
        for raw in self.registry.images(where, params):
            row = dict(raw)
            frozen = self.base.get(row["image_id"])
            if frozen is None:
                continue
            if any(row[key] != frozen[key] for key in ("source", "session_id", "split")):
                raise ValueError("Live source record no longer matches frozen base identity/split/session")
            if frozen.get("source_sha256") and row["sha256"] != frozen["source_sha256"]:
                raise ValueError("Original source image fingerprint differs from frozen base")
            selected.append(row)
        return selected

    def annotation(self, *args, **kwargs):
        self.count += 1
        if self.count % 64 == 1:
            self.progress({"phase": "freezing_original_masks", "processed": self.count, "total": len(self.base)})
        return self.registry.annotation(*args, **kwargs)


def prepare_teacher(settings, registry, base_run, output, parent_checkpoint, progress=None):
    """Freeze current trusted base masks and inherited teacher ancestry, CPU only."""
    from . import annotation_training as training
    from . import source_mask_training as masks
    from .licenses import LicenseRegistry
    from .ufld_baseline import load_frozen_export

    output = Path(output).resolve()
    target = output / "experiment.json"
    if target.exists():
        raise FileExistsError("Refusing to replace a prepared teacher experiment")
    if any((output / part).exists() for part in ("source_cohort", "cohort", "parent_initial.pt")):
        raise FileExistsError("Partial teacher preparation already contains frozen artifacts. Use a fresh output directory; existing frozen files will not be overwritten.")
    output.mkdir(parents=True, exist_ok=True)
    def update(value):
        atomic_json(output / "prepare_status.json", {**value, "updated_at": time.time(), "pid": os.getpid()})
        if progress:
            progress(value)
    update({"phase": "validating_parent"})
    parent_checkpoint = Path(parent_checkpoint).resolve()
    parent_dir = parent_checkpoint.parent
    parent_config = json.loads((parent_dir / "config.json").read_text(encoding="utf-8"))
    parent_cohort_path = parent_dir / "training_cohort.json"
    parent_cohort = json.loads(parent_cohort_path.read_text(encoding="utf-8"))
    masks._validate_manifest(parent_cohort)
    if parent_config.get("cohort_sha256") != file_sha256(parent_cohort_path):
        raise ValueError("Parent teacher cohort fingerprint mismatch")
    provenance = parent_config.get("model_provenance", {})
    if provenance.get("hub_id") != training.MODEL or provenance.get("revision") != training.REVISION:
        raise ValueError("Parent teacher architecture/revision differs from pinned model")
    LicenseRegistry(settings.licenses_path).check_model("comma_segnet")
    update({"phase": "verifying_frozen_base"})
    snapshot, records, marker = load_frozen_export(base_run)
    test_records = [dict(row) for row in registry.images("split='test'")]
    reserved = json.loads((snapshot / "reserved_test_ids.json").read_text(encoding="utf-8"))
    parent_train = parent_cohort["cohorts"]["train"]
    ancestry = validate_training_ancestry(parent_train, records, reserved, test_records)
    output.mkdir(parents=True, exist_ok=True)
    update({"phase": "freezing_original_masks", "processed": 0, "total": len(records)})
    cohort = masks.freeze_source_cohort(settings, _BaseOnlyRegistry(registry, records, update), output / "source_cohort")
    training.validate_cohort(cohort)
    # DINO's prior worker failed on all-unknown masks. Verify the semantic
    # target has real supervised pixels before allowing any optimizer step.
    from .teachers.source_labels import load_classes
    empty, retained = [], {"train": [], "val": []}
    for split, rows in cohort["cohorts"].items():
        for index, row in enumerate(rows):
            if index % 64 == 0:
                update({"phase": "checking_known_semantic_pixels", "split": split, "processed": index, "total": len(rows)})
            if (load_classes(row["source_annotation"]) > 0).any():
                retained[split].append(row)
            else:
                empty.append({"image_id": row["image_id"], "split": split})
    cohort["cohorts"] = retained
    cohort["counts"] = {split: {source: sum(row["source"] == source for row in rows)
                                        for source in ("a2d2", "comma10k")}
                        for split, rows in retained.items()}
    cohort.pop("manifest_sha256", None)
    cohort["manifest_sha256"] = masks._digest(cohort)
    masks._validate_manifest(cohort)
    training.validate_cohort(cohort)
    atomic_json(output / "cohort/training_cohort.json", cohort)
    new_train = cohort["cohorts"]["train"]
    ancestry = validate_training_ancestry([*parent_train, *new_train], records, reserved, test_records)
    ancestry["training_records"] = training_records([*parent_train, *new_train])
    parent_copy = output / "parent_initial.pt"
    shutil.copyfile(parent_checkpoint, parent_copy)
    parent_sha = file_sha256(parent_checkpoint)
    if file_sha256(parent_copy) != parent_sha:
        raise RuntimeError("Parent checkpoint changed while copying")
    shutil.copyfile(parent_cohort_path, output / "parent_training_cohort.json")
    shutil.copyfile(parent_dir / "config.json", output / "parent_config.json")
    shutil.copyfile(__file__, output / "teacher_training_code.py")
    manifest = {"schema": SCHEMA, "name": NAME, "state": "prepared",
                "data_root": str(settings.root.resolve()),
                "base_run": str(Path(base_run).resolve()), "base_snapshot": str(snapshot),
                "base_manifest_sha256": file_sha256(snapshot / "manifest.json"),
                "base_export_revision": marker["revision"],
                "parent_checkpoint": str(parent_checkpoint), "parent_initial": str(parent_copy),
                "parent_checkpoint_sha256": parent_sha,
                "parent_cohort_sha256": file_sha256(output / "parent_training_cohort.json"),
                "cohort": str(output / "cohort/training_cohort.json"),
                "cohort_sha256": file_sha256(output / "cohort/training_cohort.json"),
                "training_ancestry": ancestry,
                "new_training_image_ids": sorted(row["image_id"] for row in new_train),
                "new_validation_image_ids": sorted(row["image_id"] for row in retained["val"]),
                "coverage": {"base_train": sum(row["split"] == "train" for row in records),
                             "base_val": sum(row["split"] == "val" for row in records),
                             "teacher_train": len(new_train), "teacher_val": len(retained["val"]),
                             "skipped_all_unknown": empty},
                "config": {"epochs": 8, "patience": 3, "learning_rate": 5e-6,
                           "width": 1024, "height": 512, "requested_batch_size": 4,
                           "workers": 2, "seed": 1234},
                "label_policy": "Original human semantic masks; unknown pixels ignored. Reviewed longitudinal polylines never erase transverse paint or fill dash gaps.",
                "local_parent_outside_new_base_training": len(set(row["image_id"] for row in parent_train)
                                                              - set(row["image_id"] for row in new_train)),
                "upstream_pretraining_caveat": "Pinned comma10k model may have seen comma10k validation images; select exclusively on Audi held-out validation drives.",
                "distribution_scope": cohort["distribution_scope"], "candidate_only": True,
                "auto_promote": False, "original_registry_writes": False, "test_images_used": 0,
                "code_sha256": file_sha256(output / "teacher_training_code.py"), "created_at": time.time()}
    manifest["manifest_sha256"] = _digest(manifest)
    atomic_json(target, manifest)
    update({"phase": "prepared", "coverage": manifest["coverage"]})
    return manifest


def load_prepared(output, verify_assets=True):
    from .source_mask_training import load_source_cohort
    output = Path(output).resolve()
    prepared = json.loads((output / "experiment.json").read_text(encoding="utf-8"))
    if prepared.get("schema") != SCHEMA:
        raise ValueError("Unexpected teacher experiment schema")
    unsigned = {key: value for key, value in prepared.items() if key != "manifest_sha256"}
    if prepared.get("manifest_sha256") != _digest(unsigned):
        raise ValueError("Teacher experiment manifest fingerprint mismatch")
    if file_sha256(__file__) != prepared["code_sha256"]:
        raise ValueError("Live teacher helper differs from frozen training code")
    for relative, expected in [("parent_initial.pt", prepared["parent_checkpoint_sha256"]),
                               ("parent_training_cohort.json", prepared["parent_cohort_sha256"]),
                               ("teacher_training_code.py", prepared["code_sha256"])]:
        if file_sha256(output / relative) != expected:
            raise ValueError("Prepared teacher input changed: " + relative)
    if file_sha256(prepared["cohort"]) != prepared["cohort_sha256"]:
        raise ValueError("Prepared source cohort fingerprint mismatch")
    cohort, _ = load_source_cohort(prepared["cohort"], verify=verify_assets)
    return prepared, cohort


def select_checkpoint(output, comparison):
    """Keep the exact warm-start teacher if adaptation does not improve Audi IoU."""
    output = Path(output).resolve()
    baseline = comparison["baseline"]["a2d2"]["paint_iou"]
    candidate = comparison["candidate"]["a2d2"]["paint_iou"]
    if baseline is None or candidate is None:
        raise ValueError("Audi validation paint IoU is required to select a teacher")
    improved = candidate > baseline
    selected = Path(comparison["checkpoint"]) if improved else output / "parent_initial.pt"
    return selected, improved


def train_teacher(output, progress=None, *, device="cuda"):
    """Run real isolated adaptation only when explicitly launched by orchestration."""
    import cv2
    import numpy as np
    import torch
    from filelock import FileLock
    from . import annotation_training as training

    output = Path(output).resolve()
    progress = progress or (lambda value: None)
    state = {"schema": SCHEMA, "phase": "preflight", "pid": os.getpid(), "started_at": time.time(),
             "candidate_only": True, "auto_promote": False, "test_images_used": 0}
    def update(value):
        state.update(value, updated_at=time.time())
        atomic_json(output / "status.json", state)
        progress(dict(state))
    prepared, cohort = load_prepared(output)
    lock = FileLock(str(Path(prepared["data_root"]) / "logs/large-models.lock"))
    with FileLock(str(output / "training.lock"), timeout=0), lock.acquire(timeout=0):
        try:
            if (output / "teacher.json").exists():
                raise FileExistsError("This isolated teacher experiment is already complete")
            if device == "cuda" and not torch.cuda.is_available():
                raise RuntimeError("Teacher adaptation requires a CUDA device")
            records = training.validate_cohort(cohort)
            random.seed(1234); np.random.seed(1234); torch.manual_seed(1234)
            torch.set_num_threads(2); cv2.setNumThreads(1)
            model, pretrained = training.cached_model()
            parent = torch.load(output / "parent_initial.pt", map_location="cpu", weights_only=True)
            if parent.get("test_images_used") != 0 or parent.get("baseline_revision") != training.REVISION:
                raise ValueError("Parent checkpoint provenance differs from prepared pinned teacher")
            if parent.get("config", {}).get("cohort_sha256") != prepared["parent_cohort_sha256"]:
                raise ValueError("Parent checkpoint training cohort does not match pinned ancestry")
            model.load_state_dict(parent["model"], strict=True)
            del parent
            model = model.to(device)
            fixed = list(cohort["mapping_code"].values()) + [asset for license in cohort["licenses"].values()
                                                           for asset in license["files"]]
            config = {**prepared["config"], "cohort_sha256": prepared["cohort_sha256"],
                      "validation_manifest_sha256": _digest(records["val"]),
                      "warm_start_sha256": prepared["parent_checkpoint_sha256"],
                      "model_provenance": pretrained, "candidate_only": True, "auto_promote": False,
                      "test_images_used": 0}
            update({"phase": "fit_check", "train_images": len(records["train"]), "val_images": len(records["val"])})
            fit = training.fit_check(model, training.MaskDataset(records["train"], config["width"], config["height"],
                                     fixed_assets=fixed), config["requested_batch_size"], device)
            config["batch_size"] = fit["batch_size"]
            atomic_json(output / "gpu_fit.json", fit); atomic_json(output / "config.json", config)
            train = training.make_loader(records["train"], config["width"], config["height"], fit["batch_size"],
                                         training=True, workers=config["workers"], fixed_assets=fixed)
            validation = training.make_loader(records["val"], config["width"], config["height"], fit["batch_size"],
                                              workers=config["workers"], fixed_assets=fixed)
            comparison = training.train_candidate(model, train, validation, output / "adaptation", config,
                                                   device=device, progress=update)
            selected, improved = select_checkpoint(output, comparison)
            teacher = {"name": NAME, "version": NAME + ":" + file_sha256(selected)[:16],
                       "checkpoint": str(selected), "checkpoint_sha256": file_sha256(selected),
                       **{key: prepared["training_ancestry"][key] for key in
                          ("training_image_ids", "training_session_ids", "training_sessions", "training_records")},
                       "input": [config["width"], config["height"]], "input_range": [0, 255],
                       "class_names": list(training.CLASS_NAMES), "model_license": "MIT",
                       "distribution_scope": prepared["distribution_scope"], "parent_checkpoint_sha256": prepared["parent_checkpoint_sha256"],
                       "improved_audi_paint_iou": improved, "selection": "adapted" if improved else "preserved_parent",
                       "comparison": comparison, "candidate_only": True, "auto_promote": False,
                       "test_images_used": 0, "upstream_pretraining_caveat": prepared["upstream_pretraining_caveat"]}
            atomic_json(output / "teacher.json", teacher)
            update({"phase": "complete_awaiting_review", "teacher": {key: value for key, value in teacher.items()
                                                                       if key not in ("training_image_ids", "training_session_ids", "training_sessions", "training_records")}})
            return teacher
        except Exception as error:
            update({"phase": "error", "error_type": type(error).__name__, "error": str(error)[:1000]})
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--data-root")
    prepare.add_argument("--base-run", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--parent-checkpoint", type=Path, required=True)
    train = commands.add_parser("train")
    train.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        from .local import local_settings
        from .flagged_model_trial import ReadOnlyRegistry
        settings = local_settings(args.data_root)
        registry = ReadOnlyRegistry(settings.db_path)
        try:
            result = prepare_teacher(settings, registry, args.base_run, args.output, args.parent_checkpoint)
            print(json.dumps({"state": result["state"], "coverage": result["coverage"]}))
        finally:
            registry.close()
    else:
        result = train_teacher(args.output)
        print(json.dumps({key: result[key] for key in ("name", "version", "checkpoint", "improved_audi_paint_iou")}))


if __name__ == "__main__":
    main()
