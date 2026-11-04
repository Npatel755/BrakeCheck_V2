"""Run the authorized frozen-7B decoder experiment without altering reviews."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import shutil
import time

from .bootstrap import atomic_json
from .db import Registry
from .dinov3_download import MODEL, REVISION, download
from .local import local_settings


def memory_guard(warm_start, *, backbone=MODEL, revision=REVISION, width=896, height=448,
                 batch_size=1, precision="bfloat16"):
    """Use measured headroom only for the identical previously fitted profile."""
    result = {"required_free_vram_gib": 18.0, "basis": "unverified_profile_default"}
    if not warm_start:
        return result
    from .teachers.dinov3_seg import validate_warm_start
    from .teachers.base import file_sha256
    state = validate_warm_start(warm_start, backbone, revision, width, height)
    evidence = Path(warm_start).parent / "result.json"
    try:
        prior = json.loads(evidence.read_text(encoding="utf-8"))
        peak = float(prior["peak_vram_gib"])
        matches = (backbone == MODEL and revision == REVISION and [width, height] == [896, 448] and
                   batch_size == 1 and precision == "bfloat16" and
                   state.get("batch_size") == batch_size and state.get("precision") == precision and
                   state.get("backbone_frozen") is True and prior.get("backbone_frozen") is True and
                   prior.get("batch_size") == batch_size and prior.get("precision") == precision and
                   prior.get("backbone_revision") == revision and
                   prior.get("backbone_parameters") == state.get("backbone_parameters") == 6716035072 and
                   prior.get("training_cohort_sha256") == state.get("training_cohort_sha256") and
                   Path(prior["checkpoint"]).resolve() == Path(warm_start).resolve() and 0 < peak < 24)
        if matches:
            result.update(required_free_vram_gib=max(14.8, peak + 1.5), basis="identical_measured_profile",
                          measured_peak_vram_gib=peak, evidence_path=str(evidence.resolve()),
                          evidence_sha256=file_sha256(evidence), warm_start_sha256=file_sha256(Path(warm_start)),
                          input=[width, height], batch_size=batch_size, precision=precision,
                          backbone=backbone, revision=revision,
                          threshold_rationale="Same frozen 7B BF16/batch-1/896x448 profile: prior measured peak +1.5 GiB, floor14.8; mandatory real 3-step fit before full training")
    except (OSError, KeyError, ValueError, TypeError):
        pass
    return result


def acquire_gpu_slot(lock, free_bytes, required_gib, wait):
    """Never hold the shared model lock while waiting for another app's VRAM."""
    from filelock import Timeout
    while True:
        free = free_bytes() / 2**30
        if free < required_gib:
            wait("waiting_for_gpu_memory", {"free_vram_gib": round(free, 2),
                 "required_free_vram_gib": required_gib, "detail": "Waiting for GPU memory; other applications remain running"})
            continue
        try:
            lock.acquire(timeout=0)
        except Timeout:
            wait("waiting_for_gpu", {"detail": "Another annotation trainer holds the GPU model lock"})
            continue
        if free_bytes() / 2**30 >= required_gib:
            return
        lock.release()


