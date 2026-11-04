"""Freeze original human semantic masks for candidate-only annotation training.

This never consumes reviewed polylines, fused masks, or model predictions.  A
frozen annotation record is captured alongside the bytes it refers to; loaders
never ask the live registry which annotation is current.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

from .bootstrap import atomic_json
from .licenses import LicenseRegistry
from .teachers.base import file_sha256

SCHEMA = "original_source_mask_cohort:v1"
CLASSES = ["unknown", "road", "lane", "undrivable", "movable", "my_car"]


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _asset(path, expected=None):
    path = Path(path).resolve()
    sha = file_sha256(path)
    if expected and sha != expected:
        raise RuntimeError("Frozen source file SHA mismatch: " + str(path))
    return {"path": str(path), "sha256": sha, "bytes": path.stat().st_size}


def _verify(asset):
    if file_sha256(Path(asset["path"])) != asset["sha256"]:
        raise RuntimeError("Frozen source file SHA mismatch: " + asset["path"])


def _validate_manifest(manifest):
    if (manifest.get("schema") != SCHEMA or manifest.get("classes") != CLASSES or
            manifest.get("label_source") != "original_human_source_masks" or
            manifest.get("test_images_used") != 0 or set(manifest.get("cohorts", {})) != {"train", "val"}):
        raise ValueError("Invalid original-source training cohort")
    unsigned = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    if manifest.get("manifest_sha256") != _digest(unsigned):
        raise ValueError("Training cohort manifest SHA mismatch")
    sessions, identities, paths, hashes = {}, set(), set(), set()
    for split, rows in manifest["cohorts"].items():
        if not rows:
            raise ValueError("Separate nonempty train and validation cohorts required")
        for row in rows:
            ann = row["source_annotation"]
            params = json.loads(ann["params"] or "{}")
            if (row["split"] != split or row["status"] != "curated" or row["pool"] != "commercial" or
                    row["review_status"] == "rejected" or params.get("human") is not True or
                    ann["kind"] != "source_label" or ann["image_id"] != row["image_id"] or
                    ann["producer"] != row["source"] or _digest(ann) != row["source_annotation_sha256"]):
                raise ValueError("Invalid frozen original-source record")
            key = (row["source"], row["session_id"])
            if row["session_id"] and key in sessions and sessions[key] != split:
                raise ValueError("Recording-session leakage in original-source cohort")
            sessions[key] = split
            if row["image_id"] in identities or row["path"] in paths or row["sha256"] in hashes:
                raise ValueError("Duplicate image in original-source cohort")
            identities.add(row["image_id"])
            paths.add(row["path"])
            hashes.add(row["sha256"])
            if row["assets"]["image"]["path"] != row["path"] or row["assets"]["image"]["sha256"] != row["sha256"]:
                raise ValueError("Image fingerprint does not match frozen record")
            for license_id in row["license_ids"]:
                item = manifest["licenses"][license_id]
                if item["pool"] != "commercial" or item["status"] not in ("cleared", "cleared_conditions"):
                    raise ValueError("Original-source licence is not commercially cleared")


def freeze_source_cohort(settings, registry, output_dir, sources=("comma10k", "a2d2")):
    """Write a new immutable manifest and licence snapshots; no registry writes.

    ``cohorts[split]`` is in exactly the same order as the pairs returned by
    :func:`load_source_cohort`. The full original annotation row is preserved.
    """
    output_dir = Path(output_dir).resolve()
    target = output_dir / "training_cohort.json"
    if target.exists():
        raise FileExistsError("Refusing to replace a frozen training cohort: " + str(target))
    licensing = LicenseRegistry(settings.licenses_path)
    cohorts, used_licenses, sessions = {"train": [], "val": []}, set(), {}
    rows = registry.images("status='curated' AND pool='commercial' AND has_source_label=1 AND review_status!='rejected'")
    for raw in rows:
        row = dict(raw)
        if row["source"] not in sources or row["split"] not in ("train", "val", "test"):
            continue
        # Test drives are checked for leakage, but their masks are never loaded.
        session_key = (row["source"], row["session_id"])
        if row["session_id"] and session_key in sessions and sessions[session_key] != row["split"]:
            raise RuntimeError("Recording-session leakage across train/val/test")
        sessions[session_key] = row["split"]
        if row["split"] == "test":
            continue
        ann_raw = registry.annotation(row["image_id"], "source_label")
        if ann_raw is None:
            continue
        ann = dict(ann_raw)
        params = json.loads(ann["params"] or "{}")
        if params.get("human") is not True:
            continue
        if ann["producer"] != row["source"] or params.get("format") not in ("comma10k_rgb", "a2d2_rgb", "class_map"):
            raise RuntimeError("Only original dataset source masks may enter this cohort")
        ids = sorted(set([row["license_id"], *json.loads(ann["license_ids"] or "[]")]))
        if any(not licensing.get(key).commercial for key in ids):
            raise RuntimeError("Original source licence is not commercially cleared")
        assets = {"image": _asset(row["path"], row["sha256"]), "source": _asset(ann["path"])}
        for key in ("palette", "provenance", "calibration"):
            if params.get(key + "_path"):
                assets[key] = _asset(params[key + "_path"], params.get(key + "_sha256"))
        if params["format"] == "a2d2_rgb" and "palette" not in assets:
            raise RuntimeError("Audi original semantic labels require their pinned class palette")
        if params["format"] == "a2d2_rgb":
            if "provenance" not in assets:
                raise RuntimeError("Audi original source provenance is required")
            provenance = json.loads(Path(assets["provenance"]["path"]).read_text(encoding="utf-8"))
            originals = provenance.get("files", {})
            if (provenance.get("originals_unchanged") is not True or provenance.get("camera") != "front_center" or
                    originals.get("img", {}).get("sha256") != assets["image"]["sha256"] or
                    originals.get("mask", {}).get("sha256") != assets["source"]["sha256"] or
                    provenance.get("palette_sha256") != assets["palette"]["sha256"] or
                    ("calibration" in assets and provenance.get("calibration_sha256") != assets["calibration"]["sha256"])):
                raise RuntimeError("Audi bytes do not match pinned original image/mask provenance")
        row.update(path=assets["image"]["path"], source_annotation=ann,
                   source_annotation_sha256=_digest(ann), source_sha256=assets["source"]["sha256"],
                   palette_sha256=assets.get("palette", {}).get("sha256"), assets=assets, license_ids=ids,
                   session_namespace=row["source"] + ":" + row["session_id"])
        cohorts[row["split"]].append(row)
        used_licenses.update(ids)
    output_dir.mkdir(parents=True, exist_ok=True)
    licence_dir = output_dir / "licenses"
    licence_dir.mkdir(exist_ok=True)
    licenses = {}
    for key in sorted(used_licenses):
        entry = licensing.get(key)
        original = settings.path("licenses", key + "-LICENSE.txt")
        if not original.is_file():
            raise RuntimeError("Dataset licence snapshot missing: " + str(original))
        files = []
        for source in (original, settings.path("licenses", key + "-NOTICE.txt")):
            if source.is_file():
                dest = licence_dir / source.name
                shutil.copyfile(source, dest)
                files.append({"name": source.name, **_asset(dest)})
        licenses[key] = {"id": key, "pool": entry.pool, "status": entry.status, "entry": entry.data, "files": files}
    module_dir = Path(__file__).parent
    mapping = {str(p.relative_to(module_dir)): _asset(p) for p in
               (module_dir / "teachers/source_labels.py", module_dir / "sources/comma10k.py", module_dir / "sources/a2d2.py")}
    manifest = {"schema": SCHEMA, "classes": CLASSES, "label_source": "original_human_source_masks",
                "candidate_only": True, "auto_promote": False, "test_images_used": 0,
                "cohorts": cohorts, "licenses": licenses, "mapping_code": mapping,
                "license_registry_sha256": file_sha256(settings.licenses_path),
                "distribution_scope": "private_local_development" if "a2d2" in used_licenses else "source_licence_conditions",
                "counts": {split: {source: sum(r["source"] == source for r in items) for source in sources}
                           for split, items in cohorts.items()}}
    manifest["manifest_sha256"] = _digest(manifest)
    _validate_manifest(manifest)
    atomic_json(target, manifest)
    return manifest


def load_source_cohort(manifest_path, verify=True):
    """Load pinned records, never latest DB annotations; return manifest, pairs.

    Verified file stats are cached only while size/mtime/ctime are unchanged.
    Source records/palette/code and image bytes are verified on initial load;
    changed files are rehashed before a later mask can be used.
    """
    from .teachers.source_labels import load_classes
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    _validate_manifest(manifest)
    verified = {}

    def check(asset):
        p = Path(asset["path"])
        stat = p.stat()
        signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        key = (asset["path"], asset["sha256"])
        if verified.get(key) != signature:
            _verify(asset)
            verified[key] = signature

    fixed = list(manifest["mapping_code"].values()) + [f for entry in manifest["licenses"].values() for f in entry["files"]]
    pairs = {"train": [], "val": []}
    if verify:
        for asset in fixed:
            check(asset)
    for split, rows in manifest["cohorts"].items():
        for row in rows:
            assets = list(row["assets"].values())
            if verify:
                for asset in assets:
                    check(asset)
            def loader(a=row["source_annotation"], bound=assets):
                if verify:
                    for asset in [*fixed, *bound]:
                        check(asset)
                cls = load_classes(a)
                if cls.ndim != 2 or cls.min() < 0 or cls.max() >= len(CLASSES):
                    raise RuntimeError("Original source mask has invalid shared classes")
                return cls
            pairs[split].append((row["path"], loader))
    return manifest, pairs
