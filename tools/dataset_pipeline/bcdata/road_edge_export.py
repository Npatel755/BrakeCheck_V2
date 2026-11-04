"""Reviewed road edges in source pixels, separate from four painted UFLD lanes.

This is an annotation interchange artifact, not a stock UFLDv2 training dataset.
Call only while holding the reviewed-export lock, using that export's DB snapshot.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import uuid

from PIL import Image

from .bootstrap import atomic_json
from .export_ufld import write_notice
from .label_policy import LABELING_POLICY, normalize_road_edges, validate_road_edges


SCHEMA_ID = "brakecheck-road-edges-v1"
SIDES = ("left", "right")
STATUSES = ("visible", "absent", "unknown", "not_reviewed")
KINDS = ("curb", "pavement_edge", "shoulder", "barrier", "other")


def _inside(out, relative):
    target = out / relative
    if not target.resolve().is_relative_to(out.resolve()):
        raise ValueError("Road-edge export path points outside its dataset folder")
    return target


def _text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _photo(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    # Originals are immutable. Reuse the same hard link on later snapshots.
    if target.exists():
        try:
            if os.path.samefile(source, target):
                return
        except OSError:
            pass
    tmp = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        try:
            os.link(source, tmp)
        except OSError:
            shutil.copy2(source, tmp)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def _policy():
    return {
        **LABELING_POLICY,
        "coordinate_system": "original_image_pixels_xy",
        "instructions": [
            "Painted lane boundaries remain in UFLD slots 1 through 4; road edges have no lane slot.",
            "Trace a visible boundary of the road surface, including an unpainted curb or pavement edge.",
            "Mark the left and right edge separately. Never invent a path through an unclear or hidden area.",
            "Draw only the supported visible span. No point outside that span is a negative label.",
            "A partially visible edge may be visible over its traced span; use unknown if no reliable span can be drawn.",
        ],
        "statuses": {
            "visible": {"training_mask": True, "is_negative": False, "meaning": "Human-traced visible edge; supervision only along the polyline span."},
            "absent": {"training_mask": True, "is_negative": True, "meaning": "Human explicitly confirmed that this side has no road edge in the image."},
            "unknown": {"training_mask": False, "is_negative": False, "meaning": "Unclear or hidden; exclude this side from loss and metrics."},
            "not_reviewed": {"training_mask": False, "is_negative": False, "meaning": "Not reviewed; exclude the entire image from this export until both sides are reviewed."},
        },
        "edge_kinds": list(KINDS),
        "crop": "Images and points are uncropped originals. suggested_crop is metadata only; a consumer adapter must transform and clip points and preserve unknown masks.",
        "training_ready": False,
        "adapter_required": "A separate road-edge model/head and data adapter are required. This artifact must not be loaded as painted UFLD slots 1 through 4.",
    }


def _schema():
    side = {
        "type": "object", "additionalProperties": False,
        "required": ["status", "kind", "points", "training_mask", "is_negative"],
        "properties": {
            "status": {"enum": ["visible", "absent", "unknown"]},
            "kind": {"enum": [*KINDS, None]},
            "points": {"type": "array", "items": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "number"}}},
            "training_mask": {"type": "boolean"}, "is_negative": {"type": "boolean"},
        },
        "allOf": [
            {"if": {"properties": {"status": {"const": "visible"}}},
             "then": {"properties": {"kind": {"enum": list(KINDS)}, "points": {"minItems": 2},
                                     "training_mask": {"const": True}, "is_negative": {"const": False}}}},
            {"if": {"properties": {"status": {"const": "absent"}}},
             "then": {"properties": {"kind": {"type": "null"}, "points": {"maxItems": 0},
                                     "training_mask": {"const": True}, "is_negative": {"const": True}}}},
            {"if": {"properties": {"status": {"const": "unknown"}}},
             "then": {"properties": {"kind": {"type": "null"}, "points": {"maxItems": 0},
                                     "training_mask": {"const": False}, "is_negative": {"const": False}}}},
        ],
    }
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": SCHEMA_ID,
            "type": "object", "required": ["schema", "policy_version", "image_id", "image", "split", "source_image", "suggested_crop", "human_review", "road_edges"],
            "properties": {"schema": {"const": SCHEMA_ID}, "policy_version": {"const": LABELING_POLICY["version"]},
                           "road_edges": {"type": "object", "required": list(SIDES), "additionalProperties": False,
                                          "properties": {side_name: side for side_name in SIDES}}}}


def validate_export(out: Path):
    """Check published photos, exact source coordinates and supervision semantics."""
    errors, seen, sessions = [], set(), {}
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    records = [json.loads(line) for line in (out / "annotations.jsonl").read_text(encoding="utf-8").splitlines() if line]
    expected = {entry["image_id"]: entry["image"] for entry in manifest["images"]}
    for record in records:
        image_id = record["image_id"]
        if image_id in seen or expected.get(image_id) != record["image"]:
            errors.append(f"Duplicate or unlisted road-edge image: {image_id}")
        seen.add(image_id)
        if record["schema"] != SCHEMA_ID or record["policy_version"] != LABELING_POLICY["version"]:
            errors.append(f"Wrong road-edge schema or policy: {image_id}")
        source = record["source_image"]
        session = source["session_id"]
        if session in sessions and sessions[session] != record["split"]:
            errors.append(f"Road-edge recording session crosses dataset splits: {session}")
        sessions[session] = record["split"]
        width, height = source["width"], source["height"]
        with Image.open(_inside(out, record["image"])) as photo:
            if photo.size != (width, height):
                errors.append(f"Wrong source-image dimensions: {image_id}")
        review = record["human_review"]
        if review["label_origin"] != "human" or review["decision"] not in ("approved", "corrected") or source["pool"] != "commercial":
            errors.append(f"Unreviewed or noncommercial road-edge sample: {image_id}")
        for side in SIDES:
            edge = record["road_edges"][side]
            status = edge["status"]
            if status not in ("visible", "absent", "unknown"):
                errors.append(f"Unreviewed road edge: {image_id}/{side}")
            if edge["training_mask"] != (status in ("visible", "absent")) or edge["is_negative"] != (status == "absent"):
                errors.append(f"Unsafe road-edge supervision mask: {image_id}/{side}")
            points = edge["points"]
            if (status == "visible" and (len(points) < 2 or edge["kind"] not in KINDS)) or (status != "visible" and points):
                errors.append(f"Invalid road-edge polyline: {image_id}/{side}")
            for point in points:
                if len(point) != 2 or not all(math.isfinite(v) for v in point) or not (0 <= point[0] < width and 0 <= point[1] < height):
                    errors.append(f"Road-edge coordinates outside original image: {image_id}/{side}")
    if seen != set(expected) or len(records) != manifest["exported"]:
        errors.append("Road-edge annotations and manifest disagree")
    if hashlib.sha256((out / "annotations.jsonl").read_bytes()).hexdigest() != manifest["annotations_sha256"]:
        errors.append("Road-edge annotations checksum does not match the manifest")
    return {"ok": not errors, "checked": len(records), "errors": errors}


def export_road_edges(settings, samples, revision):
    """Publish pairs of explicitly reviewed edges from accepted snapshot tuples.

    samples contains (registry_row, immutable_human_geometry, UFLD_sample_crop).
    The caller owns the same lock and SQLite snapshot as the painted-lane export.
    """
    out = settings.path("exports", "road_edges", "BrakeCheck_Reviewed")
    out.mkdir(parents=True, exist_ok=True)
    (out / "READY.json").unlink(missing_ok=True)
    previous = json.loads((out / "manifest.json").read_text(encoding="utf-8")) if (out / "manifest.json").exists() else {"images": []}
    records, images, licenses = [], [], set()
    counts = dict.fromkeys(("train", "val", "test"), 0)
    statuses = {side: Counter({status: 0 for status in STATUSES}) for side in SIDES}
    skipped = 0
    cfg = settings.get("export.ufld", {})
    for row, geo, crop in samples:
        if not row["locked"] or row["review_status"] not in ("approved", "corrected") or row["pool"] != "commercial":
            raise ValueError("Road-edge export must use only accepted, locked commercial snapshot rows")
        edges = normalize_road_edges(geo.get("road_edges"))
        for side in SIDES:
            statuses[side][edges[side]["status"]] += 1
        if any(edges[side]["status"] == "not_reviewed" for side in SIDES):
            skipped += 1
            continue
        edges = validate_road_edges(edges, row["width"], row["height"])
        policy = geo.get("labeling_policy", {})
        version = policy.get("version") if isinstance(policy, dict) else policy
        if version != LABELING_POLICY["version"]:
            raise ValueError(f"Road edges use an unsupported labeling policy: {row['image_id']}")
        image_id = row["image_id"]
        if not image_id or Path(image_id).name != image_id or any(s in image_id for s in ("/", "\\")):
            raise ValueError("Invalid road-edge image ID")
        source_path = Path(row["path"])
        image = f"images/{row['split']}/{image_id}{source_path.suffix.lower()}"
        _photo(source_path, _inside(out, image))
        model_licenses = geo.get("model_licenses", [])
        licenses.update([row["license_id"], *model_licenses])
        records.append({
            "schema": SCHEMA_ID, "policy_version": LABELING_POLICY["version"], "image_id": image_id,
            "image": image, "split": row["split"],
            "source_image": {"width": row["width"], "height": row["height"], "sha256": row["sha256"],
                             "source": row["source"], "source_ref": row["source_ref"], "session_id": row["session_id"],
                             "license": row["license_id"], "pool": row["pool"]},
            "suggested_crop": {"source_xyxy": crop, "canvas_width": int(cfg.get("canvas_width", 1640)),
                               "canvas_height": int(cfg.get("canvas_height", 590)), "applied": False},
            "human_review": {"label_origin": "human", "decision": row["review_status"], "geometry_version": geo["version"],
                             "reviewer": geo.get("reviewer", ""), "model_licenses": model_licenses,
                             "candidate_provenance": geo.get("candidate_provenance", {}),
                             "road_edge_candidate_provenance": geo.get("road_edge_candidate_provenance", {}),
                             "assist_provenance": geo.get("assist_provenance", [])},
            "road_edges": {side: {"status": edges[side]["status"], "kind": edges[side].get("kind") if edges[side]["status"] == "visible" else None,
                                  "points": edges[side].get("points", []), "training_mask": edges[side]["status"] in ("visible", "absent"),
                                  "is_negative": edges[side]["status"] == "absent"} for side in SIDES},
        })
        images.append({"image_id": image_id, "image": image, "split": row["split"], "geometry_version": geo["version"]})
        counts[row["split"]] += 1
    active = {entry["image"] for entry in images}
    for old in previous["images"]:
        if old["image"] not in active:
            _inside(out, old["image"]).unlink(missing_ok=True)
    _text(out / "annotations.jsonl", "".join(json.dumps(record, sort_keys=True, allow_nan=False) + "\n" for record in records))
    atomic_json(out / "policy.json", _policy())
    atomic_json(out / "schema.json", _schema())
    write_notice(settings, out, licenses)
    with (out / "NOTICE.txt").open("a", encoding="utf-8") as notice:
        notice.write("\nRoad edges: annotations.jsonl contains only human-reviewed labels. Unknown sides are ignored, never negatives. See policy.json.\n")
    # Publish only exact fetched license snapshots used by this artifact.
    license_targets = set()
    for license_id in licenses:
        key = {"sam2": "sam2_large"}.get(license_id, license_id)
        source_license = settings.path("licenses", key + "-LICENSE.txt")
        if source_license.exists():
            target = _inside(out, "licenses/" + source_license.name)
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(source_license, target)
            license_targets.add(target.resolve())
    for old_license in (out / "licenses").glob("*-LICENSE.txt"):
        if old_license.resolve() not in license_targets:
            _inside(out, old_license.relative_to(out)).unlink(missing_ok=True)
    summary = {"schema": SCHEMA_ID, "policy_version": LABELING_POLICY["version"], "revision": revision,
               "exported": len(records), "counts": counts, "accepted_images": len(samples), "excluded_not_reviewed": skipped,
               "status_counts_accepted": {side: dict(counter) for side, counter in statuses.items()},
               "status_counts_exported": {side: dict(Counter(record["road_edges"][side]["status"] for record in records)) for side in SIDES},
               "training_ready": False, "adapter_required": "separate_road_edge_model_and_data_adapter", "training": "deferred", "out": str(out),
               "annotations_sha256": hashlib.sha256((out / "annotations.jsonl").read_bytes()).hexdigest()}
    atomic_json(out / "manifest.json", {**summary, "images": images, "license_ids": sorted(licenses)})
    validation = validate_export(out)
    summary["validation"] = validation
    if not validation["ok"]:
        raise ValueError("Road-edge export validation failed: " + "; ".join(validation["errors"][:5]))
    digest = hashlib.sha256((out / "manifest.json").read_bytes()).hexdigest()
    atomic_json(out / "READY.json", {**summary, "manifest_sha256": digest})
    return summary
