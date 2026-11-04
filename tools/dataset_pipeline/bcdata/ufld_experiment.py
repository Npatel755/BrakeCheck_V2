"""Isolated UFLDv2 experiments: cleared base plus explicitly provisional labels.

No registry, reviewed export, human annotation or production training report is
written here. All base train/validation examples remain present and unchanged.
Teacher additions are train-only and never become human-approved/source labels.
"""
from __future__ import annotations

from collections import Counter
import copy
import hashlib
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import time
import uuid

import cv2
import numpy as np

from .bootstrap import atomic_json
from .export_ufld import ROWS_CACHE, cache_entry, write_sample
from .geometry import make_crop
from .quality import read_gray_u8, read_rgb
from .teachers.base import file_sha256
from .ufld_baseline import HEIGHT, WIDTH, load_frozen_export

FORMAT = "ufld_teacher_experiment_v1"
PSEUDO_ORIGIN = "teacher_pseudo"
PSEUDO_DECISION = "experimental_pseudo"
_HEX = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,180}$")


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _relative(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError("Unsafe experiment relative path")
    path = PurePosixPath(name)
    if path.is_absolute() or PureWindowsPath(name).is_absolute() or ".." in path.parts:
        raise ValueError("Unsafe experiment relative path")
    return Path(*path.parts)


def _session(record):
    source, session = record.get("source"), record.get("session_id")
    if not isinstance(source, str) or not source or not isinstance(session, str) or not session:
        raise ValueError("Explicit source and recording session are required")
    return source, session


def _identity(record):
    image_id = record.get("image_id")
    if not isinstance(image_id, str) or not _SAFE_ID.fullmatch(image_id):
        raise ValueError("Invalid image identity")
    _session(record)
    if record.get("split") not in {"train", "val", "test"}:
        raise ValueError("Explicit original train/val/test assignment is required")
    return image_id


def _source_hash(record):
    value = record.get("source_sha256") or record.get("sha256")
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ValueError("Source image SHA256 is required")
    return value


def _pixel_hash(rgb):
    return hashlib.sha256(np.ascontiguousarray(rgb).tobytes()).hexdigest()


def _sample_paths(record):
    image = _relative(record["image"])
    return image, image.with_suffix(".lines.txt"), Path("laneseg_label_w16") / image.with_suffix(".png")


def _verify_sample(snapshot, record, entry, *, pixels=False):
    image, lines, segmentation = _sample_paths(record)
    rgb, mask = read_rgb(snapshot / image), read_gray_u8(snapshot / segmentation)
    if rgb is None or rgb.shape != (HEIGHT, WIDTH, 3):
        raise ValueError("Experiment image must be 1640x590: " + record["image_id"])
    if mask is None or mask.shape != (HEIGHT, WIDTH) or mask.dtype != np.uint8 or not set(np.unique(mask)) <= {0, 1, 2, 3, 4}:
        raise ValueError("Invalid experiment segmentation: " + record["image_id"])
    points = np.asarray(entry, dtype=np.float64)
    if points.shape != (4, 35, 2) or not np.isfinite(points).all():
        raise ValueError("Invalid slotted experiment cache")
    if not np.array_equal(points[:, :, 1], np.tile(np.asarray(ROWS_CACHE), (4, 1))):
        raise ValueError("Experiment cache changed row anchors")
    xs = points[:, :, 0]
    present = xs >= 0
    if np.any((xs < 0) & (xs > -10000)) or np.any(xs[present] >= WIDTH) or np.any(present[:, -1]):
        raise ValueError("Experiment cache has invalid coordinates or bottom-edge labels")
    curves = []
    flags = []
    for lane in points:
        curve = lane[lane[:, 0] >= 0][::-1]
        if len(curve) == 1:
            raise ValueError("Experiment lane has fewer than two sampled points")
        flags.append(int(len(curve) >= 2))
        if len(curve):
            curves.append(curve)
    actual = []
    for line in (snapshot / lines).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            curve = np.asarray([float(item) for item in line.split()], np.float64).reshape(-1, 2)
        except ValueError as error:
            raise ValueError("Malformed experiment polyline") from error
        actual.append(curve)
    if len(actual) != len(curves) or any(a.shape != b.shape or not np.allclose(a, b, rtol=0, atol=.0011)
                                          for a, b in zip(actual, curves)):
        raise ValueError("Experiment polylines disagree with slotted cache")
    if int(record.get("lanes", -1)) != sum(flags):
        raise ValueError("Experiment lane-count metadata disagrees with targets")
    return flags, _pixel_hash(rgb) if pixels else None


def _inventory(records):
    result = {}
    for original in records:
        record = dict(original)
        image_id = _identity(record)
        if image_id in result:
            raise ValueError("Duplicate image identity in split inventory")
        result[image_id] = record
    if not result:
        raise ValueError("Authoritative split inventory is required")
    return result


def _teacher_provenance(teacher, protected):
    teacher = copy.deepcopy(teacher)
    for name in ("name", "version"):
        if not isinstance(teacher.get(name), str) or not teacher[name]:
            raise ValueError("Teacher provenance is missing " + name)
    if not isinstance(teacher.get("checkpoint_sha256"), str) or not _HEX.fullmatch(teacher["checkpoint_sha256"]):
        raise ValueError("Teacher checkpoint fingerprint is required")
    training = teacher.get("training_records")
    if not isinstance(training, list):
        raise ValueError("Teacher training_records must include all training ancestry")
    held_ids = {r["image_id"] for r in protected}
    held_sessions = {_session(r) for r in protected}
    held_hashes = {r.get("source_sha256") or r.get("sha256") for r in protected} - {None, ""}
    for record in training:
        if _identity(record) in held_ids or _session(record) in held_sessions:
            raise ValueError("Teacher training ancestry overlaps frozen validation/test")
        if record.get("split") != "train":
            raise ValueError("Teacher fitting ancestry must contain train records only")
        if (record.get("source_sha256") or record.get("sha256")) in held_hashes:
            raise ValueError("Teacher training image fingerprint overlaps validation/test")
    # Fail early on non-JSON/nonfinite provenance rather than losing evidence.
    _digest(teacher)
    return teacher


def _geometry(geometry, width, height):
    if not isinstance(geometry, dict) or not isinstance(geometry.get("lanes"), list):
        raise ValueError("Teacher geometry must contain explicit painted polylines")
    lanes, slots = [], set()
    for lane in geometry["lanes"]:
        slot = lane.get("slot")
        if isinstance(slot, bool) or not isinstance(slot, int) or slot not in (1, 2, 3, 4) or slot in slots:
            raise ValueError("Teacher lane slots must be unique integers 1..4")
        if lane.get("physical", True) is not True or lane.get("kind") in {"road_edge", "curb", "pavement_edge"}:
            raise ValueError("Teacher experiment accepts physical paint only")
        points = np.asarray([point[:2] for point in lane.get("points", [])], np.float64)
        if points.ndim != 2 or points.shape[1:] != (2,) or len(points) < 2 or not np.isfinite(points).all():
            raise ValueError("Invalid teacher polyline coordinates")
        if np.any(points[:, 0] < 0) or np.any(points[:, 0] >= width) or np.any(points[:, 1] < 0) or np.any(points[:, 1] >= height):
            raise ValueError("Teacher polyline lies outside source image")
        order = np.argsort(points[:, 1])
        points = points[order]
        if np.any(np.diff(points[:, 1]) <= 0):
            raise ValueError("Teacher polyline has ambiguous row crossings")
        slots.add(slot)
        # Export exactly the supplied observed span with piecewise linear
        # interpolation; a teacher polynomial cannot silently extend it.
        lanes.append({"slot": slot, "physical": True, "points": points.tolist(),
                      "y_top": float(points[0, 1]), "y_bottom": float(points[-1, 1]),
                      "fit": {"basis": "interp", "ys": points[:, 1].tolist(), "xs": points[:, 0].tolist()}})
    if not lanes:
        raise ValueError("Teacher absence is not a verified no-lanes label")
    return {"lanes": lanes}


def _input_image(descriptor, record, registered):
    source = Path(descriptor["image_path"]).resolve()
    original_hash = _source_hash(record)
    image_hash = file_sha256(source)
    declared_hash = descriptor.get("image_sha256", original_hash)
    if declared_hash != image_hash:
        raise ValueError("extra_source_image_changed")
    provenance = descriptor.get("copy_provenance")
    if image_hash != original_hash:
        if not isinstance(provenance, dict) or provenance.get("transform") != "full_resolution_jpeg_q97":
            raise ValueError("derived_input_requires_explicit_full_resolution_copy_provenance")
        if provenance.get("original_sha256") != original_hash or provenance.get("copy_sha256") != image_hash:
            raise ValueError("derived_input_fingerprints_disagree_with_copy_provenance")
        with source.open("rb") as stream:
            signature = stream.read(2)
        if source.suffix.lower() not in {".jpg", ".jpeg"} or signature != b"\xff\xd8":
            raise ValueError("derived_input_must_be_the_declared_jpeg_copy")
    elif provenance is not None:
        raise ValueError("copy_provenance_requires_a_distinct_derived_image")
    rgb = read_rgb(source)
    if rgb is None:
        raise ValueError("extra_source_image_is_unreadable")
    height, width = rgb.shape[:2]
    if image_hash != original_hash:
        for key, actual in (("width", width), ("height", height)):
            if not isinstance(registered.get(key), int) or registered[key] != actual or provenance.get(key) != actual:
                raise ValueError("derived_input_changed_original_image_dimensions")
    elif registered.get("width") and registered.get("height"):
        if (registered["width"], registered["height"]) != (width, height):
            raise ValueError("source_image_dimensions_disagree_with_inventory")
    return rgb, image_hash, copy.deepcopy(provenance)


def _base_state(base_run, split_inventory):
    snapshot, records, ready = load_frozen_export(base_run)
    inventory = _inventory(split_inventory)
    hashes = _read(snapshot / "hashes.json")
    ids, paths = set(), set()
    sessions = {"train": set(), "val": set(), "test": set()}
    for record in records:
        image_id = _identity(record)
        if image_id in ids or record["image"] in paths:
            raise ValueError("Duplicate cleared base identity or path")
        ids.add(image_id); paths.add(record["image"])
        sessions[record["split"]].add(_session(record))
        registered = inventory.get(image_id)
        if registered is None or registered["split"] != record["split"] or _session(registered) != _session(record):
            raise ValueError("Cleared base disagrees with original split inventory")
        for name in _sample_paths(record):
            if name.as_posix() not in hashes:
                raise ValueError("Cleared base sample lacks a file fingerprint")
    if not sessions["train"] or not sessions["val"]:
        raise ValueError("Cleared base must include both train and validation")
    reserved = _read(snapshot / "reserved_test_ids.json")
    if not isinstance(reserved, list) or len(set(reserved)) != len(reserved):
        raise ValueError("Invalid reserved-test identity inventory")
    for image_id in reserved:
        if image_id not in inventory or inventory[image_id]["split"] != "test":
            raise ValueError("Split inventory must identify every frozen reserved test session")
    protected = [dict(r) for r in records if r["split"] == "val"]
    protected.extend(r for r in inventory.values() if r["split"] in {"val", "test"})
    held_sessions = {_session(r) for r in protected}
    if sessions["train"] & held_sessions:
        raise ValueError("Cleared training sessions overlap validation/test")
    if ids & set(reserved):
        raise ValueError("Reserved-test identity entered the cleared base")
    return snapshot, records, ready, inventory, protected, reserved


def build_experiment(base_run, output_run, extras, *, split_inventory, teacher,
                     max_extra_ratio=1.0):
    """Create a new immutable prepared run without changing any source dataset.

    Each extra is {record, image_path, geometry, crop_config?}. record retains its
    original image_id, source, session_id, split, pool, license[_id], source_sha256.
    Teacher ancestry is explicit training_records; all original val/test sessions
    are excluded. Extras are deterministic, train-only and capped relative to
    the *unchanged* cleared base training count (default at most 1:1).
    """
    output_run, base_run = Path(output_run).resolve(), Path(base_run).resolve()
    if output_run.exists() or output_run == base_run or output_run.is_relative_to(base_run):
        raise ValueError("Experiment output must be a new directory outside the cleared base")
    if isinstance(max_extra_ratio, bool) or not isinstance(max_extra_ratio, (int, float)) or not math.isfinite(max_extra_ratio) or max_extra_ratio < 0:
        raise ValueError("Extra-to-cleared training ratio must be finite and nonnegative")
    snapshot, base_records, ready, inventory, protected, reserved = _base_state(base_run, split_inventory)
    teacher = _teacher_provenance(teacher, protected)
    teacher_hash = _digest(teacher)
    source_hashes = {r.get("source_sha256") for r in base_records} - {None, ""}
    held_hashes = {r.get("source_sha256") or r.get("sha256") for r in protected} - {None, ""}
    held_sessions, held_ids = {_session(r) for r in protected}, {r["image_id"] for r in protected}
    base_hashes, base_cache = _read(snapshot / "hashes.json"), _read(snapshot / "culane_anno_cache.json")
    pixels, image_hashes = set(), set()
    for record in base_records:
        _, pixel_hash = _verify_sample(snapshot, record, base_cache[record["image"]], pixels=True)
        pixels.add(pixel_hash); image_hashes.add(base_hashes[record["image"]])
    descriptors = list(extras)
    descriptors.sort(key=lambda item: str(item.get("record", {}).get("image_id", "")))
    cleared_train = sum(r["split"] == "train" for r in base_records)
    limit = int(cleared_train * max_extra_ratio)
    accepted, rejected = [], []
    seen_ids = {r["image_id"] for r in base_records}
    stage = output_run.with_name(output_run.name + ".preparing-" + uuid.uuid4().hex)
    target = stage / "dataset"
    target.mkdir(parents=True)
    try:
        # Copy physical bytes, never hard-link a mutable reviewed source file.
        for relative, sha in base_hashes.items():
            rel = _relative(relative)
            destination = target / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snapshot / rel, destination)
            if file_sha256(destination) != sha:
                raise ValueError("Cleared base changed during isolated copy: " + relative)
        for name in ("reserved_test_ids.json", "export_marker.json"):
            shutil.copy2(snapshot / name, target / ("base_" + name))
        for name in ("NOTICE.txt", "licenses"):
            source = snapshot / name
            if source.is_dir():
                shutil.copytree(source, target / name, dirs_exist_ok=True)
            elif source.is_file():
                shutil.copy2(source, target / name)
        shutil.copy2(snapshot / "manifest.json", target / "base_manifest.json")
        atomic_json(target / "base_hashes.json", base_hashes)
        shutil.copy2(snapshot / "culane_anno_cache.json", target / "base_anno_cache.json")
        records, cache = copy.deepcopy(base_records), copy.deepcopy(base_cache)
        for descriptor in descriptors:
            record = dict(descriptor.get("record", {}))
            image_id = record.get("image_id", "unknown")
            try:
                _identity(record)
                registered = inventory.get(image_id)
                if registered is None or registered["split"] != record["split"] or _session(registered) != _session(record):
                    raise ValueError("extra_split_inventory_mismatch")
                if record["split"] != "train" or image_id in held_ids or _session(record) in held_sessions:
                    raise ValueError("extra_is_held_out_image_or_session")
                if image_id in seen_ids:
                    raise ValueError("duplicate_image_identity")
                seen_ids.add(image_id)
                if record.get("pool") != "commercial" or registered.get("pool") != "commercial":
                    raise ValueError("extra_is_not_in_eligible_commercial_pool")
                if any(r.get("review_status") in {"approved", "corrected", "rejected"}
                       or r.get("decision") in {"approved", "corrected", "rejected", "source_validated"}
                       or r.get("locked") in (True, 1, "1") for r in (record, registered)):
                    raise ValueError("human_or_cleared_truth_is_protected")
                license_id = record.get("license") or record.get("license_id")
                if not isinstance(license_id, str) or not license_id:
                    raise ValueError("extra_dataset_license_provenance_is_required")
                digest = _source_hash(record)
                registered_hash = registered.get("source_sha256") or registered.get("sha256")
                if registered_hash != digest:
                    raise ValueError("extra_source_fingerprint_disagrees_with_inventory")
                if digest in held_hashes or digest in source_hashes or digest in image_hashes:
                    raise ValueError("duplicate_or_held_out_source_fingerprint")
                rgb, input_hash, copy_provenance = _input_image(descriptor, record, registered)
                height, width = rgb.shape[:2]
                geometry = _geometry(descriptor.get("geometry"), width, height)
                crop = make_crop(descriptor.get("crop_config") or {}, width, height, WIDTH, HEIGHT)
                if not 0 <= crop.x0 < crop.x1 <= width or not 0 <= crop.y0 < crop.y1 <= height:
                    raise ValueError("extra_crop_is_invalid")
                canvas = cv2.resize(rgb[int(crop.y0):int(crop.y1), int(crop.x0):int(crop.x1)],
                                    (WIDTH, HEIGHT), interpolation=cv2.INTER_AREA)
                if _pixel_hash(canvas) in pixels:
                    raise ValueError("duplicate_canvas_pixels")
                if len(accepted) >= limit:
                    raise ValueError("extra_training_ratio_cap")
                image = f"images/train/experiment/{image_id}.jpg"
                segmentation = "laneseg_label_w16/" + image.replace(".jpg", ".png")
                exists, lane_rows, _ = write_sample(rgb, geometry, crop, 16, 95, target / image, target / segmentation)
                if not any(exists):
                    raise ValueError("teacher_has_no_trainable_sampled_paint")
                (target / image).with_suffix(".lines.txt").write_text("".join(
                    " ".join(f"{x:.3f} {y}" for x, y in points) + " \n" for points in lane_rows if points), encoding="utf-8")
                entry = cache_entry(lane_rows)
                addition = {**record, "image": image, "license": license_id, "source_sha256": digest,
                            "split": "train", "label_origin": PSEUDO_ORIGIN, "decision": PSEUDO_DECISION,
                            "lanes": sum(exists), "experimental": True, "training_weight": 1.0,
                            "teacher_provenance_sha256": teacher_hash,
                            "geometry_sha256": _digest(geometry),
                            "input_image_sha256": input_hash, "copy_provenance": copy_provenance,
                            "original_metadata": copy.deepcopy(record)}
                _, pixel_hash = _verify_sample(target, addition, entry, pixels=True)
                if pixel_hash in pixels:
                    raise ValueError("duplicate_encoded_canvas_pixels")
                pixels.add(pixel_hash); source_hashes.add(digest)
                records.append(addition); cache[image] = entry; accepted.append(addition)
            except (ValueError, KeyError, OSError, TypeError) as error:
                rejected.append({"image_id": str(image_id), "reason": str(error)[:500]})
        if not accepted:
            raise ValueError("No eligible extra TRAIN images; original cleared base was not changed")
        atomic_json(target / "manifest.json", records)
        atomic_json(target / "culane_anno_cache.json", cache)
        atomic_json(target / "reserved_test_ids.json", reserved)
        atomic_json(target / "split_inventory.json", list(inventory.values()))
        atomic_json(target / "teacher_provenance.json", teacher)
        shutil.copy2(Path(__file__), target / "experiment_builder.py")
        atomic_json(target / "rejected_extras.json", rejected)
        atomic_json(target / "export_marker.json", {
            "revision": ready["revision"], "errors": [], "validation": {"ok": True},
            "experimental": True, "format": FORMAT, "base_manifest_sha256": base_hashes["manifest.json"],
            "manifest_sha256": file_sha256(target / "manifest.json"), "auto_promote": False})
        report = {"format": FORMAT, "experimental": True, "auto_promote": False,
                  "created_at": time.time(), "base_run": str(base_run), "output_run": str(output_run),
                  "base_manifest_sha256": base_hashes["manifest.json"],
                  "base_train_images": cleared_train, "base_val_images": sum(r["split"] == "val" for r in base_records),
                  "extra_train_images": len(accepted), "train_images": cleared_train + len(accepted),
                  "val_images": sum(r["split"] == "val" for r in base_records), "test_images_used": 0,
                  "max_extra_ratio": max_extra_ratio, "training_weight_policy": "equal_weight_capped_train_only",
                  "experiment_builder_sha256": file_sha256(target / "experiment_builder.py"),
                  "teacher_provenance_sha256": teacher_hash, "rejected_extras": len(rejected),
                  "rejection_counts": dict(Counter(item["reason"] for item in rejected))}
        atomic_json(target / "experiment.json", report)
        lists = target / "list"
        lists.mkdir(exist_ok=True)
        for split, name in (("train", "train_gt"), ("val", "val_gt")):
            lines = []
            for record in records:
                if record["split"] != split:
                    continue
                image, _, segmentation = _sample_paths(record)
                flags = [int(any(p[0] >= 0 for p in curve)) for curve in cache[record["image"]]]
                lines.append(f"/{image.as_posix()} /{segmentation.as_posix()} " + " ".join(map(str, flags)))
            (lists / f"{name}.txt").write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        for name in ("test", "test_gt"):
            (lists / f"{name}.txt").write_text("", encoding="utf-8")
        hashes = {path.relative_to(target).as_posix(): file_sha256(path)
                  for path in target.rglob("*") if path.is_file() and path.name != "hashes.json"}
        atomic_json(target / "hashes.json", hashes)
        load_experiment_export(stage)
        stage.rename(output_run)
        return report
    except Exception as error:
        atomic_json(stage / "FAILED.json", {"error": str(error), "originals_unchanged": True})
        raise


