"""Candidate-only adaptation of the pinned comma road/paint segmenter.

This worker reads frozen original source masks. It never registers annotations,
updates review decisions, or changes the independent model used by source audits.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import time

import cv2
import numpy as np

from .bootstrap import atomic_json
from .teachers.base import file_sha256

MODEL = "commaai/comma10k-segnet"
REVISION = "b642c614eb0737fe7befa229f47fbd0245033038"
NAME = "comma_segnet_adapted"
IGNORE = 255
CLASS_NAMES = ("road", "paint", "undrivable", "movable", "ego_vehicle")


def validate_cohort(manifest):
    cohorts = manifest.get("cohorts", {})
    if set(cohorts) != {"train", "val"} or manifest.get("test_images_used") != 0:
        raise ValueError("Only frozen train/val cohorts are allowed")
    identities, sessions = {}, {}
    for split, records in cohorts.items():
        if not records:
            raise ValueError("Both training and validation source masks are required")
        if any(row.get("split") != split or row.get("source") not in ("a2d2", "comma10k") for row in records):
            raise ValueError("Unexpected source or split in frozen cohort")
        identities[split] = {row["image_id"] for row in records}
        if len(identities[split]) != len(records):
            raise ValueError("Duplicate image in frozen cohort")
        sessions[split] = {(row["source"], row["session_id"]) for row in records if row.get("session_id")}
    if identities["train"] & identities["val"] or sessions["train"] & sessions["val"]:
        raise ValueError("Image or recording-session leakage between train and validation")
    if not any(row["source"] == "a2d2" for row in cohorts["val"]):
        raise ValueError("Audi held-out validation drives are required")
    return cohorts


def map_target(shared):
    """Preserve unknown pixels as ignored; existing five model outputs stay fixed."""
    shared = np.asarray(shared)
    if shared.ndim != 2 or not np.isin(shared, np.arange(6)).all():
        raise ValueError("Source masks must use shared classes 0..5")
    target = np.full(shared.shape, IGNORE, dtype=np.uint8)
    valid = shared > 0
    target[valid] = shared[valid] - 1
    return target


def segmentation_loss(logits, target, weights=(1., 4., 1., 1., 1.)):
    import torch
    import torch.nn.functional as F
    valid = target != IGNORE
    if not valid.any():
        return logits.sum() * 0.
    ce = F.cross_entropy(logits.float(), target,
                         weight=logits.new_tensor(weights, dtype=torch.float32), ignore_index=IGNORE)
    paint = torch.softmax(logits.float(), dim=1)[:, 1] * valid
    truth = (target == 1).float()
    dice = 1. - (2. * (paint * truth).sum() + 1.) / (paint.sum() + truth.sum() + 1.)
    return ce + .5 * dice


def confusion_update(matrix, predicted, target):
    predicted, target = np.asarray(predicted), np.asarray(target)
    if predicted.shape != target.shape:
        raise ValueError("Prediction and target dimensions differ")
    valid = target != IGNORE
    if not np.isin(predicted[valid], np.arange(5)).all() or not np.isin(target[valid], np.arange(5)).all():
        raise ValueError("Invalid segmentation classes")
    matrix += np.bincount((target[valid] * 5 + predicted[valid]).astype(np.int64), minlength=25).reshape(5, 5)


def metrics_from_confusion(matrix):
    matrix = np.asarray(matrix, np.int64)
    def ratio(a, b):
        return float(a / b) if b else None
    tp = int(matrix[1, 1]); predicted = int(matrix[:, 1].sum()); truth = int(matrix[1].sum())
    road_tp = int(matrix[:2, :2].sum())
    road_pred, road_true = int(matrix[:, :2].sum()), int(matrix[:2].sum())
    return {"paint_iou": ratio(tp, predicted + truth - tp),
            "paint_precision": ratio(tp, predicted), "paint_recall": ratio(tp, truth),
            "road_iou": ratio(road_tp, road_pred + road_true - road_tp),
            "class_iou": {name: ratio(int(matrix[k, k]), int(matrix[:, k].sum() + matrix[k].sum() - matrix[k, k]))
                          for k, name in enumerate(CLASS_NAMES)},
            "confusion": matrix.tolist(), "valid_pixels": int(matrix.sum())}


class MaskDataset:
    def __init__(self, records, width, height, augment=False, fixed_assets=()):
        self.records, self.width, self.height, self.augment = list(records), width, height, augment
        self.fixed_assets = list(fixed_assets)
        self.verified = {}

    def __len__(self):
        return len(self.records)

    def __getitem__(self, index):
        import torch
        from .teachers.source_labels import load_classes
        row = self.records[index]
        for asset in [*self.fixed_assets, *row.get("assets", {}).values()]:
            path = Path(asset["path"])
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            key = (str(path), asset["sha256"])
            if self.verified.get(key) != signature:
                if file_sha256(path) != asset["sha256"]:
                    raise RuntimeError("Frozen source file SHA mismatch: " + str(path))
                self.verified[key] = signature
        rgb = cv2.imread(str(row["path"]), cv2.IMREAD_COLOR)
        if rgb is None:
            raise IOError(row["path"])
        rgb = cv2.cvtColor(rgb, cv2.COLOR_BGR2RGB)
        classes = load_classes(row["source_annotation"])
        if classes.shape != rgb.shape[:2]:
            raise ValueError("Original image and source-mask dimensions differ")
        image = cv2.resize(rgb, (self.width, self.height), interpolation=cv2.INTER_AREA).astype(np.float32)
        target = map_target(cv2.resize(classes, (self.width, self.height), interpolation=cv2.INTER_NEAREST))
        if self.augment:
            if random.random() < .5:
                image, target = image[:, ::-1].copy(), target[:, ::-1].copy()
            image = np.clip(image * random.uniform(.8, 1.2) + random.uniform(-10., 10.), 0., 255.)
        # Match the pinned pretrained model: RGB 0..255, no normalization.
        return torch.from_numpy(image.copy()).permute(2, 0, 1), torch.from_numpy(target.astype(np.int64)), row["source"]


def _worker_init(worker):
    import torch
    cv2.setNumThreads(1)
    seed = torch.initial_seed() % 2**32
    random.seed(seed); np.random.seed(seed)


def make_loader(records, width, height, batch_size, *, training=False, workers=0, seed=1234, fixed_assets=()):
    import torch
    from collections import Counter
    counts = Counter(row["source"] for row in records)
    generator = torch.Generator().manual_seed(seed)
    sampler = None
    if training:
        sampler = torch.utils.data.WeightedRandomSampler(
            [1. / counts[row["source"]] for row in records], len(records), replacement=True, generator=generator)
    return torch.utils.data.DataLoader(MaskDataset(records, width, height, training, fixed_assets), batch_size=batch_size,
                                      sampler=sampler, num_workers=workers, pin_memory=torch.cuda.is_available(),
                                      worker_init_fn=_worker_init, generator=generator,
                                      persistent_workers=workers > 0, **({"prefetch_factor": 2} if workers else {}))


def _autocast(torch, device):
    return torch.autocast("cuda", dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16) if device == "cuda" else nullcontext()


def evaluate(model, loader, device, progress=None):
    import torch
    matrices, counts, ignored = {}, {}, {}
    model.eval()
    done = 0
    with torch.inference_mode():
        for images, target, sources in loader:
            with _autocast(torch, device):
                predicted = model(images.to(device, non_blocking=True)).argmax(1).cpu().numpy()
            for prediction, truth, source in zip(predicted, target.numpy(), sources):
                matrix = matrices.setdefault(source, np.zeros((5, 5), np.int64))
                confusion_update(matrix, prediction, truth)
                counts[source] = counts.get(source, 0) + 1
                ignored[source] = ignored.get(source, 0) + int((truth == IGNORE).sum())
            done += len(sources)
            if progress and (done % 64 < len(sources) or done == len(loader.dataset)):
                progress({"evaluated": done, "evaluation_total": len(loader.dataset)})
    return {source: {**metrics_from_confusion(matrix), "images": counts[source],
                     "ignored_pixels": ignored[source], "evaluation_scope": "known_original_source_classes_only",
                     "interpretation": "held_out_drives_model_selection" if source == "a2d2" else
                     "diagnostic_only_pretrained_model_used_comma10k"}
            for source, matrix in matrices.items()}


def fit_check(model, dataset, batch_size, device):
    """Exercise real forward/backward/optimizer, then restore pretrained state."""
    import torch
    original = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
    attempted = []
    for size in dict.fromkeys((batch_size, 1)):
        attempted.append(size)
        optimizer = torch.optim.AdamW(model.parameters(), lr=1e-5)
        images = target = loss = samples = None
        try:
            if device == "cuda":
                torch.cuda.reset_peak_memory_stats()
            model.train()
            samples = [dataset[index % len(dataset)] for index in range(size)]
            images = torch.stack([sample[0] for sample in samples]).to(device)
            target = torch.stack([sample[1] for sample in samples]).to(device)
            with _autocast(torch, device):
                loss = segmentation_loss(model(images), target)
            if not torch.isfinite(loss):
                raise RuntimeError("Non-finite fit-check loss")
            loss.backward(); optimizer.step()
            result = {"batch_size": size, "attempted_batch_sizes": attempted, "real_optimizer_step": True,
                      "loss": float(loss.detach()), "peak_vram_gib":
                      round(torch.cuda.max_memory_allocated() / 2**30, 3) if device == "cuda" else None}
            break
        except torch.OutOfMemoryError:
            if size == 1:
                raise
        finally:
            model.load_state_dict(original)
            optimizer.zero_grad(set_to_none=True)
            del optimizer, images, target, loss, samples
            if device == "cuda":
                torch.cuda.empty_cache()
    return result


def _save_checkpoint(torch, path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    torch.save(data, temporary)
    os.replace(temporary, path)


def train_candidate(model, train_loader, val_loader, output, config, *, device="cuda", progress=None):
    """Train only an isolated model object, selecting exclusively on Audi val."""
    import torch
    progress = progress or (lambda value: None)
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    optimizer = torch.optim.AdamW(model.parameters(), lr=config.get("learning_rate", 1e-5), weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, max(1, config["epochs"] * len(train_loader)))
    scaler = torch.amp.GradScaler("cuda", enabled=device == "cuda" and not torch.cuda.is_bf16_supported())
    baseline = evaluate(model, val_loader, device, lambda p: progress({"phase": "baseline_evaluation", **p}))
    if baseline.get("a2d2", {}).get("paint_iou") is None:
        raise RuntimeError("Audi validation paint is required for model selection")
    atomic_json(output / "baseline.json", baseline)
    history, best_score, best_epoch, stale, step = [], float("-inf"), 0, 0, 0
    for epoch in range(1, config["epochs"] + 1):
        model.train(); loss_sum, trained = 0., 0
        for images, target, sources in train_loader:
            optimizer.zero_grad(set_to_none=True)
            images, target = images.to(device, non_blocking=True), target.to(device, non_blocking=True)
            with _autocast(torch, device):
                loss = segmentation_loss(model(images), target)
            if not torch.isfinite(loss):
                raise RuntimeError("Non-finite training loss")
            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.)
            scaler.step(optimizer); scaler.update(); scheduler.step()
            trained += len(sources); loss_sum += float(loss.detach()) * len(sources); step += 1
            if trained % 64 < len(sources) or trained == len(train_loader.dataset):
                progress({"phase": "training", "epoch": epoch, "step": step, "epoch_images": trained,
                          "train_images": len(train_loader.dataset), "loss": loss_sum / trained,
                          "learning_rate": optimizer.param_groups[0]["lr"]})
        metrics = evaluate(model, val_loader, device, lambda p: progress({"phase": "validation", "epoch": epoch, **p}))
        score = metrics["a2d2"]["paint_iou"]
        record = {"epoch": epoch, "loss": loss_sum / max(1, trained), "metrics": metrics, "trained_steps": step}
        history.append(record); atomic_json(output / "history.json", history)
        checkpoint = {"schema": "comma_segnet_adapted:v1", "model": model.state_dict(), "optimizer": optimizer.state_dict(),
                      "scheduler": scheduler.state_dict(), "scaler": scaler.state_dict(), "config": config,
                      "trained_steps": step, "epoch": epoch, "metrics": metrics, "candidate_only": True,
                      "auto_promote": False, "test_images_used": 0, "baseline_revision": REVISION}
        _save_checkpoint(torch, output / "last.pt", checkpoint)
        if score is not None and score > best_score:
            best_score, best_epoch, stale = score, epoch, 0
            _save_checkpoint(torch, output / "best.pt", checkpoint)
            atomic_json(output / "best.json", record)
        else:
            stale += 1
        progress({"phase": "epoch_complete", **record, "best_epoch": best_epoch, "best_audi_paint_iou": best_score})
        if stale >= config.get("patience", 3):
            break
    selected = torch.load(output / "best.pt", map_location="cpu", weights_only=True)
    model.load_state_dict(selected["model"])
    final = evaluate(model, val_loader, device, lambda p: progress({"phase": "final_comparison", **p}))
    comparison = {"baseline": baseline, "candidate": final, "selection_source": "a2d2_validation_drives",
                  "same_frozen_validation": True, "candidate_only": True, "auto_promote": False,
                  "cohort_sha256": config.get("cohort_sha256"),
                  "validation_manifest_sha256": config.get("validation_manifest_sha256"),
                  "improved_audi_paint_iou": final["a2d2"]["paint_iou"] > baseline["a2d2"]["paint_iou"],
                  "best_epoch": best_epoch, "epochs_completed": len(history), "trained_steps": step,
                  "test_images_used": 0, "checkpoint": str(output / "best.pt"),
                  "checkpoint_sha256": file_sha256(output / "best.pt")}
    atomic_json(output / "comparison.json", comparison)
    return comparison


def cached_model():
    from huggingface_hub import snapshot_download
    import segmentation_models_pytorch as smp
    snapshot = Path(snapshot_download(MODEL, revision=REVISION, local_files_only=True))
    paths = [snapshot / "config.json", snapshot / "model.safetensors"]
    if not all(path.is_file() for path in paths):
        raise RuntimeError("The pinned cached pretrained model is incomplete")
    return smp.from_pretrained(str(snapshot), local_files_only=True), {
        "hub_id": MODEL, "revision": REVISION, "snapshot": str(snapshot),
        "files": {str(path): file_sha256(path) for path in paths}, "license": "MIT"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root")
    parser.add_argument("--cohort", type=Path, required=True, help="Verified shared original-mask training manifest")
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=512)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    if min(args.epochs, args.batch_size, args.width, args.height) < 1 or args.workers < 0 or args.width % 32 or args.height % 32:
        parser.error("Use positive sizes and dimensions divisible by32")
    import torch
    from filelock import FileLock, Timeout
    from .local import local_settings
    from .licenses import LicenseRegistry
    from . import source_mask_training
    settings = local_settings(args.data_root)
    report = settings.path("reports", "annotation_training.json")
    state = {"schema": "annotation_training:v1", "pid": os.getpid(), "phase": "preflight", "model": NAME,
             "candidate_only": True, "auto_promote": False, "test_images_used": 0, "started_at": time.time()}
    def update(value):
        state.update(value, updated_at=time.time()); atomic_json(report, state)
    gpu_lock = FileLock(str(settings.path("logs", "large-models.lock")))
    def wait_or_raise(phase):
        update({"phase": phase})
        if not args.wait:
            raise RuntimeError(phase)
        time.sleep(10)
    with FileLock(str(settings.path("logs", "annotation-training.lock")), timeout=0):
        acquired = False
        try:
            LicenseRegistry(settings.licenses_path).check_model("comma_segnet")
            cohort, _ = source_mask_training.load_source_cohort(args.cohort, verify=True)
            records = validate_cohort(cohort)
            fixed_assets = list(cohort["mapping_code"].values()) + [asset for entry in cohort["licenses"].values()
                                                                  for asset in entry["files"]]
            if not torch.cuda.is_available():
                raise RuntimeError("This adaptation worker requires CUDA")
            torch.set_num_threads(2); cv2.setNumThreads(1)
            random.seed(1234); np.random.seed(1234); torch.manual_seed(1234)
            output = settings.path("models", NAME, "run-" + time.strftime("%Y%m%d-%H%M%S"))
            output.mkdir(parents=True, exist_ok=False)
            shutil.copyfile(args.cohort, output / "training_cohort.json")
            (output / "licenses").mkdir()
            for binding in cohort.get("licenses", {}).values():
                for item in binding.get("files", []):
                    shutil.copyfile(item["path"], output / "licenses" / item["name"])
            shutil.copyfile(settings.licenses_path, output / "license_registry.yaml")
            validation_sha = hashlib.sha256(json.dumps(records["val"], sort_keys=True,
                                            separators=(",", ":")).encode()).hexdigest()
            config = {"epochs": args.epochs, "patience": 3, "learning_rate": 1e-5, "width": args.width,
                      "height": args.height, "requested_batch_size": args.batch_size, "workers": args.workers,
                      "seed": 1234, "input_range": [0, 255], "class_weights": [1., 4., 1., 1., 1.],
                      "loss": "weighted_cross_entropy_plus_0.5_paint_dice", "source_balanced_sampling": True,
                      "cohort_path": str(output / "training_cohort.json"), "cohort_sha256": file_sha256(args.cohort),
                      "validation_manifest_sha256": validation_sha,
                      "code_sha256": file_sha256(__file__), "candidate_only": True, "auto_promote": False,
                      "test_images_used": 0, "baseline_revision": REVISION, "class_names": list(CLASS_NAMES),
                      "torch": str(torch.__version__), "opencv": cv2.__version__,
                      "source_mask_training_sha256": file_sha256(source_mask_training.__file__)}
            shutil.copyfile(__file__, output / "training_code.py")
            atomic_json(output / "config.json", config)
            update({"output": str(output), "train_images": len(records["train"]), "val_images": len(records["val"]),
                    "config": config})
            while not acquired:
                try:
                    gpu_lock.acquire(timeout=0); acquired = True
                except Timeout:
                    wait_or_raise("waiting_for_gpu")
            # Source snapshots must still match after any long GPU wait.
            source_mask_training.load_source_cohort(args.cohort, verify=True)
            model, provenance = cached_model()
            model = model.to("cuda")
            atomic_json(output / "pretrained_manifest.json", provenance)
            update({"phase": "fit_check", "parameters": sum(p.numel() for p in model.parameters())})
            fit = fit_check(model, MaskDataset(records["train"], args.width, args.height, fixed_assets=fixed_assets), args.batch_size, "cuda")
            config["batch_size"] = fit["batch_size"]
            config["model_provenance"] = provenance
            atomic_json(output / "config.json", config); atomic_json(output / "gpu_fit.json", fit)
            update({"gpu_fit": fit})
            train_loader = make_loader(records["train"], args.width, args.height, fit["batch_size"],
                                       training=True, workers=args.workers, fixed_assets=fixed_assets)
            val_loader = make_loader(records["val"], args.width, args.height, fit["batch_size"], workers=args.workers,
                                     fixed_assets=fixed_assets)
            result = train_candidate(model, train_loader, val_loader, output, config, progress=update)
            update({"phase": "complete_awaiting_comparison_review", "result": result, "finished_at": time.time()})
        except Exception as error:
            update({"phase": "error", "error_type": type(error).__name__, "error": str(error)[:700]})
            raise
        finally:
            if acquired:
                gpu_lock.release()


if __name__ == "__main__":
    main()
