"""Read-only catalog and previews of frozen datasets, independent of review queues.

This module deliberately does not import Registry, ReviewStore or training code.
Counts describe immutable snapshots; candidate counts never imply trainable labels.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
import hashlib
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import threading
from typing import Callable
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,180}\Z")
_FRIENDLY = {"a2d2": "Audi A2D2", "comma10k": "comma10k"}
_API = "/api/studio/datasets"
_PREVIEW = "/static/dataset_samples/carlane-tulane"


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _inside(root: Path, value: str | Path) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError("Missing artifact path")
    path = Path(value)
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Artifact path leaves its allowed directory")
    return path


def _asset(snapshot: Path, value: str, *, resolve=True) -> Path:
    # Never interpret platform-specific drive paths or alternate separators.
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("Invalid snapshot image path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or PureWindowsPath(value).is_absolute() or ".." in relative.parts:
        raise ValueError("Invalid snapshot image path")
    # Manifest validation is structural; only selected files need filesystem
    # resolution. Resolving every image in a 15k snapshot is slow on OneDrive.
    path = _inside(snapshot, Path(*relative.parts)) if resolve else snapshot / Path(*relative.parts)
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
        raise ValueError("Snapshot image type is not supported")
    return path


def _metrics(status):
    result = {}
    for key in ("best_val_f1", "best_short_span_val_f1"):
        value = status.get(key)
        if type(value) in (float, int) and math.isfinite(value) and 0 <= value <= 1:
            result[key] = value
    return result


def _progress(value):
    """Accept only small scalar progress fields, including partial atomic updates."""
    if not isinstance(value, dict):
        return None
    result = {}
    for key in ("phase", "split", "processed", "total", "epoch", "epochs", "step", "steps",
                "optimizer_steps", "train_images", "val_images", "loss", "updated_at"):
        item = value.get(key)
        if isinstance(item, str) and len(item) <= 200:
            result[key] = item
        elif type(item) in (float, int) and math.isfinite(item) and item >= 0:
            result[key] = item
    return result or None


class StudioDatasets:
    def __init__(self, data_root: Path, *, preview_root: Path | None = None):
        self.root = Path(data_root).resolve()
        self.baselines = _inside(self.root, "models/ufld_baseline")
        self.experiments = _inside(self.root, "experiments/flagged_teacher")
        self.preview_root = Path(preview_root).resolve() if preview_root is not None else (
            Path(__file__).resolve().parents[1] / "review_ui/dataset_samples/carlane-tulane")
        self._cache = {}
        self._base_cache = {}
        self._lock = threading.RLock()

    def _read(self, path: Path):
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._cache.get(str(path))
            if cached and cached[0] == key:
                return cached[1], cached[2]
        raw = path.read_bytes()
        value = json.loads(raw)
        digest = hashlib.sha256(raw).hexdigest()
        with self._lock:
            self._cache[str(path)] = (key, value, digest)
        return value, digest

    def _optional(self, root, relative, warnings):
        try:
            value, _ = self._read(_inside(root, relative))
            if not isinstance(value, dict):
                raise ValueError("Object expected")
            return value
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError):
            warnings.append(f"{relative} is unavailable or being updated")
            return {}

    def _active(self, warnings):
        if not self.experiments.is_dir():
            return None, None
        for child in sorted(self.experiments.iterdir(), key=lambda item: item.name, reverse=True):
            if not child.name.startswith("run-") or not _ID.fullmatch(child.name) or ".." in child.name:
                continue
            try:
                run = _inside(self.experiments, child.name)
                path = _inside(run, "experiment.json")
                if not path.is_file():
                    continue
                metadata, _ = self._read(path)
                if not isinstance(metadata, dict):
                    raise ValueError("Object expected")
                return run, metadata
            except (OSError, ValueError, TypeError):
                warnings.append("Latest experiment metadata is unavailable or being updated")
                # Do not silently present an older experiment as the active one.
                return None, None
        return None, None

    def _base(self, experiment, warnings):
        production = self._optional(self.root, "reports/ufld_training.json", warnings)
        value = experiment.get("base_run") if experiment else production.get("output")
        if not value:
            raise ValueError("No frozen cleared dataset is available yet")
        candidate = _inside(self.root, value)
        run = _inside(self.baselines, candidate)
        if run == self.baselines or not run.is_dir():
            raise ValueError("Cleared base run is unavailable")
        snapshot = _inside(run, "dataset")
        expected = experiment.get("base_manifest_sha256") if experiment else production.get("manifest_sha256")
        fingerprints = []
        for name in ("dataset/manifest.json", "dataset/hashes.json", "dataset/export_marker.json",
                     "dataset/reserved_test_ids.json", "status.json"):
            try:
                stat = _inside(run, name).stat()
                fingerprints.append((name, stat.st_mtime_ns, stat.st_size))
            except FileNotFoundError:
                fingerprints.append((name, None, None))
        base_key = (str(run), expected, tuple(fingerprints))
        with self._lock:
            cached = self._base_cache.get(base_key)
            if cached:
                return cached
        records, sha = self._read(_inside(snapshot, "manifest.json"))
        hashes, _ = self._read(_inside(snapshot, "hashes.json"))
        ready, _ = self._read(_inside(snapshot, "export_marker.json"))
        reserved, _ = self._read(_inside(snapshot, "reserved_test_ids.json"))
        if not isinstance(hashes, dict) or hashes.get("manifest.json") != sha:
            raise ValueError("Cleared base manifest fingerprint does not match")
        if expected and expected != sha:
            raise ValueError("Selected base manifest fingerprint does not match")
        if not isinstance(ready, dict) or ready.get("errors") or ready.get("validation", {}).get("ok") is not True:
            raise ValueError("Cleared base export has not passed validation")
        if not isinstance(records, list) or not records:
            raise ValueError("Cleared base manifest is empty")
        ids = set()
        for record in records:
            if (not isinstance(record, dict) or record.get("split") not in ("train", "val")
                    or record.get("pool") != "commercial"
                    or record.get("decision") not in ("approved", "corrected", "source_validated")
                    or not isinstance(record.get("image_id"), str) or not _ID.fullmatch(record["image_id"])
                    or ".." in record["image_id"] or record["image_id"] in ids):
                raise ValueError("Cleared base contains invalid or ineligible records")
            _asset(snapshot, record.get("image"), resolve=False)
            ids.add(record["image_id"])
        if (not isinstance(reserved, list) or any(not isinstance(item, str) for item in reserved)
                or len(set(reserved)) != len(reserved) or ids.intersection(reserved)):
            raise ValueError("Reserved test identities are invalid or overlap the base")
        if isinstance(ready.get("counts"), dict):
            actual_counts = Counter(record["split"] for record in records)
            actual_counts["test"] = len(reserved)
            if any(ready["counts"].get(split) != actual_counts[split] for split in ("train", "val", "test")):
                raise ValueError("Frozen export counts do not match the base and reserved identities")
        if hashes.get("reserved_test_ids.json"):
            if hashes["reserved_test_ids.json"] != self._read(_inside(snapshot, "reserved_test_ids.json"))[1]:
                raise ValueError("Reserved test fingerprint does not match")
        status = self._optional(run, "status.json", warnings)
        counts = Counter(record["split"] for record in records)
        sources = []
        for source in sorted({str(record.get("source") or "unknown") for record in records}):
            source_counts = Counter(r["split"] for r in records if str(r.get("source") or "unknown") == source)
            sources.append({"source": source, "title": _FRIENDLY.get(source, source),
                            "train_images": source_counts["train"], "val_images": source_counts["val"]})
        base = {"available": True, "run_id": run.name, "manifest_sha256": sha,
                "train_images": counts["train"], "val_images": counts["val"],
                "reserved_test_images": len(reserved), "test_images_used_for_training": 0,
                "sources": sources, "metrics": _metrics(status),
                "preview_sample_url": _API + "/base/samples?offset=0&limit=3",
                "label_policy": "Cleared human decisions and audited source conversions; painted lane boundaries only.",
                "reserved_test_context": "Reserved test identities stay outside training and validation; previews use training images."}
        result = (base, run, snapshot, records)
        with self._lock:
            if len(self._base_cache) >= 8:
                self._base_cache.clear()
            self._base_cache[base_key] = result
        return result

    def _converted_base(self, experiment, warnings):
        """Select the canonical cleared collection without repinning experiments."""
        try:
            pointer, _ = self._read(_inside(self.root, "reports/converted_data.json"))
        except FileNotFoundError:
            return self._base(experiment, warnings)
        if (not isinstance(pointer, dict) or pointer.get("schema") != "converted_data:v1"
                or not isinstance(pointer.get("run"), str) or not pointer["run"]
                or not isinstance(pointer.get("manifest_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", pointer["manifest_sha256"])):
            raise ValueError("Canonical converted dataset pointer is invalid")
        return self._base({"base_run": pointer["run"],
                           "base_manifest_sha256": pointer["manifest_sha256"]}, warnings)

    def _prepared_extras(self, run, metadata, base, records, warnings):
        value = metadata.get("ufld_run", "ufld_run")
        ufld = _inside(run, value)
        path = _inside(ufld, "dataset/experiment.json")
        if not path.is_file():
            return None, {}, []
        report, _ = self._read(path)
        if (not isinstance(report, dict) or report.get("format") != "ufld_teacher_experiment_v1"
                or report.get("experimental") is not True or report.get("auto_promote") is not False
                or report.get("base_manifest_sha256") != base["manifest_sha256"]
                or report.get("test_images_used") != 0
                or report.get("base_train_images") != base["train_images"]
                or report.get("base_val_images") != base["val_images"]):
            raise ValueError("Experiment training snapshot does not match the cleared base")
        extra = _count(report.get("extra_train_images"))
        if (extra is None or extra == 0 or report.get("train_images") != base["train_images"] + extra
                or report.get("val_images") != base["val_images"]):
            raise ValueError("Experiment training counts are inconsistent")
        manifest, sha = self._read(_inside(ufld, "dataset/manifest.json"))
        hashes, _ = self._read(_inside(ufld, "dataset/hashes.json"))
        if not isinstance(hashes, dict) or hashes.get("manifest.json") != sha:
            raise ValueError("Experiment manifest fingerprint does not match")
        if (not isinstance(manifest, list) or manifest[:len(records)] != records
                or len(manifest) != len(records) + extra
                or any(not isinstance(r, dict) or r.get("split") != "train" for r in manifest[len(records):])):
            raise ValueError("Experiment did not preserve the full cleared base and train-only additions")
        base_run = _inside(self.root, metadata["base_run"])
        reserved, _ = self._read(_inside(base_run, "dataset/reserved_test_ids.json"))
        extra_reserved, _ = self._read(_inside(ufld, "dataset/reserved_test_ids.json"))
        if extra_reserved != reserved:
            raise ValueError("Experiment reserved test identities changed")
        protected = {record["image_id"] for record in records} | set(reserved)
        for record in manifest[len(records):]:
            image_id = record.get("image_id")
            if (not isinstance(image_id, str) or not _ID.fullmatch(image_id) or image_id in protected
                    or record.get("label_origin") != "teacher_pseudo"
                    or record.get("decision") != "experimental_pseudo" or record.get("pool") != "commercial"):
                raise ValueError("Experiment additions have invalid identities or label provenance")
            protected.add(image_id)
        source_counts = Counter(str(r.get("source") or "unknown") for r in manifest[len(records):])
        status = self._optional(ufld, "status.json", warnings)
        return report, status, [{"source": source, "title": _FRIENDLY.get(source, source), "train_images": count}
                                for source, count in sorted(source_counts.items())]

    def _previews(self, warnings):
        manifest = self._optional(self.preview_root, "provenance.json", warnings)
        previews = []
        items = manifest.get("items", [])
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            filename = item.get("filename")
            if (not isinstance(filename, str) or not _ID.fullmatch(filename) or ".." in filename
                    or Path(filename).suffix.lower() not in (".jpg", ".jpeg", ".png")):
                continue
            try:
                if not _inside(self.preview_root, filename).is_file():
                    continue
            except ValueError:
                continue
            previews.append({"image_url": _PREVIEW + "/" + quote(filename),
                             "source_url": item.get("source_url"), "dimensions": item.get("dimensions"),
                             "domain": "CARLA simulation", "subset": "TuLane source",
                             "selection": "Publisher-selected website example; not a random training sample"})
        return previews

    def _simulation_import(self, warnings):
        """Report acquisition progress without treating imported labels as cleared.

        The importer/audit process writes only its isolated experiment directory.
        A progress counter, including `eligible_train_images`, is not a pinned
        training manifest, so it never contributes to a catalog training total.
        """
        root = _inside(self.root, "experiments/simulated_tulane")
        path = _inside(root, "import.json")
        if not path.is_file():
            return None
        metadata = self._optional(root, "import.json", warnings)
        states = {"starting", "downloading", "converting", "awaiting_audit", "ready", "error"}
        if (metadata.get("schema") != "tulane_simulation_import:v1"
                or metadata.get("source") != "carlane_tulane"
                or metadata.get("domain") != "synthetic"
                or metadata.get("license") != "Apache-2.0"
                or metadata.get("state") not in states):
            warnings.append("Simulation import metadata is unavailable or failed source checks")
            return {"state": "error", "source": "carlane_tulane", "counts": {},
                    "training_manifest_verified": False, "verified_eligible_train_images": 0}
        raw_counts = metadata.get("counts")
        if not isinstance(raw_counts, dict):
            raw_counts = metadata
        counts = {name: _count(raw_counts.get(name)) for name in (
            "downloaded", "converted", "train_images", "val_images", "audit_samples",
            "audits_remaining", "eligible_train_images")}
        return {"state": metadata["state"], "source": "carlane_tulane", "domain": "synthetic",
                "license": "Apache-2.0", "counts": counts, **counts,
                "total_expected": _count(metadata.get("total_expected")),
                "reported_eligible_train_images": counts.get("eligible_train_images"),
                "training_manifest_verified": False, "verified_eligible_train_images": 0,
                "eligibility_context": "Import and audit progress alone do not establish a frozen training selection.",
                "original_reviews_unchanged": True}

    def _overnight_queue(self, base, warnings):
        """Expose isolated experiment progress; never promote a queued dataset."""
        invalid = {"state": "error", "jobs": [], "provisional": True,
                   "unaudited_simulation": True, "auto_promote": False,
                   "approved_collections_unchanged": True,
                   "message": "Overnight queue metadata is unavailable or failed isolation checks"}
        try:
            root = _inside(self.root, "experiments/overnight_collections")
            if not _inside(root, "active.json").is_file():
                return None
            metadata = self._optional(root, "active.json", warnings)
            states = {"waiting_import", "waiting_proposals", "waiting_space", "building", "running", "complete", "error"}
            if (metadata.get("schema") != "overnight_collection_queue:v1"
                    or metadata.get("state") not in states
                    or metadata.get("provisional") is not True
                    or metadata.get("unaudited_simulation") is not True
                    or metadata.get("auto_promote") is not False
                    or metadata.get("approved_collections_unchanged") is not True):
                raise ValueError("Queue contract does not preserve approved collections")
            base_run = _inside(self.baselines, _inside(self.root, metadata.get("base_run")))
            if not base.get("available") or base_run.name != base["run_id"]:
                raise ValueError("Queue references another cleared base")
            if metadata.get("base_manifest_sha256") != base["manifest_sha256"]:
                raise ValueError("Queue base fingerprint changed")
            expected = {"collection_2": "converted_plus_proposals",
                        "collection_3": "converted_plus_simulated",
                        "collection_4": "converted_plus_proposals_plus_simulated"}
            if not isinstance(metadata.get("jobs"), list) or len(metadata["jobs"]) > 3:
                raise ValueError("Invalid overnight job list")
            jobs, seen = [], set()
            for item in metadata["jobs"]:
                if (not isinstance(item, dict) or item.get("id") not in expected
                        or item.get("collection_id") != expected[item["id"]]
                        or item["id"] in seen or item.get("state") not in states):
                    raise ValueError("Invalid overnight job")
                seen.add(item["id"])
                status, dataset = {}, {}
                output_id = None
                if item.get("output_run"):
                    output = _inside(self.root, item["output_run"])
                    if not output.is_relative_to(root) and not output.is_relative_to(self.experiments):
                        raise ValueError("Overnight output leaves isolated experiment directories")
                    output_id = output.name
                    status = self._optional(output, "status.json", warnings)
                    dataset = self._optional(output, "dataset/experiment.json", warnings)
                embedded = item.get("metrics")
                embedded_status = item.get("training_status")
                if not status and isinstance(embedded_status, dict):
                    status = embedded_status
                metrics = _metrics(status) or _metrics(embedded if isinstance(embedded, dict) else {})
                train_images = _count(status.get("train_images"))
                if train_images is None:
                    train_images = _count(dataset.get("train_images"))
                if train_images is None:
                    train_images = _count(item.get("train_images"))
                extra_images = _count(dataset.get("extra_train_images"))
                if extra_images is None:
                    extra_images = _count(item.get("extra_images"))
                jobs.append({"id": item["id"], "collection_id": item["collection_id"],
                             "state": item["state"], "output_run_id": output_id,
                             "progress": _progress(status) or _progress(item.get("progress")),
                             "metrics": metrics, "train_images": train_images,
                             "extra_images": extra_images, "provisional": True,
                             "link": "/datasets?collection=" + item["collection_id"]})
            if [job["id"] for job in jobs] != sorted(seen):
                raise ValueError("Overnight collection order changed")
            result = {"schema": "overnight_collection_queue:v1", "state": metadata["state"],
                      "jobs": jobs, "pid": _count(metadata.get("pid")),
                      "base_ref": {"run_id": base["run_id"], "manifest_sha256": base["manifest_sha256"]},
                      "provisional": True, "unaudited_simulation": True, "auto_promote": False,
                      "approved_collections_unchanged": True,
                      "message": "Private provisional experiments only; their additions stay outside approved collections until audits."}
            return result
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            warnings.append(invalid["message"])
            return invalid

    def catalog(self):
        warnings = []
        run, metadata = self._active(warnings)
        try:
            base, _, _, records = self._base(metadata, warnings)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            base = {"available": False, "run_id": None, "manifest_sha256": None, "train_images": None,
                    "val_images": None, "reserved_test_images": None, "sources": [], "metrics": {},
                    "test_images_used_for_training": 0}
            records = []
            warnings.append("The cleared frozen base is unavailable or failed integrity checks")
        ref = {"run_id": base["run_id"], "manifest_sha256": base["manifest_sha256"]}
        train = base["train_images"]
        common = {"base_ref": ref, "base_train_images": train, "val_images": base["val_images"],
                  "reserved_test_images": base["reserved_test_images"], "sources": base["sources"],
                  "test_images_used_for_training": 0}
        try:
            converted_base, _, _, _ = self._converted_base(metadata, warnings)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            converted_base = {"available": False, "run_id": None, "manifest_sha256": None,
                              "train_images": None, "val_images": None, "reserved_test_images": None,
                              "sources": [], "metrics": {}, "test_images_used_for_training": 0}
            warnings.append("The canonical converted dataset is unavailable or failed integrity checks")
        converted_common = {"base_ref": {"run_id": converted_base["run_id"],
                                         "manifest_sha256": converted_base["manifest_sha256"]},
                            "base_train_images": converted_base["train_images"],
                            "val_images": converted_base["val_images"],
                            "reserved_test_images": converted_base["reserved_test_images"],
                            "sources": converted_base["sources"], "test_images_used_for_training": 0}
        converted = {**converted_common, "id": "converted_only", "title": "Converted data only",
                     "description": "The complete cleared converted base, with its fixed validation and reserved test sets.",
                     "state": "ready" if converted_base["available"] else "unavailable",
                     "state_label": "Ready" if converted_base["available"] else "Base unavailable",
                     "extra_train_images": 0, "total_train_images": converted_base["train_images"],
                     "training_ready": converted_base["available"], "metrics": converted_base["metrics"],
                     "contents": ["All cleared converted training images", "Unchanged validation and reserved test identities"],
                     "steps": ["Frozen cleared snapshot available" if converted_base["available"] else "Restore a validated frozen base"],
                     "links": [{"label": "Preview converted images", "href": "/datasets#base-preview-title"}]}
        active = None
        report = None
        extra_status = {}
        extra_sources = []
        candidates = None
        phase = "not_started"
        if run and metadata is not None:
            phase = metadata.get("phase") if isinstance(metadata.get("phase"), str) else "unknown"
            splits = metadata.get("flagged_by_split")
            candidates = _count(splits.get("train")) if isinstance(splits, dict) else None
            progress = _progress(metadata.get("teacher_progress"))
            if not progress:
                progress = _progress(self._optional(run, "teacher/status.json", warnings)) or _progress(
                    self._optional(run, "teacher/prepare_status.json", warnings))
            active = {"run_id": run.name, "phase": phase, "teacher_progress": progress,
                      "candidate_train_images": candidates, "candidate_images": _count(metadata.get("flagged_images")),
                      "copies_done": _count(metadata.get("copies_done")),
                      "proposals_done": _count(metadata.get("proposals_done")),
                      "quality_pass_proposals_all_splits": _count(metadata.get("quality_pass")),
                      "training_progress": _progress(self._optional(run, "ufld_training.json", warnings)),
                      "auto_promote": False}
            if base["available"]:
                try:
                    report, extra_status, extra_sources = self._prepared_extras(run, metadata, base, records, warnings)
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    warnings.append("Experiment additions are not verified as a prepared training snapshot")
        extra = report["extra_train_images"] if report else None
        proposal_state = "awaiting_audit" if report else ("error" if phase == "error" else "preparing")
        proposal_label = ("Provisional training complete; audits pending" if report and phase == "complete_awaiting_comparison_review"
                          else "Provisional training in progress; audits pending" if report and phase == "training_ufld"
                          else "Provisional snapshot ready; audits pending" if report else "Teacher training" if phase == "training_teacher"
                          else "Preparing teacher" if phase in ("preparing_teacher", "prepared")
                          else "Not started" if phase == "not_started" else "Experiment error" if phase == "error"
                          else "Preparing proposals")
        proposals = {**common, "id": "converted_plus_proposals", "title": "Converted + model proposals",
                     "description": "The same full cleared base plus separate train-only model labels that pass experiment quality checks.",
                     "state": proposal_state, "state_label": proposal_label, "phase": phase,
                     "extra_train_images": 0, "total_train_images": train,
                     "candidate_train_images": candidates, "training_ready": False,
                     "provisional_available_extra_train_images": extra,
                     "model_proposal_candidate_images": extra,
                     "provisional_extra_sources": extra_sources, "extra_sources": [],
                     "metrics": {}, "provisional_metrics": _metrics(extra_status),
                     "teacher_progress": active["teacher_progress"] if active else None,
                     "contents": ["All cleared converted training images", "Model proposals remain separate provisional experiment labels until their audits pass"],
                     "steps": ["Prepare and fit the proposal teacher", "Generate fresh proposals on copied images",
                               "Keep only quality-passing training additions", "Compare UFLDv2 on the unchanged validation set"],
                     "links": [{"label": "View proposal experiment", "href": "/experiment"}],
                     "review_effect": "Optional exclude/skip decisions affect the next snapshot; a frozen active training run is unchanged."}
        previews = self._previews(warnings)
        simulation_import = self._simulation_import(warnings)
        sim_state = simulation_import["state"] if simulation_import else "preview_only"
        sim_labels = {"preview_only": "Preview only; simulation data not imported",
                      "starting": "Starting simulated source import",
                      "downloading": "Downloading simulated source photos",
                      "converting": "Converting simulation annotations",
                      "awaiting_audit": "Simulation import awaiting audits",
                      "ready": "Import complete; training selection not verified",
                      "error": "Simulation import needs attention"}
        sim_counts = simulation_import.get("counts", {}) if simulation_import else {}
        imported_train = sim_counts.get("train_images") or 0
        simulated = {**common, "id": "converted_plus_simulated", "title": "Converted + simulated",
                     "description": "The same full cleared base plus a separately identified CARLA TuLane source subset after its conversion audits and training selection.",
                     "state": sim_state, "state_label": sim_labels[sim_state],
                     "extra_train_images": 0, "total_train_images": train, "training_ready": False,
                     "preview_images": len(previews), "previews": previews,
                     "simulation_import": simulation_import, "imported_simulated_train_images": imported_train,
                     "simulated_extra_train_images": 0,
                     "published_simulated_train_images": 24000,
                     "published_count_context": "Published TuLane CARLA source training subset; this is not a count of cleared imported training additions.",
                     "contents": ["All cleared converted training images", "Simulated source import and conversion audits are tracked separately; no verified simulation training selection yet",
                                  "Only the CARLA simulated source is planned; real TuSimple target images are excluded"],
                     "steps": ["Inspect published simulation examples", "Import source images and their lane annotations",
                               "Audit the conversion and domain mix", "Build a separate training snapshot with the unchanged base holdouts"],
                     "links": [{"label": "Review simulation audits", "href": "/simulation"},
                               {"label": "View published simulation samples", "href": _PREVIEW + "/index.html"},
                               {"label": "Official CARLANE dataset", "href": "https://carlanebenchmark.github.io/"}]}
        combined = {**common, "id": "converted_plus_proposals_plus_simulated",
                    "title": "Converted + model proposals + simulated",
                    "description": "The full cleared converted base, separate quality-passing model proposals, and an audited simulation addition in one future training snapshot.",
                    "state": "error" if proposal_state == "error" or sim_state == "error" else "preparing",
                    "state_label": "Awaiting verified simulation selection" if report else "Proposals and simulation preparing",
                    "phase": phase, "extra_train_images": 0,
                    "model_extra_train_images": 0, "model_proposal_train_images": 0,
                    "provisional_available_extra_train_images": extra,
                    "model_proposal_candidate_images": extra,
                    "simulated_extra_train_images": 0,
                    "total_train_images": train,
                    "candidate_train_images": candidates, "training_ready": False,
                    "model_proposals_training_ready": proposals["training_ready"],
                    "simulation_training_ready": False, "extra_sources": [], "provisional_extra_sources": extra_sources,
                    "teacher_progress": active["teacher_progress"] if active else None,
                    "simulation_import": simulation_import, "imported_simulated_train_images": imported_train,
                    "preview_images": len(previews), "previews": previews,
                    "contents": ["All cleared converted training images", "Separate train-only model proposals that pass experiment checks",
                                 "CARLA simulated source only, after conversion audits and verified training selection",
                                 "Real TuSimple target images are excluded; original validation and test identities stay unchanged"],
                    "steps": ["Finish the proposal teacher and select train-only model additions",
                              "Import the simulated source and audit its lane conversion",
                              "Freeze a combined selection against the unchanged real-image holdouts",
                              "Compare UFLDv2 validation results before considering promotion"],
                    "links": [{"label": "View proposal experiment", "href": "/experiment"},
                              {"label": "Review simulation audits", "href": "/simulation"},
                              {"label": "Inspect simulation samples", "href": _PREVIEW + "/index.html"}]}
        overnight = self._overnight_queue(base, warnings)
        variants = [converted, proposals, simulated, combined]
        for job in overnight.get("jobs", []) if overnight else []:
            variant = next(v for v in variants if v["id"] == job["collection_id"])
            variant["provisional_experiment"] = {"queue_state": overnight["state"],
                "job_state": job["state"], "metrics": job["metrics"], "progress": job["progress"],
                "train_images": job["train_images"], "extra_images": job["extra_images"],
                "link": job["link"], "approved_collections_unchanged": True}
        return {"schema": "studio_datasets:v1", "base": converted_base, "active_experiment": active,
                "simulation_import": simulation_import,
                "overnight_queue": overnight,
                "variants": variants, "warnings": list(dict.fromkeys(warnings)),
                "read_only": True}

    def _preview_state(self):
        warnings = []
        _, metadata = self._active(warnings)
        try:
            base, run, snapshot, records = self._converted_base(metadata, warnings)
            return base, run, snapshot, records
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise HTTPException(409, "The frozen cleared dataset is unavailable or failed integrity checks") from None

    def samples(self, offset=0, limit=3):
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 12:
            raise HTTPException(422, "Invalid frozen preview pagination")
        base, _, snapshot, records = self._preview_state()
        # Round-robin sources preserves stable pagination and shows both Audi/comma.
        groups = defaultdict(deque)
        for record in records:
            if record["split"] == "train":
                groups[str(record.get("source") or "unknown")].append(record)
        ordered = []
        keys = sorted(groups)
        while any(groups.values()):
            for key in keys:
                if groups[key]:
                    ordered.append(groups[key].popleft())
        try:
            cache, sha = self._read(_inside(snapshot, "culane_anno_cache.json"))
            hashes, _ = self._read(_inside(snapshot, "hashes.json"))
            if not isinstance(cache, dict) or hashes.get("culane_anno_cache.json") != sha:
                raise ValueError("Invalid frozen geometry fingerprint")
            items = []
            for record in ordered[offset:offset + limit]:
                entry = cache.get(record["image"])
                if not isinstance(entry, list) or len(entry) != 4:
                    raise ValueError("Invalid frozen lane cache")
                lanes = []
                for slot, points in enumerate(entry, start=1):
                    if not isinstance(points, list):
                        raise ValueError("Invalid lane points")
                    observed = []
                    for point in points:
                        if (not isinstance(point, list) or len(point) != 2
                                or any(type(v) not in (float, int) or not math.isfinite(v) for v in point)):
                            raise ValueError("Invalid frozen lane point")
                        x, y = point
                        if x >= 0:
                            if not 0 <= x < 1640 or not 0 <= y < 590:
                                raise ValueError("Frozen lane point is out of bounds")
                            observed.append([x, y])
                    if len(observed) >= 2:
                        lanes.append({"slot": slot, "points": observed})
                items.append({"image_id": record["image_id"], "source": record.get("source"),
                              "split": "train", "lanes": len(lanes),
                              "image_url": _API + "/base/photos/" + quote(record["image_id"]),
                              "geometry": {"width": 1640, "height": 590, "lanes": lanes}})
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise HTTPException(409, "Frozen preview geometry is unavailable or failed integrity checks") from None
        return {"items": items, "total": len(ordered), "offset": offset, "limit": limit,
                "next_offset": offset + limit if offset + limit < len(ordered) else None,
                "base_ref": {"run_id": base["run_id"], "manifest_sha256": base["manifest_sha256"]}}

    def photo(self, image_id):
        if not _ID.fullmatch(image_id) or ".." in image_id:
            raise HTTPException(400, "Invalid frozen image identifier")
        _, _, snapshot, records = self._preview_state()
        record = next((r for r in records if r["image_id"] == image_id and r["split"] == "train"), None)
        if not record:
            raise HTTPException(404, "Frozen training image not found")
        try:
            path = _asset(snapshot, record["image"])
            if not path.is_file():
                raise FileNotFoundError
            return path
        except (ValueError, OSError):
            raise HTTPException(404, "Frozen training photo is unavailable") from None


def create_studio_datasets_router(data_root: Path, require_session: Callable) -> APIRouter:
    """Mount with the existing review server's authenticated session dependency."""
    router = APIRouter(prefix=_API, tags=["read-only datasets"])
    store = StudioDatasets(data_root)

    def authenticated(session=Depends(require_session)):
        if not isinstance(session, dict) or not session.get("reviewer"):
            raise HTTPException(401, "Pair this device before opening datasets")
        return session

    @router.get("")
    def catalog(session=Depends(authenticated)):
        return store.catalog()

    @router.get("/base/samples")
    def samples(offset: int = Query(0, ge=0), limit: int = Query(3, ge=1, le=12), session=Depends(authenticated)):
        return store.samples(offset, limit)

    @router.get("/base/photos/{image_id}")
    def photo(image_id: str, session=Depends(authenticated)):
        return FileResponse(store.photo(image_id), headers={"Cache-Control": "private, max-age=3600"})

    return router