def load_experiment_export(output_run):
    """Validate isolated bytes, unchanged cleared data and train-only provenance.

    Only an explicit experimental trainer flag should call this loader. The
    production load_frozen_export deliberately continues to reject pseudolabels.
    """
    snapshot = Path(output_run).resolve() / "dataset"
    hashes = _read(snapshot / "hashes.json")
    required = {"manifest.json", "culane_anno_cache.json", "experiment.json", "base_manifest.json",
                "base_hashes.json", "base_anno_cache.json", "teacher_provenance.json",
                "split_inventory.json", "reserved_test_ids.json", "base_reserved_test_ids.json", "export_marker.json",
                "experiment_builder.py"}
    if not required <= set(hashes):
        raise ValueError("Experiment is missing immutable provenance fingerprints")
    for relative, sha in hashes.items():
        path = snapshot / _relative(relative)
        if not path.resolve().is_relative_to(snapshot) or file_sha256(path) != sha:
            raise ValueError("Frozen experiment file changed: " + relative)
    report, ready = _read(snapshot / "experiment.json"), _read(snapshot / "export_marker.json")
    if report.get("format") != FORMAT or ready.get("format") != FORMAT or not ready.get("validation", {}).get("ok") or ready.get("errors"):
        raise ValueError("Invalid experimental dataset marker")
    if report.get("experiment_builder_sha256") != hashes["experiment_builder.py"]:
        raise ValueError("Experiment builder code provenance changed")
    records, base = _read(snapshot / "manifest.json"), _read(snapshot / "base_manifest.json")
    cache, base_cache = _read(snapshot / "culane_anno_cache.json"), _read(snapshot / "base_anno_cache.json")
    base_hashes = _read(snapshot / "base_hashes.json")
    if file_sha256(snapshot / "base_manifest.json") != base_hashes["manifest.json"] or file_sha256(snapshot / "base_anno_cache.json") != base_hashes["culane_anno_cache.json"]:
        raise ValueError("Experiment changed the original base fingerprint evidence")
    if report["base_manifest_sha256"] != base_hashes["manifest.json"] or _digest(base) != _digest(records[:len(base)]):
        raise ValueError("Experiment changed or omitted original cleared base records")
    if len(records) <= len(base) or {r["split"] for r in records} != {"train", "val"}:
        raise ValueError("Experiment must retain base train/val and add train records")
    for record in base:
        if cache.get(record["image"]) != base_cache.get(record["image"]):
            raise ValueError("Experiment changed cleared lane targets")
        for relative in _sample_paths(record):
            if hashes.get(relative.as_posix()) != base_hashes.get(relative.as_posix()):
                raise ValueError("Experiment changed cleared image/annotation bytes")
    inventory = _inventory(_read(snapshot / "split_inventory.json"))
    reserved = _read(snapshot / "reserved_test_ids.json")
    if reserved != _read(snapshot / "base_reserved_test_ids.json"):
        raise ValueError("Experiment changed reserved test identity inventory")
    protected = [r for r in inventory.values() if r["split"] in {"val", "test"}]
    protected += [r for r in base if r["split"] == "val"]
    teacher = _teacher_provenance(_read(snapshot / "teacher_provenance.json"), protected)
    teacher_hash = _digest(teacher)
    if teacher_hash != report["teacher_provenance_sha256"]:
        raise ValueError("Experiment teacher provenance changed")
    held_ids, held_sessions = {r["image_id"] for r in protected} | set(reserved), {_session(r) for r in protected}
    held_hashes = {r.get("source_sha256") or r.get("sha256") for r in protected} - {None, ""}
    ids, names, source_hashes = set(), set(), set()
    for index, record in enumerate(records):
        image_id = _identity(record)
        if image_id in ids or record["image"] in names or image_id in reserved:
            raise ValueError("Duplicate or reserved-test experiment image")
        ids.add(image_id); names.add(record["image"])
        for path in _sample_paths(record):
            if path.as_posix() not in hashes:
                raise ValueError("Experiment image/annotation lacks a fingerprint")
        if index >= len(base):
            if record["split"] != "train" or image_id in held_ids or _session(record) in held_sessions:
                raise ValueError("Extra teacher labels entered validation/test")
            registered = inventory.get(image_id)
            if registered is None or registered["split"] != "train" or _session(registered) != _session(record):
                raise ValueError("Experiment extra disagrees with original split inventory")
            if record.get("label_origin") != PSEUDO_ORIGIN or record.get("decision") != PSEUDO_DECISION or record.get("pool") != "commercial" or not record.get("experimental"):
                raise ValueError("Extra pseudolabel provenance is not explicit")
            if record.get("teacher_provenance_sha256") != teacher_hash or record.get("training_weight") != 1.0:
                raise ValueError("Experiment pseudo-label policy changed")
            copy_provenance = record.get("copy_provenance")
            if record.get("input_image_sha256") != _source_hash(record):
                if not isinstance(copy_provenance, dict) or copy_provenance.get("transform") != "full_resolution_jpeg_q97" or copy_provenance.get("original_sha256") != _source_hash(record) or copy_provenance.get("copy_sha256") != record.get("input_image_sha256"):
                    raise ValueError("Experiment derived-copy provenance is incomplete")
                if any(copy_provenance.get(key) != registered.get(key) for key in ("width", "height")):
                    raise ValueError("Experiment derived-copy dimensions changed")
            if _source_hash(record) != _source_hash(registered) or _source_hash(record) in source_hashes:
                raise ValueError("Duplicate extra source fingerprint")
            if _source_hash(record) in held_hashes:
                raise ValueError("Extra source fingerprint overlaps validation/test")
            original = record.get("original_metadata") or {}
            if registered.get("pool") != "commercial" or any(
                    r.get("review_status") in {"approved", "corrected", "rejected"}
                    or r.get("decision") in {"approved", "corrected", "rejected", "source_validated"}
                    or r.get("locked") in (True, 1, "1") for r in (original, registered)):
                raise ValueError("Experimental pseudolabel replaced protected human truth")
            if int(record.get("lanes", 0)) < 1:
                raise ValueError("Teacher absence is not a verified negative")
        source_hashes.add(record.get("source_sha256"))
        _verify_sample(snapshot, record, cache.get(record["image"], []))
    if set(cache) != names:
        raise ValueError("Experiment cache must contain exactly its frozen image records")
    if ready.get("manifest_sha256") != hashes["manifest.json"]:
        raise ValueError("Experiment marker disagrees with manifest fingerprint")
    count = sum(r["split"] == "train" for r in base)
    if len(records) - len(base) > int(count * report["max_extra_ratio"]):
        raise ValueError("Experiment extras exceed the declared ratio cap")
    expected = {"base_train_images": count, "base_val_images": sum(r["split"] == "val" for r in base),
                "extra_train_images": len(records) - len(base), "train_images": sum(r["split"] == "train" for r in records),
                "val_images": sum(r["split"] == "val" for r in records), "test_images_used": 0}
    if any(report.get(key) != value for key, value in expected.items()):
        raise ValueError("Experiment split counts disagree with frozen records")
    return snapshot, records, ready