def main():
    parser = argparse.ArgumentParser(description="Train the DINOv3 decoder only; never train UFLDv2")
    parser.add_argument("--data-root")
    parser.add_argument("--cache-dir")
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--check", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", type=Path, help="Continue the exact saved cohort/optimizer experiment")
    mode.add_argument("--warm-start", type=Path, help="Initialize only decoder weights; freeze a fresh expanded cohort")
    parser.add_argument("--cohort", type=Path, help="Use a pre-frozen original-source manifest shared with other candidates")
    args = parser.parse_args()
    if args.epochs < 1:
        parser.error("epochs must be positive")
    if args.resume and args.cohort:
        parser.error("--resume uses its original cohort; --cohort is for a new experiment")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.ERROR)
    from filelock import FileLock, Timeout
    import torch
    from .teachers.dinov3_seg import train, _load_pairs
    from .teachers.base import file_sha256
    from .source_mask_training import freeze_source_cohort, load_source_cohort, SCHEMA

    settings = local_settings(args.data_root)
    report = settings.path("reports", "dinov3_training.json")
    state = {"pid": os.getpid(), "model": MODEL, "revision": REVISION,
             "phase": "preflight", "epochs": args.epochs, "backbone_frozen": True,
             "ufld_training": "deferred", "auto_promote": False, "test_images_used": 0}
    gpu_owned = False

    def update(changes):
        state.update(changes, updated_at=time.time())
        atomic_json(report, state)
        def publish_models():
            model_path = settings.path("reports", "model_status.json")
            try:
                models = json.loads(model_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                models = {"models": {}}
            item = models.setdefault("models", {}).setdefault("dinov3", {})
            item.update(state="decoder_" + state["phase"], hub_id=MODEL, revision=REVISION,
                        training_report=str(report), step=state.get("step"),
                        total_steps=state.get("total_steps"), val_metrics=state.get("val_metrics"))
            models.update(pid=os.getpid())
            atomic_json(model_path, models)
        if gpu_owned:
            publish_models()
        elif not args.check:
            try:
                # Never race the active SAM3 status writer during downloads.
                with FileLock(str(settings.path("logs", "large-models.lock")), timeout=0):
                    publish_models()
            except Timeout:
                pass

    def wait_or_fail(phase, details):
        update({"phase": phase, **details})
        if not args.wait:
            raise RuntimeError(phase)
        time.sleep(10)

    with FileLock(str(settings.path("logs", "dinov3-training.lock")), timeout=0):
        reg = Registry(settings.db_path)
        try:
            pairs = {split: _load_pairs(reg, (split,), ("commercial",)) for split in ("train", "val")}
            if not all(pairs.values()):
                raise RuntimeError("Separate eligible train and validation masks are required")
            rows = reg.images("status='curated' AND pool='commercial' AND has_source_label=1 AND review_status!='rejected'")
            cohorts = {split: [dict(row) for row in rows if row["split"] == split] for split in ("train", "val")}
            train_sessions = {(row["source"], row["session_id"]) for row in cohorts["train"] if row["session_id"]}
            val_sessions = {(row["source"], row["session_id"]) for row in cohorts["val"] if row["session_id"]}
            if train_sessions & val_sessions:
                raise RuntimeError("Recording-session leakage between train and validation")
            update({"train_images": len(pairs["train"]), "val_images": len(pairs["val"]),
                    "cuda": torch.cuda.is_available(), "free_disk_gib": round(shutil.disk_usage(settings.root).free / 2**30, 2)})
            if args.check:
                if args.cohort:
                    shared = json.loads(args.cohort.read_text(encoding="utf-8"))
                    state.update(cohort=str(args.cohort), train_images=len(shared["cohorts"]["train"]),
                                 val_images=len(shared["cohorts"]["val"]), source_counts=shared.get("counts"))
                print(json.dumps(state, indent=2))
                return
            if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
                raise RuntimeError("The pinned 7B experiment requires CUDA BF16 support")
            output = settings.path("models", "dinov3_seg", "run-" + time.strftime("%Y%m%d-%H%M%S"))
            output.mkdir(parents=True, exist_ok=False)
            atomic_json(output / "cohort-preflight.json", {"label_source": "original_human_source_masks",
                        "test_images_used": 0, "cohorts": cohorts})
            guard = memory_guard(args.warm_start)
            atomic_json(output / "gpu-memory-profile.json", guard)
            update({"memory_guard": guard, "warm_start": str(args.warm_start) if args.warm_start else None})
            update({"output": str(output), "phase": "downloading"})
            while True:
                try:
                    snapshot = download(args.cache_dir, update)
                    break
                except Timeout:
                    wait_or_fail("waiting_for_download", {"detail": "Another process is downloading the same pinned weights"})
            update({"snapshot": str(snapshot), "phase": "waiting_for_gpu"})
            gpu_lock = FileLock(str(settings.path("logs", "large-models.lock")))
            acquire_gpu_slot(gpu_lock, lambda: torch.cuda.mem_get_info()[0], guard["required_free_vram_gib"], wait_or_fail)
            gpu_owned = True
            try:
                cfg = settings.cfg["teachers"]["dinov3_seg"]
                cfg.update(backbone=MODEL, revision=REVISION, precision="bfloat16",
                           cache_dir=str(snapshot.parents[2]), enabled=False,
                           input_width=896, input_height=448)
                license_file = settings.path("licenses", "dinov3-LICENSE.txt")
                if not license_file.is_file():
                    raise RuntimeError("The verified DINOv3 license snapshot is required")
                shutil.copyfile(license_file, output / "DINOv3-LICENSE.txt")
                cfg["license_sha256"] = file_sha256(license_file)
                shutil.copyfile(snapshot / "verified_manifest.json", output / "backbone_manifest.json")
                cfg["backbone_manifest_sha256"] = file_sha256(output / "backbone_manifest.json")
                cohort_file = output / "training_cohort.json"
                if args.resume:
                    from .teachers.source_labels import load_classes
                    frozen_file = args.resume.parent / "training_cohort.json"
                    frozen = json.loads(frozen_file.read_text(encoding="utf-8"))
                    if set(frozen["cohorts"]) != {"train", "val"} or frozen.get("test_images_used") != 0:
                        raise RuntimeError("Invalid resume cohort")
                    if frozen.get("schema") == SCHEMA:
                        frozen, pairs = load_source_cohort(frozen_file)
                    else:
                        # Legacy runs did not store annotation fingerprints.
                        # Keep their exact image cohort and explicitly bind the
                        # currently available original masks in an extra report.
                        pairs, legacy_sources = {}, []
                        for split, rows in frozen["cohorts"].items():
                            pairs[split] = []
                            for row in rows:
                                live = reg.image(row["image_id"])
                                if (row["split"] != split or live is None or live["split"] != split or
                                        live["status"] != "curated" or live["review_status"] == "rejected" or
                                        file_sha256(Path(row["path"])) != row["sha256"]):
                                    raise RuntimeError("Legacy resume cohort is no longer eligible")
                                ann = reg.annotation(row["image_id"], "source_label")
                                if ann is None or json.loads(ann["params"] or "{}").get("human") is not True:
                                    raise RuntimeError("Resume cohort source mask missing")
                                legacy_sources.append({"annotation": dict(ann), "source_sha256": file_sha256(Path(ann["path"]))})
                                pairs[split].append((row["path"], lambda a=dict(ann): load_classes(a)))
                        atomic_json(output / "legacy-resume-source-bindings.json", legacy_sources)
                    shutil.copyfile(frozen_file, cohort_file)
                    # Keep the previous best separately from the resume state.
                    shutil.copyfile(args.resume.parent / "best.pt", output / "best.pt")
                    shutil.copyfile(args.resume.parent / "best.json", output / "best.json")
                    update({"resumed_from": str(args.resume), "train_images": len(pairs["train"]),
                            "val_images": len(pairs["val"])})
                else:
                    if args.cohort:
                        frozen, pairs = load_source_cohort(args.cohort)
                        shutil.copyfile(args.cohort, cohort_file)
                    else:
                        freeze_source_cohort(settings, reg, output)
                        frozen, pairs = load_source_cohort(cohort_file)
                if frozen.get("licenses"):
                    licenses_dir = output / "licenses"
                    licenses_dir.mkdir(exist_ok=True)
                    for item in frozen["licenses"].values():
                        for asset in item["files"]:
                            destination = licenses_dir / asset["name"]
                            if Path(asset["path"]).resolve() != destination.resolve():
                                shutil.copyfile(asset["path"], destination)
                    cfg["training_data_licenses"] = frozen["licenses"]
                    cfg["distribution_scope"] = frozen["distribution_scope"]
                else:
                    source_license = settings.path("licenses", "comma10k-LICENSE.txt")
                    shutil.copyfile(source_license, output / "comma10k-LICENSE.txt")
                    cfg["training_data_license_sha256"] = file_sha256(source_license)
                cfg["training_cohort_sha256"] = file_sha256(cohort_file)
                val_sources = [row["source"] for row in frozen["cohorts"]["val"]]
                selection_source = "a2d2" if "a2d2" in val_sources else None
                update({"train_images": len(pairs["train"]), "val_images": len(pairs["val"]),
                        "source_counts": frozen.get("counts"), "training_cohort_sha256": cfg["training_cohort_sha256"],
                        "selection_metric": "a2d2.lane_iou" if selection_source else "lane_iou"})
                torch.cuda.reset_peak_memory_stats()
                update({"phase": "fit_check", "step": 0, "total_steps": 3})
                # Real weights, source-labelled train/val photos and real optimizer
                # steps; this small checkpoint is never presented as a ready model.
                smoke = None if args.resume else train(settings, reg, output / "smoke.pt", backbone_id=MODEL,
                              batch_size=1, max_steps=3, train_limit=4, val_limit=4,
                              device="cuda", data_pairs=pairs, init_from=args.warm_start,
                              progress=lambda p: update({**p, "phase": "fit_check"}))
                atomic_json(output / "gpu-fit.json", smoke)
                update({"phase": "training", "gpu_fit": smoke, "step": 0,
                        "total_steps": args.epochs * len(pairs["train"])})
                result = train(settings, reg, output / "best.pt", backbone_id=MODEL,
                               epochs=args.epochs, batch_size=1, device="cuda", data_pairs=pairs, progress=update,
                               resume_from=args.resume, init_from=args.warm_start,
                               evaluate_initial=bool(args.warm_start), val_sources=val_sources,
                               selection_source=selection_source)
                atomic_json(output / "result.json", result)
                update({"phase": "trained_awaiting_comparison", "result": result,
                        "checkpoint": str(output / "best.pt"),
                        "checkpoint_sha256": file_sha256(output / "best.pt")})
            finally:
                gpu_owned = False
                gpu_lock.release()
        except Exception as error:
            update({"failed_phase": state["phase"], "phase": "error", "error_type": type(error).__name__,
                    "error_reason": str(error)[:500] if isinstance(error, RuntimeError) else None})
            logging.error("DINOv3 stage failed: %s", type(error).__name__)
            raise SystemExit(2) from None
        finally:
            reg.close()


if __name__ == "__main__":
    main()
