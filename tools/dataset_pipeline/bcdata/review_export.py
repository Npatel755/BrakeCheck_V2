"""Incremental reviewed-only CULane export. Never launches model training."""
from __future__ import annotations

from pathlib import Path
import csv
import hashlib
import io
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import uuid

import numpy as np

from .bootstrap import atomic_json
from .config import Settings
from .db import Registry
from .export_ufld import cache_entry, validate, write_config, write_notice, write_sample
from .geometry import make_crop
from .lanes import current_geometry
from .source_validation import validated_record
from .quality import read_rgb

log = logging.getLogger(__name__)
_export_lock = threading.Lock()


def _atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _materialize(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        os.link(source, tmp)
    except OSError:
        shutil.copy2(source, tmp)
    tmp.replace(target)


def export_reviewed(settings: Settings):
    from filelock import FileLock
    with _export_lock:
        with FileLock(str(settings.path("logs", "review-export.lock")), timeout=60):
            return _export(settings)


def _export(settings):
    started = time.time()
    out = settings.path("exports", "ufld_v2", "BrakeCheck_Reviewed")
    out.mkdir(parents=True, exist_ok=True)
    previous = []
    if (out / "manifest.csv").exists():
        with (out / "manifest.csv").open(newline="", encoding="utf-8") as old_manifest:
            previous = list(csv.DictReader(old_manifest))
    report_path = settings.path("reports", "reviewed_export.json")
    # Consumers must inspect READY.json; it is absent during publication.
    (out / "READY.json").unlink(missing_ok=True)
    settings.path("exports", "road_edges", "BrakeCheck_Reviewed", "READY.json").unlink(missing_ok=True)
    atomic_json(report_path, {"state": "building", "started": started})
    snapshot_path = settings.path("exports", ".review-snapshot-" + uuid.uuid4().hex + ".sqlite")
    source = Registry(settings.db_path)
    try:
        dest = sqlite3.connect(snapshot_path)
        try:
            source.conn.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    reg = Registry(snapshot_path)
    try:
        revision = int(reg.conn.execute("SELECT value FROM local_meta WHERE key='revision'").fetchone()[0])
        all_rows = reg.images("status='curated'")
        held_missing_split = [r['image_id'] for r in all_rows if r['split'] not in ('train', 'val', 'test')]
        accepted = [r for r in all_rows if r["locked"] and r["review_status"] in ("approved", "corrected") and r["pool"] == "commercial" and r['split'] in ('train', 'val', 'test')]
        human_accepted = len(accepted)
        source_records = {}
        has_deferrals = reg.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_deferrals'").fetchone()
        deferred_ids = {r[0] for r in reg.conn.execute("SELECT image_id FROM review_deferrals")} if has_deferrals else set()
        for row in all_rows:
            if row["image_id"] in deferred_ids or row["pool"] != "commercial" or row["review_status"] in ("approved", "corrected", "rejected") or row['split'] not in ('train', 'val', 'test'):
                continue
            record = validated_record(reg, row["image_id"], verify_files=True)
            if record:
                source_records[row["image_id"]] = record
                accepted.append(row)
        reviewed = sum(r["review_status"] in ("approved", "corrected", "rejected") for r in all_rows)
        cfg = settings.get("export.ufld", {})
        lists = {key: [] for key in ("train_gt", "val_gt", "test_gt", "test", "train_gt_synthetic")}
        manifest, cache, human_samples = [], {}, []
        license_ids = set()
        counts = {"train": 0, "val": 0, "test": 0}
        errors = []
        for row in accepted:
            cur = current_geometry(reg, row["image_id"])
            source_record = source_records.get(row["image_id"])
            if cur is None or (cur[0] != "human" and source_record is None):
                raise ValueError(f"Reviewed image has no human geometry: {row['image_id']}")
            geo = cur[1]
            license_ids.update([row["license_id"], *geo.get("model_licenses", [])])
            signature = hashlib.sha256(json.dumps([geo, cfg, row["sha256"]], sort_keys=True).encode()).hexdigest()
            cached = settings.path("exports", "review_cache", signature)
            record_path = cached / "sample.json"
            if not record_path.exists():
                cached.mkdir(parents=True, exist_ok=True)
                rgb = read_rgb(row["path"])
                if rgb is None:
                    raise ValueError(f"Image missing: {row['image_id']}")
                crop_cfg = (cfg.get("crop") or {}).get(row["source"]) or (cfg.get("crop") or {}).get("default", {})
                crop = make_crop(crop_cfg, rgb.shape[1], rgb.shape[0], int(cfg.get("canvas_width", 1640)), int(cfg.get("canvas_height", 590)))
                exists, lane_rows, _ = write_sample(rgb, geo, crop, int(cfg.get("line_width", 16)), int(cfg.get("jpeg_quality", 95)), cached / "image.jpg", cached / "seg.png")
                lines = "".join(" ".join(f"{x:.3f} {y}" for x, y in pts) + " \n" for pts in lane_rows if pts)
                (cached / "lines.txt").write_text(lines, encoding="utf-8")
                atomic_json(record_path, {"exists": exists, "cache": cache_entry(lane_rows), "crop": [crop.x0, crop.y0, crop.x1, crop.y1]})
            sample = json.loads(record_path.read_text(encoding="utf-8"))
            if source_record is None:
                human_samples.append((row, geo, sample["crop"]))
            image = f"images/{row['split']}/{row['image_id']}.jpg"
            seg = f"laneseg_label_w16/images/{row['split']}/{row['image_id']}.png"
            marker = out / ".signatures" / (row["image_id"] + ".txt")
            if not marker.exists() or marker.read_text() != signature or not all((out / name).exists() for name in (image, seg, image.replace(".jpg", ".lines.txt"))):
                _materialize(cached / "image.jpg", out / image)
                _materialize(cached / "seg.png", out / seg)
                _materialize(cached / "lines.txt", out / image.replace(".jpg", ".lines.txt"))
                _atomic_text(marker, signature)
            flags = " ".join(map(str, sample["exists"]))
            lists[{"train": "train_gt", "val": "val_gt", "test": "test_gt"}[row["split"]]].append(f"/{image} /{seg} {flags}")
            if row["split"] == "test":
                lists["test"].append("/" + image)
            if row["split"] == "train":
                cache[image] = sample["cache"]
            counts[row["split"]] += 1
            manifest.append({"image": image, "image_id": row["image_id"], "split": row["split"], "source": row["source"],
                             "session_id": row["session_id"], "license": row["license_id"], "pool": row["pool"],
                             "synthetic": row["synthetic"], "label_origin": "source_validated" if source_record else "human", "decision": "source_validated" if source_record else row["review_status"],
                             "source_validation": json.dumps({"group": source_record["group_key"], "geometry_sha256": source_record["geometry_sha256"], "source_sha256": source_record["source_sha256"], "evidence": json.loads(source_record["evidence"])} if source_record else {}),
                             "geometry_version": geo["version"], "reviewer": geo.get("reviewer", ""), "model_licenses": json.dumps(geo.get("model_licenses", [])),
                             "model_provenance": json.dumps(geo.get("candidate_provenance", {})),
                             "assisted_trace_provenance": json.dumps(geo.get("assist_provenance", [])),
                             "lanes": sum(sample["exists"]), "source_ref": row["source_ref"], "source_sha256": row["sha256"]})
        for key, lines in lists.items():
            _atomic_text(out / "list" / (key + ".txt"), "".join(line + "\n" for line in lines))
        active_images = {entry["image"] for entry in manifest}
        for old in previous:
            if old["image"] not in active_images:
                image = Path(old["image"])
                targets = [out / image, out / image.with_suffix(".lines.txt"), out / "laneseg_label_w16" / image.with_suffix(".png"), out / ".signatures" / (old["image_id"] + ".txt")]
                for target in targets:
                    if not target.resolve().is_relative_to(out.resolve()):
                        raise ValueError("Old export manifest points outside the reviewed export")
                    target.unlink(missing_ok=True)
        atomic_json(out / "culane_anno_cache.json", cache)
        fields = ["image", "image_id", "split", "source", "session_id", "license", "pool", "synthetic", "label_origin", "decision", "geometry_version", "reviewer", "model_licenses", "model_provenance", "assisted_trace_provenance", "source_validation", "lanes", "source_ref", "source_sha256"]
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        writer.writerows(manifest)
        _atomic_text(out / "manifest.csv", buffer.getvalue())
        training_config = write_config(out, counts["train"], settings)
        write_notice(settings, out, license_ids)
        # Bundle the exact fetched model licences when model-derived candidates
        # have been explicitly adopted by a human reviewer.
        for model_license in license_ids:
            key = {"sam2": "sam2_large"}.get(model_license, model_license)
            source_license = settings.path("licenses", key + "-LICENSE.txt")
            if source_license.exists():
                (out / "licenses").mkdir(exist_ok=True)
                shutil.copyfile(source_license, out / "licenses" / source_license.name)
        # In addition to the original train/cache validator, check every split,
        # including negatives and existence flags, with the same dimensions.
        from PIL import Image
        for entry in manifest:
            image = entry["image"]
            mask_path = out / "laneseg_label_w16" / image.replace(".jpg", ".png")
            with Image.open(out / image) as photo, Image.open(mask_path) as mask:
                values = set(np.unique(np.asarray(mask)).tolist())
                if photo.size != (1640, 590) or mask.size != (1640, 590) or not values <= {0, 1, 2, 3, 4}:
                    errors.append(f"Invalid image/mask: {entry['image_id']}")
            human_valid = entry["label_origin"] == "human" and entry["decision"] in ("approved", "corrected")
            source_valid = entry["label_origin"] == "source_validated" and entry["decision"] == "source_validated" and entry["image_id"] in source_records
            if not (human_valid or source_valid):
                errors.append("Unreviewed image in export")
        validation = validate(out)
        errors += validation["errors"]
        sessions = {}
        for entry in manifest:
            if entry["session_id"] in sessions and sessions[entry["session_id"]] != entry["split"]:
                errors.append("Session crosses splits")
            sessions[entry["session_id"]] = entry["split"]
        # The same locked snapshot and immutable human files drive this separate
        # artifact. Road edges never occupy one of UFLD's four painted slots.
        from .road_edge_export import export_road_edges
        road_edges = export_road_edges(settings, human_samples, revision)
        report = {"state": "error" if errors else ("ready" if reviewed == len(all_rows) and accepted and not held_missing_split else "partial" if accepted else "awaiting_review"),
                  "revision": revision, "exported": len(accepted), "counts": counts, "reviewed": reviewed,
                  "human_accepted": human_accepted, "source_validated": len(source_records),
                  "held_missing_split": len(held_missing_split), "held_missing_split_ids": held_missing_split,
                  "pending": len(all_rows) - reviewed, "out": str(out), "errors": errors,
                  "validation": {"train_checked": validation["checked"], "all_splits_checked": len(manifest), "ok": not errors,
                                 "dali_gpu_loader": "not_run; preparation/review does not require CUDA"},
                  "training": "deferred", "road_edges": road_edges, "finished": time.time(), "seconds": round(time.time() - started, 2)}
        report.update(config=str(training_config), backbone=str(settings.get("export.ufld.backbone", "18")))
        if not errors:
            # A marker describes this exact snapshot. The UI also compares its
            # revision with the current registry before showing it as current.
            digest = hashlib.sha256((out / "manifest.csv").read_bytes()).hexdigest()
            atomic_json(out / "READY.json", {**report, "manifest_sha256": digest, "complete_review": reviewed == len(all_rows) and not held_missing_split,
                                           "all_training_splits_nonempty": all(counts.values())})
        atomic_json(report_path, report)
        if errors:
            raise ValueError("Export validation failed: " + "; ".join(errors[:5]))
        return report
    except Exception as exc:
        atomic_json(report_path, {"state": "error", "error": str(exc), "finished": time.time()})
        raise
    finally:
        reg.close()
        snapshot_path.unlink(missing_ok=True)
        Path(str(snapshot_path) + "-wal").unlink(missing_ok=True)
        Path(str(snapshot_path) + "-shm").unlink(missing_ok=True)


class ExportWorker:
    """Coalesce rapid phone decisions; no training or network calls."""
    def __init__(self, settings):
        self.settings = settings
        self.dirty = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="review-export")

    def start(self):
        self.thread.start()
        self.dirty.set()

    def request(self):
        self.dirty.set()

    def close(self):
        self.stop.set()
        self.dirty.set()
        if self.thread.is_alive():
            self.thread.join(timeout=15)

    def _run(self):
        while not self.stop.is_set():
            if not self.dirty.wait(timeout=1):
                continue
            # Wait for a quiet window, capped at 10 seconds for busy reviewers.
            began = time.monotonic()
            self.dirty.clear()
            while self.dirty.wait(timeout=2) and time.monotonic() - began < 10:
                self.dirty.clear()
            if self.stop.is_set():
                break
            try:
                export_reviewed(self.settings)
            except Exception:
                log.exception("Reviewed export failed; review decisions remain saved")
