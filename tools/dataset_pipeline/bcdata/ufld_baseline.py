"""Provisional upstream UFLDv2 baseline with a documented native PyTorch loader."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time

import cv2
import numpy as np

from .bootstrap import atomic_json
from .local import local_settings
from .teachers.base import file_sha256

UPSTREAM = "c903880678454dfd9b55a63022368db05c00bc6d"
WIDTH, HEIGHT = 1640, 590
ROW_ANCHORS = np.linspace(.42, 1, 72)
COL_ANCHORS = np.linspace(0, 1, 81)
DECODER_POLICIES = {
    "stock": {"row_minimum": 37, "col_minimum": 21, "minimum_mean_existence_probability": 0.0},
    "short_span_v1": {"row_minimum": 4, "col_minimum": 4, "minimum_mean_existence_probability": .75},
    "short_span_geometry_v2": {"row_minimum": 4, "col_minimum": 4, "minimum_mean_existence_probability": .75,
                               "geometry_guard": True},
    "short_span_geometry_v3": {"row_minimum": 4, "col_minimum": 4, "minimum_mean_existence_probability": .75,
                               "geometry_guard": "v3"},
}
SHORT_SPAN_NOTE = ("Exploratory decoder selected from prior validation inspection; "
                   "not an independent test score or automatic model promotion")


def interpolate(points, anchors, direction):
    """Last crossing of valid adjacent cache points, as in upstream my_interp.

    No extrapolation beyond annotated spans; negative cache sentinels never
    become positive labels when an affine transform moves their coordinates.
    """
    points = np.asarray(points, np.float32)
    out = np.full((len(anchors), 4), -1, np.float32)
    for lane, curve in enumerate(points):
        a, b = curve[:-1], curve[1:]
        valid = np.all(a > -10000, axis=1) & np.all(b > -10000, axis=1)
        delta = b[:, 1 - direction] - a[:, 1 - direction]
        valid &= np.abs(delta) > 1e-6
        for index, anchor in enumerate(anchors):
            crosses = valid & ((a[:, 1 - direction] - anchor) * (b[:, 1 - direction] - anchor) <= 0)
            indices = np.flatnonzero(crosses)
            if len(indices):
                k = indices[-1]
                fraction = (anchor - a[k, 1 - direction]) / delta[k]
                out[index, lane] = a[k, direction] + fraction * (b[k, direction] - a[k, direction])
    return out


def targets(points):
    row = interpolate(points, ROW_ANCHORS * HEIGHT, 0)
    col = interpolate(points, COL_ANCHORS * WIDTH, 1)
    rows = (row / WIDTH * 199).astype(np.int64)
    cols = (col / HEIGHT * 99).astype(np.int64)
    rows[(row < 0) | (row > WIDTH)] = -1
    cols[(col < 0) | (col > HEIGHT)] = -1
    return rows, cols


def freeze_export(settings, output):
    from filelock import FileLock
    export = settings.path("exports", "ufld_v2", "BrakeCheck_Reviewed")
    snapshot = output / "dataset"
    snapshot.mkdir()
    with FileLock(str(settings.path("logs", "review-export.lock")), timeout=90):
        ready = json.loads((export / "READY.json").read_text())
        if ready.get("errors") or not ready.get("validation", {}).get("ok"):
            raise ValueError("Reviewed export is not validated")
        raw_manifest = (export / "manifest.csv").read_bytes()
        if hashlib.sha256(raw_manifest).hexdigest() != ready["manifest_sha256"]:
            raise ValueError("Export manifest changed before snapshot")
        all_records = list(csv.DictReader(raw_manifest.decode().splitlines()))
        sessions = {split: {r["session_id"] for r in all_records if r["split"] == split} for split in ("train", "val", "test")}
        if sessions["train"] & sessions["val"] or sessions["train"] & sessions["test"] or sessions["val"] & sessions["test"]:
            raise ValueError("Recording-session leakage in export")
        records = [r for r in all_records if r["split"] in ("train", "val")]
        if any(r["decision"] not in ("approved", "corrected", "source_validated") or r["pool"] != "commercial" for r in records):
            raise ValueError("Unapproved or noncommercial training record")
        for record in records:
            if record["decision"] == "source_validated":
                proof = json.loads(record.get("source_validation") or "{}")
                if record["label_origin"] != "source_validated" or not proof.get("evidence", {}).get("human_approved_sample_ids"):
                    raise ValueError("Source-validated training record lacks sample evidence")
        cache = json.loads((export / "culane_anno_cache.json").read_text())
        # The stock export caches train only. Validation's exact slotted cache
        # lives in the immutable per-review sample, rather than guessing lane
        # identity from the order of nonempty .lines.txt records.
        for record in records:
            if record["image"] not in cache:
                signature = (export / ".signatures" / (record["image_id"] + ".txt")).read_text().strip()
                if len(signature) != 64 or any(c not in "0123456789abcdef" for c in signature):
                    raise ValueError("Invalid reviewed sample signature")
                sample = json.loads(settings.path("exports", "review_cache", signature, "sample.json").read_text())
                cache[record["image"]] = sample["cache"]
        hashes = {}
        for record in records:
            rel = Path(record["image"])
            for path in (rel, rel.with_suffix(".lines.txt"), Path("laneseg_label_w16") / rel.with_suffix(".png")):
                source, dest = export / path, snapshot / path
                if not source.resolve().is_relative_to(export.resolve()):
                    raise ValueError("Snapshot path escaped export")
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
                hashes[path.as_posix()] = file_sha256(dest)
        atomic_json(snapshot / "culane_anno_cache.json", {r["image"]: cache[r["image"]] for r in records})
        atomic_json(snapshot / "manifest.json", records)
        for name in ("manifest.json", "culane_anno_cache.json"):
            hashes[name] = file_sha256(snapshot / name)
        atomic_json(snapshot / "hashes.json", hashes)
        atomic_json(snapshot / "export_marker.json", ready)
        atomic_json(snapshot / "reserved_test_ids.json", [r["image_id"] for r in all_records if r["split"] == "test"])
        if (export / "licenses").exists():
            shutil.copytree(export / "licenses", snapshot / "licenses")
        if (export / "NOTICE.txt").exists():
            shutil.copy2(export / "NOTICE.txt", snapshot / "NOTICE.txt")
    return snapshot, records, ready


def load_frozen_export(output):
    """Verify a prepared/resumed immutable dataset before any GPU fitting."""
    snapshot = Path(output) / "dataset"
    hashes = json.loads((snapshot / "hashes.json").read_text())
    if not {"manifest.json", "culane_anno_cache.json"} <= set(hashes):
        raise ValueError("Frozen dataset is missing manifest/cache fingerprints")
    for name, sha in hashes.items():
        path = (snapshot / name).resolve()
        if not path.is_relative_to(snapshot.resolve()) or file_sha256(path) != sha:
            raise ValueError("Frozen training file changed: " + name)
    records = json.loads((snapshot / "manifest.json").read_text())
    ready = json.loads((snapshot / "export_marker.json").read_text())
    if ready.get("errors") or not ready.get("validation", {}).get("ok"):
        raise ValueError("Frozen export was not validated")
    if any(r["split"] not in ("train", "val") for r in records):
        raise ValueError("Frozen training snapshot must exclude test images")
    if any(r["decision"] not in ("approved", "corrected", "source_validated") or r["pool"] != "commercial" for r in records):
        raise ValueError("Frozen snapshot has ineligible labels")
    return snapshot, records, ready


def validate_warm_start_config(config):
    """A continuation must use the same network, anchors and input geometry."""
    expected = {"architecture": "UFLDv2", "backbone": "ResNet34", "upstream_commit": UPSTREAM,
                "input": [1600, 320], "row_anchors": ROW_ANCHORS.tolist(), "col_anchors": COL_ANCHORS.tolist()}
    for key, value in expected.items():
        if config.get(key) != value:
            raise ValueError("Incompatible warm-start checkpoint: " + key)


def validate_warm_start_splits(parent_records, reserved_test_ids, new_records):
    """Keep previously trained images/sessions out of subsequent evaluation."""
    parent_train = [r for r in parent_records if r["split"] == "train"]
    trained_ids = {r["image_id"] for r in parent_train}
    trained_sessions = {r["session_id"] for r in parent_train}
    evaluation = [r for r in new_records if r["split"] in ("val", "test")]
    if trained_ids & {r["image_id"] for r in evaluation} or trained_sessions & {r["session_id"] for r in evaluation}:
        raise ValueError("Warm-start training images or sessions entered evaluation")
    if set(reserved_test_ids) & {r["image_id"] for r in new_records if r["split"] in ("train", "val")}:
        raise ValueError("Warm-start reserved test images entered fitting or validation")


def warm_start_model(model, checkpoint, records):
    """Load only weights from our trusted local checkpoint into a new run."""
    import torch
    checkpoint = Path(checkpoint).resolve()
    digest = file_sha256(checkpoint)
    saved = torch.load(checkpoint, map_location="cpu", weights_only=False, mmap=True)
    config = saved["config"]
    validate_warm_start_config(config)
    snapshot = checkpoint.parent / "dataset"
    parent_manifest = snapshot / "manifest.json"
    if file_sha256(parent_manifest) != config["manifest_sha256"]:
        raise ValueError("Warm-start parent manifest changed")
    parent_records = json.loads(parent_manifest.read_text())
    reserved = json.loads((snapshot / "reserved_test_ids.json").read_text())
    validate_warm_start_splits(parent_records, reserved, records)
    model.load_state_dict(saved["model"], strict=True)
    return {"lane_heads": "local UFLDv2 checkpoint", "previous_initialization": config.get("initialization"),
            "warm_start": {"checkpoint": str(checkpoint), "sha256": digest,
                           "completed_epochs": saved["epoch"] + 1, "optimizer_steps": saved["step"],
                           "manifest_sha256": config["manifest_sha256"],
                           "optimizer_restored": False, "schedule": "reset for reviewed dataset"}}


class NativeDataset:
    def __init__(self, root, split, augment=False):
        if split not in ("train", "val"):
            raise ValueError("Baseline loader must not read test data")
        self.root, self.augment = Path(root), augment
        self.records = [r for r in json.loads((self.root / "manifest.json").read_text()) if r["split"] == split]
        self.cache = json.loads((self.root / "culane_anno_cache.json").read_text())
        # Augmented training computes targets after each affine transform;
        # precomputing its unused fixed targets delays large-run startup.
        self.fixed_targets = {} if augment else {r["image"]: targets(self.cache[r["image"]]) for r in self.records}

    def __len__(self):
        return len(self.records)

    def __getitem__(self, index):
        import torch
        name = self.records[index]["image"]
        image = cv2.imread(str(self.root / name))
        if image is None or image.shape[:2] != (HEIGHT, WIDTH):
            raise ValueError("Missing or incorrectly sized baseline image: " + name)
        points = np.array(self.cache[name], np.float32)
        if self.augment:
            transform = cv2.getRotationMatrix2D((WIDTH / 2, HEIGHT / 2), random.uniform(-4, 4), random.uniform(.9, 1.1))
            transform[:, 2] += [random.uniform(-80, 80), random.uniform(-30, 30)]
            image = cv2.warpAffine(image, transform, (WIDTH, HEIGHT))
            missing = points[:, :, 0] < 0
            points = points @ transform[:, :2].T + transform[:, 2]
            points[missing] = -1e5
            row, col = targets(points)
            if random.random() < .5:
                image = np.clip(image.astype(np.float32) * random.uniform(.8, 1.2), 0, 255).astype(np.uint8)
        else:
            row, col = self.fixed_targets[name]
        # Same 0.6 bottom crop and ImageNet normalization as upstream DALI.
        image = cv2.resize(image, (1600, int(320 / .6)), interpolation=cv2.INTER_LINEAR)[-320:]
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float32) / 255
        image = (image - np.array([.485, .456, .406], np.float32)) / np.array([.229, .224, .225], np.float32)
        return {"images": torch.from_numpy(np.ascontiguousarray(image.transpose(2, 0, 1))),
                "labels_row": torch.from_numpy(row.copy()), "labels_col": torch.from_numpy(col.copy()), "index": index}


def upstream_model(vendor, pretrained=True):
    sys.path.insert(0, str(vendor))
    from model.model_culane import parsingNet
    return parsingNet(pretrained=pretrained, backbone="34", num_grid_row=200, num_cls_row=72,
                      num_grid_col=100, num_cls_col=81, num_lane_on_row=4, num_lane_on_col=4,
                      use_aux=False, input_height=320, input_width=1600, fc_norm=True)


def criterion(pred, batch):
    """Upstream focal/mean/existence losses; empty localization targets contribute zero."""
    import torch
    from utils.loss import SoftmaxFocalLoss, MeanLoss
    result = {}
    loss = pred["loc_row"].sum() * 0
    for axis in ("row", "col"):
        labels = batch["labels_" + axis]
        logits = pred["loc_" + axis].float()
        focal = SoftmaxFocalLoss(2, ignore_lb=-1)(logits, labels) if (labels >= 0).any() else logits.sum() * 0
        mean = MeanLoss()(logits, labels) if (labels >= 0).any() else logits.sum() * 0
        existence = torch.nn.functional.cross_entropy(pred["exist_" + axis].float(), (labels >= 0).long())
        loss = loss + focal + .05 * mean + existence
        result[axis] = float((focal + .05 * mean + existence).detach())
    return loss, result


def decode(pred, index, policy="stock"):
    """Hybrid local softargmax; keep upstream gating as the historical default.

    short_span_v1 is a separately reported, validation-selected experiment. It
    keeps confident short observed traces without changing labels or geometry.
    """
    if policy not in DECODER_POLICIES:
        raise ValueError("Unknown UFLDv2 decoder policy: " + str(policy))
    if policy == "recall_budget_v4":
        from .recall_search import candidate_heads, select_indices
        candidates = candidate_heads(pred, index)
        return [candidates[i]["points"] for i in select_indices(candidates, DECODER_POLICIES[policy])]
    rules = DECODER_POLICIES[policy]
    lanes = []
    for axis, slots, anchors, extent in (("row", (1, 2), ROW_ANCHORS, WIDTH),
                                       ("col", (0, 3), COL_ANCHORS, HEIGHT)):
        logits = pred["loc_" + axis][index].float().cpu()
        existence = pred["exist_" + axis][index].float().cpu()
        valid = existence.argmax(0).numpy().astype(bool)
        centers = logits.argmax(0)
        for slot in slots:
            selected = valid[:, slot]
            if selected.sum() < rules[axis + "_minimum"]:
                continue
            if rules["minimum_mean_existence_probability"]:
                probability = existence[:, selected, slot].softmax(0)[1].mean().item()
                if not math.isfinite(probability) or probability < rules["minimum_mean_existence_probability"]:
                    continue
            points = []
            for anchor_index in np.flatnonzero(selected):
                center = int(centers[anchor_index, slot])
                start, stop = max(0, center - 1), min(logits.shape[0], center + 2)
                scores = logits[start:stop, anchor_index, slot].softmax(0).numpy()
                coord = (np.dot(scores, np.arange(start, stop)) + .5) / (logits.shape[0] - 1) * extent
                point = [coord, anchors[anchor_index] * HEIGHT] if axis == "row" else [anchors[anchor_index] * WIDTH, coord]
                if 0 <= point[0] < WIDTH and 0 <= point[1] < HEIGHT:
                    points.append(point)
            if len(points) >= 2:
                if rules.get("geometry_guard"):
                    from .lane_improvements import credible_trace, GEOMETRY_RULES, STRICT_GEOMETRY_RULES
                    if not credible_trace(points, GEOMETRY_RULES if rules["geometry_guard"] == "v3" else STRICT_GEOMETRY_RULES):
                        continue
                lanes.append(points)
    return lanes


def validation_metrics(loss, counts):
    """Summarize one decoder without changing the historical scalar fields."""
    tp, fp, fn = (counts[key] for key in ("tp", "fp", "fn"))
    precision, recall = tp / max(1, tp + fp), tp / max(1, tp + fn)
    return {"loss": loss, "precision": precision, "recall": recall,
            "lane_f1": 2 * tp / max(1, 2 * tp + fp + fn), **counts,
            "metric": "Python raster lane F1, width 30 px, IoU >= 0.5; not official CULane evaluator"}


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--accumulation", type=int, default=4)
    parser.add_argument("--learning-rate", type=float, default=.00625)
    parser.add_argument("--warmup-steps", type=int, default=110)
    parser.add_argument("--evaluate-initial", action="store_true",
                        help="Record warm-start stock and experimental validation metrics before fitting")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--source-sampling", choices=("uniform", "balanced"), default="uniform")
    parser.add_argument("--recall-policy", type=Path, help="Pinned recall decoder and validation false-positive budget")
    parser.add_argument("--error-review-root", type=Path, help="Save separate validation error collections under this data root")
    run_mode = parser.add_mutually_exclusive_group()
    run_mode.add_argument("--resume-run", type=Path)
    run_mode.add_argument("--prepared-run", type=Path)
    parser.add_argument("--experimental-data", action="store_true",
                        help="Explicitly allow isolated teacher-pseudolabel train additions; held-out data stays frozen")
    parser.add_argument("--status-report", type=Path,
                        help="Separate JSON progress report; experimental runs never overwrite the production report")
    parser.add_argument("--warm-start", type=Path, help="Trusted local checkpoint weights; new optimizer and schedule")
    parser.add_argument("--gpu-lock-timeout", type=float, default=0)
    args = parser.parse_args(argv)
    if args.epochs < 1:
        parser.error("Epochs must be positive")
    if args.batch_size < 1 or args.accumulation < 1:
        parser.error("Batch size and gradient accumulation must be positive")
    if not math.isfinite(args.learning_rate) or args.learning_rate <= 0:
        parser.error("Learning rate must be finite and positive")
    if args.warmup_steps < 0:
        parser.error("Warmup steps must be nonnegative")
    if not math.isfinite(args.gpu_lock_timeout) or args.gpu_lock_timeout < -1:
        parser.error("GPU lock timeout must be -1 (wait) or nonnegative")
    if args.resume_run and args.warm_start:
        parser.error("Use --warm-start for new data or --resume-run for the unchanged frozen dataset")
    if args.evaluate_initial and (not args.warm_start or args.resume_run):
        parser.error("--evaluate-initial requires a new --warm-start run")
    if args.experimental_data and not (args.prepared_run or args.resume_run):
        parser.error("--experimental-data requires an isolated --prepared-run or --resume-run")
    return args


def training_report_path(args, settings, output):
    """Keep optional teacher-data experiments out of production progress state."""
    production = settings.path("reports", "ufld_training.json").resolve()
    experimental = bool(getattr(args, "experimental_data", False))
    requested = getattr(args, "status_report", None)
    report = Path(requested).resolve() if requested else (
        (Path(output) / "experiment_training.json").resolve() if experimental else production)
    if report.suffix.lower() != ".json":
        raise ValueError("Training status report must be a JSON path")
    if report.is_relative_to((Path(output) / "dataset").resolve()):
        raise ValueError("Training status cannot overwrite frozen dataset files")
    if experimental and report == production:
        raise ValueError("Experimental training cannot overwrite the production status report")
    if report == (Path(output) / "config.json").resolve():
        raise ValueError("Training status cannot overwrite checkpoint configuration")
    return report


def load_prepared_export(output, *, experimental=False):
    if experimental:
        marker = json.loads((Path(output) / "dataset" / "export_marker.json").read_text())
        if marker.get("format") == "ufld_zod_cleared_v1":
            from .zod_training import load
            return load(output)
        if marker.get("format") == "ufld_collection_experiment_v1":
            from .ufld_collection_experiment import load_collection_export
            return load_collection_export(output)
        from .ufld_experiment import load_experiment_export
        return load_experiment_export(output)
    return load_frozen_export(output)


def training_schedule(args, saved_config=None):
    """Resume the recorded base rate/warmup, including pre-option checkpoints."""
    config = saved_config if saved_config is not None else vars(args)
    learning_rate = config.get("learning_rate", .00625)
    warmup_steps = config.get("warmup_steps", 110)
    if (not isinstance(learning_rate, (int, float)) or isinstance(learning_rate, bool)
            or not math.isfinite(learning_rate) or learning_rate <= 0):
        raise ValueError("Stored learning rate must be finite and positive")
    if not isinstance(warmup_steps, int) or isinstance(warmup_steps, bool) or warmup_steps < 0:
        raise ValueError("Stored warmup steps must be a nonnegative integer")
    return learning_rate, warmup_steps


def validate_resume_decoder_policies(config):
    recorded = config.get("decoder_policies")
    if recorded is not None and any(DECODER_POLICIES.get(k) != v for k, v in recorded.items()):
        raise ValueError("Resume decoder policies changed; use a new warm-start run")
    if "geometry_rules" in config:
        from .lane_improvements import GEOMETRY_RULES
        if config["geometry_rules"] != GEOMETRY_RULES:
            raise ValueError("Resume geometry rules changed; use a new warm-start run")


def schedule_multiplier(step, total_steps, warmup_steps):
    warmup = min(1., (step + 1) / warmup_steps) if warmup_steps else 1.
    return warmup * .5 * (1 + math.cos(math.pi * step / max(1, total_steps)))


def lane_counts(predicted, reference):
    from scipy.optimize import linear_sum_assignment
    def raster(points):
        mask = np.zeros((HEIGHT, WIDTH), np.uint8)
        if len(points) >= 2:
            cv2.polylines(mask, [np.round(points).astype(np.int32)], False, 1, thickness=30)
        return mask.astype(bool)
    p, r = [raster(lane) for lane in predicted], [raster(lane) for lane in reference]
    overlaps = np.zeros((len(p), len(r)), np.float32)
    for i, a in enumerate(p):
        for j, b in enumerate(r):
            overlaps[i, j] = np.count_nonzero(a & b) / max(1, np.count_nonzero(a | b))
    if len(p) and len(r):
        a, b = linear_sum_assignment(-overlaps)
        tp = int(np.count_nonzero(overlaps[a, b] >= .5))
    else:
        tp = 0
    return tp, len(p) - tp, len(r) - tp


def validate(model, loader, device, max_batches=None, error_collection=None):
    import torch
    model.eval()
    total_loss, batches = 0., 0
    totals = {name: dict(tp=0, fp=0, fn=0, negative_images=0, correct_negative_images=0, images=0)
              for name in DECODER_POLICIES}
    with torch.inference_mode():
        for batch in loader:
            indices = batch["index"].tolist()
            batch = {key: value.to(device) for key, value in batch.items() if key != "index"}
            with torch.autocast("cuda", dtype=torch.bfloat16):
                pred = model(batch["images"])
            loss, _ = criterion(pred, batch)
            total_loss += float(loss); batches += 1
            for k, index in enumerate(indices):
                name = loader.dataset.records[index]["image"]
                reference = []
                for line in (loader.dataset.root / Path(name).with_suffix(".lines.txt")).read_text().splitlines():
                    values = np.array([float(v) for v in line.split()]).reshape(-1, 2)
                    if len(values) >= 2:
                        reference.append(values)
                predictions = {}
                for policy, counts in totals.items():
                    predicted = decode(pred, k, policy)
                    predictions[policy] = predicted
                    a, b, c = lane_counts(predicted, reference)
                    counts["tp"] += a; counts["fp"] += b; counts["fn"] += c
                    counts["images"] += 1
                    if not reference:
                        counts["negative_images"] += 1
                        counts["correct_negative_images"] += not predicted
                if error_collection is not None:
                    error_collection.add(loader.dataset.records[index], [r.tolist() for r in reference], predictions)
            if max_batches and batches >= max_batches:
                break
    loss = total_loss / max(1, batches)
    metrics = validation_metrics(loss, totals["stock"])
    metrics["decoder_policy"] = "stock"
    metrics["decoder_comparisons"] = {
        policy: {**validation_metrics(loss, totals[policy]), "decoder_policy": policy, "interpretation": SHORT_SPAN_NOTE}
        for policy in DECODER_POLICIES if policy != "stock"}
    if error_collection is not None:
        error_collection.finish(metrics)
    return metrics


def atomic_checkpoint(path, value):
    import torch
    temporary = path.with_suffix(".tmp")
    torch.save(value, temporary)
    os.replace(temporary, path)


def save_training_checkpoint(output, model, optimizer, *, epoch, step, history,
                             config, metrics, best, best_short_span, initial_baseline=False, best_geometry=-1.,
                             best_recall=-1., recall_fp_budget=None):
    """Select each policy independently, including the untouched warm-start.

    An initial assessment is epoch -1/step 0 so its last.pt resumes at epoch 0.
    Keeping the starting weights prevents fine-tuning regressions from silently
    replacing the best model for either decoder.
    """
    improved = metrics["lane_f1"] > best
    candidate_f1 = metrics["decoder_comparisons"]["short_span_v1"]["lane_f1"]
    candidate_improved = candidate_f1 > best_short_span
    geometry = metrics["decoder_comparisons"].get("short_span_geometry_v3", {}).get("lane_f1", -1.)
    best, best_short_span = max(best, metrics["lane_f1"]), max(best_short_span, candidate_f1)
    saved = {"model": model.state_dict(), "optimizer": optimizer.state_dict(),
             "epoch": epoch, "step": step, "best": best, "best_short_span": best_short_span,
             "history": history, "config": config, "validation": metrics,
             "initial_baseline": initial_baseline}
    saved["best_geometry"] = max(best_geometry, geometry)
    recall_metrics = metrics["decoder_comparisons"].get("recall_budget_v4", {})
    recall_score = recall_metrics.get("recall", -1.)
    recall_eligible = recall_fp_budget is not None and recall_metrics.get("fp", float("inf")) <= recall_fp_budget
    saved["best_recall"] = max(best_recall, recall_score) if recall_eligible else best_recall
    atomic_checkpoint(output / "last.pt", saved)
    if improved:
        atomic_checkpoint(output / "best.pt", saved)
    if candidate_improved:
        atomic_checkpoint(output / "best_short_span.pt", saved)
    if geometry > best_geometry:
        atomic_checkpoint(output / "best_geometry.pt", saved)
    if recall_eligible and recall_score > best_recall:
        atomic_checkpoint(output / "best_recall.pt", saved)
    return best, best_short_span


def main():
    args = parse_arguments()
    recall_config = None
    if args.recall_policy:
        from .recall_search import load_policy
        recall_config = load_policy(args.recall_policy)
        DECODER_POLICIES["recall_budget_v4"] = recall_config["rule"]
    import torch
    from torch.utils.data import DataLoader
    from filelock import FileLock
    torch.set_num_threads(4); cv2.setNumThreads(1)
    random.seed(0); np.random.seed(0); torch.manual_seed(0)
    settings = local_settings()
    supplied_run = args.resume_run or args.prepared_run
    output = supplied_run.resolve() if supplied_run else settings.path("models", "ufld_baseline", "run-" + time.strftime("%Y%m%d-%H%M%S"))
    output.mkdir(parents=True, exist_ok=bool(supplied_run))
    report = training_report_path(args, settings, output)
    state = {"pid": os.getpid(), "phase": "preparing", "architecture": "UFLDv2", "backbone": "ResNet34",
             "epochs": args.epochs, "provisional": True, "auto_promote": False, "test_images_used": 0,
              "experimental": args.experimental_data, "status_report": str(report),
             "output": str(output), "upstream_commit": UPSTREAM, "loader": "native PyTorch adapter; DALI not used"}
    def update(**changes):
        state.update(changes, updated_at=time.time())
        atomic_json(report, state)
        atomic_json(output / "status.json", state)
    update()
    update(phase="waiting_for_gpu")
    with FileLock(str(settings.path("logs", "large-models.lock")), timeout=args.gpu_lock_timeout):
        try:
            if not torch.cuda.is_available():
                raise RuntimeError("CUDA GPU required")
            vendor = Path(__file__).resolve().parents[1] / "vendor" / "ufld_v2"
            commit = subprocess.check_output(["git", "-C", str(vendor), "rev-parse", "HEAD"], text=True).strip()
            if commit != UPSTREAM:
                raise RuntimeError("UFLDv2 upstream revision mismatch")
            patch = subprocess.check_output(["git", "-C", str(vendor), "diff"], text=True)
            (output / "upstream.patch").write_text(patch)
            shutil.copy2(vendor / "LICENSE", output / "UFLDv2-LICENSE.txt")
            shutil.copy2(Path(__file__), output / "training_code.py")
            if supplied_run:
                snapshot, records, ready = load_prepared_export(output, experimental=args.experimental_data)
            else:
                snapshot, records, ready = freeze_export(settings, output)
            train_data, val_data = NativeDataset(snapshot, "train", True), NativeDataset(snapshot, "val")
            if recall_config and (recall_config["manifest_sha256"] != file_sha256(snapshot / "manifest.json")
                                  or recall_config["validation_images"] != len(val_data)):
                raise ValueError("Recall budget was calibrated on another frozen validation dataset")
            if not len(train_data) or not len(val_data):
                raise ValueError("Nonempty train and validation sets required")
            update(phase="loading_model", train_images=len(train_data), val_images=len(val_data),
                   export_revision=ready["revision"], manifest_sha256=file_sha256(snapshot / "manifest.json"))
            if args.experimental_data:
                state["dataset_experiment"] = json.loads((snapshot / "experiment.json").read_text(encoding="utf-8"))
            from .lane_improvements import source_sampling, GEOMETRY_RULES
            weights, sampling = source_sampling(train_data.records, args.source_sampling)
            update(source_sampling=sampling)
            sampler_generator = torch.Generator()
            sampler = torch.utils.data.WeightedRandomSampler(weights, len(train_data), replacement=True,
                        generator=sampler_generator) if weights is not None else None
            train_loader = DataLoader(train_data, batch_size=args.batch_size, shuffle=sampler is None, sampler=sampler,
                                      num_workers=0, pin_memory=True, drop_last=True)
            val_loader = DataLoader(val_data, batch_size=args.batch_size, shuffle=False, num_workers=0, pin_memory=True)
            model = upstream_model(vendor, pretrained=not bool(args.resume_run or args.warm_start)).cuda()
            total_steps = args.epochs * math.ceil(len(train_loader) / args.accumulation)
            history, step, start_epoch, best, best_short_span = [], 0, 0, -1., -1.
            best_geometry = -1.
            best_recall = -1.
            recall_budget = recall_config["false_positive_budget"] if recall_config else None
            if args.warm_start:
                state["initialization"] = warm_start_model(model, args.warm_start, records)
            learning_rate, warmup_steps = training_schedule(args)
            optimizer = torch.optim.SGD(model.parameters(), lr=learning_rate, momentum=.9, weight_decay=.0001)
            if args.resume_run:
                # Only load our own trusted local checkpoint, never remote pickle.
                saved = torch.load(output / "last.pt", map_location="cpu", weights_only=False, mmap=True)
                validate_resume_decoder_policies(saved["config"])
                if saved["config"].get("source_sampling", {}).get("mode", "uniform") != args.source_sampling:
                    raise ValueError("Resume source sampling changed; use a new warm-start run")
                if saved["config"].get("recall_policy") != recall_config:
                    raise ValueError("Resume recall policy or budget changed; use a new warm-start run")
                model.load_state_dict(saved["model"]); optimizer.load_state_dict(saved["optimizer"])
                step, start_epoch, best, history = saved["step"], saved["epoch"] + 1, saved["best"], saved["history"]
                state["initialization"] = saved["config"].get("initialization")
                learning_rate, warmup_steps = training_schedule(args, saved["config"])
                best_short_span = saved.get("best_short_span", -1.)
                best_geometry = saved.get("best_geometry", -1.)
                best_recall = saved.get("best_recall", -1.)
                del saved
            elif not args.warm_start:
                from torchvision.models import ResNet34_Weights
                url = ResNet34_Weights.IMAGENET1K_V1.url
                initial = Path(torch.hub.get_dir()) / "checkpoints" / url.rsplit("/", 1)[1]
                state["initialization"] = {"backbone": "torchvision ResNet34 ImageNet1K V1", "url": url,
                                           "sha256": file_sha256(initial), "lane_heads": "random", "lane_checkpoint": None}
            config = {**state, "batch_size": args.batch_size, "accumulation": args.accumulation,
                      "effective_batch_size": args.batch_size * args.accumulation, "learning_rate": learning_rate,
                      "warmup_steps": warmup_steps, "decoder_policies": DECODER_POLICIES,
                      "primary_decoder": "stock", "short_span_interpretation": SHORT_SPAN_NOTE,
                      "source_sampling": sampling, "geometry_rules": GEOMETRY_RULES,
                      "recall_policy": recall_config,
                      "initial_assessment_requested": args.evaluate_initial,
                      "checkpoint_selection": {"best.pt": "stock lane F1", "best_short_span.pt": "short_span_v1 lane F1"},
                      "seed": 0, "input": [1600, 320], "row_anchors": ROW_ANCHORS.tolist(), "col_anchors": COL_ANCHORS.tolist(),
                      "adapter_differences": ["native image decoder/affine/interpolation", "conservative annotation spans without upstream bottom extrapolation", "empty localization targets contribute zero loss", "no DALI/my_interp extension", "Python raster validation F1; no official CULane test score"]}
            atomic_json(output / "config.json", config)
            def error_collection(stage):
                if args.error_review_root is None:
                    return None
                if args.error_review_root.resolve() != settings.root.resolve():
                    raise ValueError("Error review root must be this training data root")
                from .lane_error_artifacts import ErrorCollection
                return ErrorCollection(settings.root, output.name + "-" + stage, snapshot,
                    {"run": str(output), "stage": stage, "optimizer_step": step,
                     "manifest_sha256": config["manifest_sha256"],
                     "warm_start": str(args.warm_start) if args.warm_start else None,
                     "decoder_policies": DECODER_POLICIES, "geometry_rules": GEOMETRY_RULES})
            if args.evaluate_initial:
                update(phase="initial_validation", step=0)
                metrics = validate(model, val_loader, "cuda", error_collection=error_collection("initial"))
                initial = {"stage": "before_optimizer_updates", "validation": metrics,
                           "manifest_sha256": config["manifest_sha256"],
                           "warm_start": state["initialization"]["warm_start"]}
                atomic_json(output / "initial_validation.json", initial)
                best, best_short_span = save_training_checkpoint(
                    output, model, optimizer, epoch=-1, step=0, history=history,
                    config=config, metrics=metrics, best=best, best_short_span=best_short_span,
                    initial_baseline=True, best_recall=best_recall, recall_fp_budget=recall_budget)
                update(initial_validation=metrics, validation=metrics, best_val_f1=best,
                       best_short_span_val_f1=best_short_span)
                best_geometry = metrics["decoder_comparisons"]["short_span_geometry_v3"]["lane_f1"]
                rm = metrics["decoder_comparisons"].get("recall_budget_v4", {})
                if recall_budget is not None and rm.get("fp", float("inf")) <= recall_budget:
                    best_recall = max(best_recall, rm["recall"])
            torch.cuda.reset_peak_memory_stats()
            update(phase="training", step=step, total_steps=total_steps, trainable_parameters=sum(p.numel() for p in model.parameters()))
            for epoch in range(start_epoch, args.epochs):
                sampler_generator.manual_seed(epoch)
                model.train(); optimizer.zero_grad(set_to_none=True)
                running, seen, accumulated = 0., 0, 0
                for batch_index, batch in enumerate(train_loader):
                    batch.pop("index")
                    batch = {key: value.cuda(non_blocking=True) for key, value in batch.items()}
                    with torch.autocast("cuda", dtype=torch.bfloat16):
                        pred = model(batch["images"])
                    loss, components = criterion(pred, batch)
                    if not torch.isfinite(loss):
                        raise RuntimeError("Nonfinite UFLD loss")
                    group_size = min(args.accumulation, len(train_loader) - batch_index // args.accumulation * args.accumulation)
                    (loss / group_size).backward()
                    accumulated += 1; running += float(loss.detach()) * len(batch["images"]); seen += len(batch["images"])
                    if accumulated == group_size:
                        rate = schedule_multiplier(step, total_steps, warmup_steps)
                        for group in optimizer.param_groups:
                            group["lr"] = learning_rate * rate
                        norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 10., error_if_nonfinite=True)
                        optimizer.step(); optimizer.zero_grad(set_to_none=True); step += 1; accumulated = 0
                        update(phase="training", epoch=epoch + 1, step=step, total_steps=total_steps,
                               train_loss=running / seen, loss=float(loss.detach()), learning_rate=optimizer.param_groups[0]["lr"],
                               peak_vram_gib=round(torch.cuda.max_memory_allocated() / 2**30, 3))
                        if args.smoke and step >= 2:
                            torch.cuda.synchronize()
                            metrics = validate(model, val_loader, "cuda", max_batches=1)
                            update(phase="smoke_passed", gpu_optimizer_steps=step, components=components, validation=metrics)
                            return
                update(phase="validation", epoch=epoch + 1)
                metrics = validate(model, val_loader, "cuda", error_collection=error_collection("epoch-" + str(epoch + 1)))
                entry = {"epoch": epoch + 1, "step": step, "train_loss": running / seen, "validation": metrics}
                history.append(entry)
                best, best_short_span = save_training_checkpoint(
                    output, model, optimizer, epoch=epoch, step=step, history=history,
                    config=config, metrics=metrics, best=best, best_short_span=best_short_span, best_geometry=best_geometry,
                    best_recall=best_recall, recall_fp_budget=recall_budget)
                best_geometry = max(best_geometry, metrics["decoder_comparisons"]["short_span_geometry_v3"]["lane_f1"])
                rm = metrics["decoder_comparisons"].get("recall_budget_v4", {})
                if recall_budget is not None and rm.get("fp", float("inf")) <= recall_budget:
                    best_recall = max(best_recall, rm["recall"])
                atomic_json(output / "history.json", history)
                update(phase="training", validation=metrics, best_val_f1=best, best_short_span_val_f1=best_short_span,
                       best_geometry_val_f1=best_geometry, source_sampling=sampling,
                       best_recall_within_budget=best_recall if best_recall >= 0 else None,
                       false_positive_budget=recall_budget)
            candidate = output / "best_short_span.pt"
            update(phase="trained_awaiting_review", checkpoint=str(output / "best.pt"), checkpoint_sha256=file_sha256(output / "best.pt"),
                   short_span_checkpoint=str(candidate) if candidate.exists() else None,
                   short_span_checkpoint_sha256=file_sha256(candidate) if candidate.exists() else None)
        except Exception as error:
            update(phase="error", error_type=type(error).__name__, error_reason=str(error)[:600])
            raise


if __name__ == "__main__":
    main()
