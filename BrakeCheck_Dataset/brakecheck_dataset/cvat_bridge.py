"""Offline CVAT 1.1 handoff and append-only human review intake.

No inference output is promoted to a reviewed label by this module. A person
must supply one explicit decision per image when importing a CVAT export.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import tempfile
import xml.etree.ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

import numpy as np
from PIL import Image

from .pipeline import setup


CVAT_FORMAT = "CVAT for images 1.1"
DECISIONS = frozenset({"approved", "corrected", "rejected", "ambiguous"})
_IMAGE_ID = re.compile(r"^[a-f0-9]{24}$")
_XML_LIMIT = 100_000_000


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_relative(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative.replace("\\", "/"))
    if not path.parts or path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        raise ValueError(f"Unsafe dataset path: {relative}")
    resolved = (root / Path(*path.parts)).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"Dataset path leaves root: {relative}")
    return resolved


def encode_cvat_mask(mask: np.ndarray) -> dict[str, str] | None:
    """Encode one binary bitmap in CVAT's cropped, row-major, zero-first RLE."""
    if mask.ndim != 2:
        raise ValueError("Mask must be 2D")
    mask = mask.astype(bool, copy=False)
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return None
    left, top, right, bottom = int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())
    flat = mask[top:bottom + 1, left:right + 1].ravel(order="C")
    changes = np.flatnonzero(flat[1:] != flat[:-1]) + 1
    runs = np.diff(np.r_[0, changes, len(flat)]).tolist()
    if flat[0]:
        runs.insert(0, 0)
    return {"rle": ", ".join(map(str, runs)), "left": str(left), "top": str(top),
            "width": str(right - left + 1), "height": str(bottom - top + 1)}


def decode_cvat_mask(element: ET.Element, width: int, height: int) -> np.ndarray:
    """Decode and bounds-check one CVAT mask without trusting its RLE length."""
    try:
        left, top = int(element.attrib["left"]), int(element.attrib["top"])
        crop_width, crop_height = int(element.attrib["width"]), int(element.attrib["height"])
        runs = [int(part.strip()) for part in element.attrib["rle"].split(",")]
    except (KeyError, ValueError) as error:
        raise ValueError("Invalid CVAT mask fields") from error
    if not (0 <= left < width and 0 <= top < height and
            0 < crop_width <= width - left and 0 < crop_height <= height - top):
        raise ValueError("CVAT mask exceeds image bounds")
    if not runs or any(run < 0 for run in runs) or sum(runs) != crop_width * crop_height:
        raise ValueError("Invalid CVAT mask RLE length")
    flat = np.zeros(crop_width * crop_height, dtype=bool)
    cursor = 0
    for index, run in enumerate(runs):
        if index % 2:
            flat[cursor:cursor + run] = True
        cursor += run
    result = np.zeros((height, width), dtype=bool)
    result[top:top + crop_height, left:left + crop_width] = flat.reshape(crop_height, crop_width)
    return result


def _label_name(role: str, model: str, checkpoint: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")[:42]
    return f"{role}__{slug}__{checkpoint[:10]}"


def _read_mask(path: Path, size: tuple[int, int], *, red_only: bool = False) -> np.ndarray:
    with Image.open(path) as opened:
        if opened.size != size:
            raise ValueError(f"Mask dimensions disagree with source image: {path}")
        if red_only:
            pixels = np.asarray(opened.convert("RGB"))
            return np.all(pixels == (255, 0, 0), axis=2)
        return np.asarray(opened.convert("L")) > 0


def _add_mask(image: ET.Element, label: str, mask: np.ndarray, source: str) -> None:
    encoded = encode_cvat_mask(mask)
    if encoded is not None:
        ET.SubElement(image, "mask", {"label": label, "source": source,
                                          "occluded": "0", "z_order": "0", **encoded})


def _geometry_segments(root: Path, image_id: str, size: tuple[int, int]) -> list[list[list[float]]]:
    path = root / "lane_geometry" / f"{image_id}.json"
    if not path.exists():
        return []
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("image_id") != image_id or (document.get("width"), document.get("height")) != size:
        raise ValueError(f"Lane geometry identity/size mismatch: {image_id}")
    segments = []
    for lane in document.get("lanes", []):
        if lane.get("kind") != "physical":
            continue
        for segment in lane.get("visible_segments") or [lane.get("points", [])]:
            if len(segment) < 2:
                continue
            points = []
            for point in segment:
                if len(point) != 2 or not all(isinstance(value, (int, float)) and
                    np.isfinite(value) for value in point):
                    raise ValueError(f"Invalid geometry point: {image_id}")
                x, y = map(float, point)
                if not (0 <= x < size[0] and 0 <= y < size[1]):
                    raise ValueError(f"Lane geometry exceeds image: {image_id}")
                points.append([x, y])
            segments.append(points)
    return segments


def _zip_add_bytes(archive: ZipFile, name: str, data: bytes) -> None:
    from zipfile import ZipInfo
    item = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    item.compress_type = ZIP_DEFLATED
    item.external_attr = 0o644 << 16
    archive.writestr(item, data)


def export_cvat_bundle(root: Path, destination: Path, *, limit: int = 100,
                       image_ids: list[str] | None = None, include_road: bool = False,
                       include_source_lane: bool = True) -> dict:
    """Build a deterministic, local CVAT-for-images ZIP of provisional labels.

    Images are always full-color originals. Each teacher/checkpoint gets a
    separate CVAT label, so reviewers can toggle candidate masks by label.
    Optional precomputed physical lane segments are exported as polylines.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    root, destination = Path(root), Path(destination)
    db = setup(root)
    try:
        if image_ids is None:
            rows = db.execute("""SELECT i.image_id,i.image_path,i.mask_path,i.width,i.height
                FROM images i WHERE EXISTS (SELECT 1 FROM predictions p WHERE p.image_id=i.image_id)
                ORDER BY i.image_id LIMIT ?""", (limit,)).fetchall()
        else:
            if len(image_ids) > limit or len(set(image_ids)) != len(image_ids):
                raise ValueError("Invalid or duplicate image ID list")
            rows = []
            for image_id in image_ids:
                if not _IMAGE_ID.fullmatch(image_id):
                    raise ValueError(f"Invalid image ID: {image_id}")
                row = db.execute("SELECT image_id,image_path,mask_path,width,height FROM images WHERE image_id=?",
                                 (image_id,)).fetchone()
                if row is None:
                    raise ValueError(f"Image not in registry: {image_id}")
                rows.append(row)
        if not rows:
            raise ValueError("No registered images with teacher predictions")
        rows.sort(key=lambda row: row[0])
        predictions = {}
        labels = set()
        for image_id, *_ in rows:
            found = db.execute("""SELECT model_name,checkpoint_sha256,lane_mask_path,road_mask_path
                FROM predictions WHERE image_id=? ORDER BY model_name,checkpoint_sha256""", (image_id,)).fetchall()
            predictions[image_id] = found
            for model, checkpoint, _, _ in found:
                labels.add(_label_name("lane_marking", model, checkpoint))
                if include_road:
                    labels.add(_label_name("road_surface", model, checkpoint))
        if include_source_lane and any(row[2] for row in rows):
            labels.add("source_lane_marking")
        labels.add("lane_boundary_candidate")
        xml_root = ET.Element("annotations")
        ET.SubElement(xml_root, "version").text = "1.1"
        task = ET.SubElement(ET.SubElement(xml_root, "meta"), "task")
        ET.SubElement(task, "name").text = "BrakeCheck provisional lane review"
        ET.SubElement(task, "size").text = str(len(rows))
        ET.SubElement(task, "mode").text = "annotation"
        label_list = ET.SubElement(task, "labels")
        for name in sorted(labels):
            label = ET.SubElement(label_list, "label")
            ET.SubElement(label, "name").text = name
            ET.SubElement(label, "type").text = "polyline" if name == "lane_boundary_candidate" else "mask"
            ET.SubElement(label, "attributes")
        files = []
        for index, (image_id, relative_image, relative_source_mask, width, height) in enumerate(rows):
            source = _safe_relative(root, relative_image)
            if not source.is_file():
                raise FileNotFoundError(source)
            with Image.open(source) as opened:
                if opened.size != (width, height):
                    raise ValueError(f"Original image dimensions changed: {image_id}")
            image_name = f"{image_id}{source.suffix.lower()}"
            image = ET.SubElement(xml_root, "image", {"id": str(index), "name": image_name,
                                                        "width": str(width), "height": str(height)})
            if include_source_lane and relative_source_mask:
                _add_mask(image, "source_lane_marking",
                          _read_mask(_safe_relative(root, relative_source_mask), (width, height), red_only=True),
                          "manual")
            for model, checkpoint, lane_path, road_path in predictions[image_id]:
                _add_mask(image, _label_name("lane_marking", model, checkpoint),
                          _read_mask(_safe_relative(root, lane_path), (width, height)), "auto")
                if include_road:
                    _add_mask(image, _label_name("road_surface", model, checkpoint),
                              _read_mask(_safe_relative(root, road_path), (width, height)), "auto")
            for segment in _geometry_segments(root, image_id, (width, height)):
                points = ";".join(f"{x:.2f},{y:.2f}" for x, y in segment)
                ET.SubElement(image, "polyline", {"label": "lane_boundary_candidate", "source": "auto",
                                                  "points": points, "occluded": "0", "z_order": "1"})
            files.append((f"images/{image_name}", source))
        document = ET.tostring(xml_root, encoding="utf-8", xml_declaration=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".zip", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            with ZipFile(temporary_path, "w") as archive:
                _zip_add_bytes(archive, "annotations.xml", document)
                for name, path in files:
                    _zip_add_bytes(archive, name, path.read_bytes())
            if destination.exists():
                if _digest(destination.read_bytes()) != _digest(temporary_path.read_bytes()):
                    raise FileExistsError(f"Existing CVAT bundle differs: {destination}")
                created = False
            else:
                os.replace(temporary_path, destination)
                created = True
        finally:
            temporary_path.unlink(missing_ok=True)
        return {"format": CVAT_FORMAT, "images": len(rows), "labels": sorted(labels),
                "bundle": str(destination), "created": created}
    finally:
        db.close()


def _xml_bytes(path: Path) -> bytes:
    if path.suffix.lower() == ".xml":
        if path.stat().st_size > _XML_LIMIT:
            raise ValueError("CVAT XML exceeds size limit")
        data = path.read_bytes()
    elif path.suffix.lower() == ".zip":
        with ZipFile(path) as archive:
            candidates = [item for item in archive.infolist()
                          if PurePosixPath(item.filename).name == "annotations.xml"]
            if len(candidates) != 1 or candidates[0].file_size > _XML_LIMIT:
                raise ValueError("Expected one bounded annotations.xml in CVAT ZIP")
            data = archive.read(candidates[0])
    else:
        raise ValueError("Expected a CVAT .xml or .zip export")
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("DTD/entities are not accepted in review exports")
    return data


def _parse_points(raw: str, width: int, height: int) -> list[list[float]]:
    result = []
    for pair in raw.split(";"):
        try:
            x, y = [float(value) for value in pair.split(",")]
        except ValueError as error:
            raise ValueError("Invalid CVAT polyline coordinate") from error
        if not (np.isfinite(x) and np.isfinite(y) and 0 <= x < width and 0 <= y < height):
            raise ValueError("CVAT polyline exceeds image bounds")
        result.append([x, y])
    if len(result) < 2:
        raise ValueError("A reviewed polyline requires at least two points")
    return result


def _decisions(path: Path) -> dict[str, dict]:
    decisions = {}
    with Path(path).open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            item = json.loads(line)
            image_id, decision = item.get("image_id"), item.get("decision")
            if not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id) or decision not in DECISIONS:
                raise ValueError(f"Invalid review decision at line {line_number}")
            if image_id in decisions:
                raise ValueError(f"Duplicate decision for {image_id}")
            seconds = item.get("review_seconds")
            if seconds is not None and (not isinstance(seconds, (int, float)) or
                                        not np.isfinite(seconds) or seconds < 0):
                raise ValueError(f"Invalid review_seconds at line {line_number}")
            if decision in {"rejected", "ambiguous"} and not str(item.get("reason", "")).strip():
                raise ValueError(f"Reason required for {decision}: {image_id}")
            decisions[image_id] = item
    return decisions


def _review_tables(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS human_reviews (
        review_id TEXT PRIMARY KEY, image_id TEXT NOT NULL, reviewer TEXT NOT NULL,
        decision TEXT NOT NULL, annotation_sha256 TEXT NOT NULL,
        annotation_path TEXT NOT NULL, review_seconds REAL, reason TEXT,
        scene_category TEXT, recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (image_id) REFERENCES images(image_id)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS human_reviews_image ON human_reviews(image_id)")
    db.commit()


def import_cvat_review(root: Path, cvat_export: Path, decisions_file: Path, *,
                       reviewer: str, allow_supersede: bool = False) -> dict:
    """Import CVAT annotations only with explicit per-image human decisions.

    Each review is saved under its content hash with an append-only database
    event. An approved/corrected review can be superseded only by an explicit
    human call with ``allow_supersede=True``; AI pipelines never call this.
    """
    reviewer = reviewer.strip()
    if not reviewer or len(reviewer) > 100:
        raise ValueError("A named human reviewer is required")
    root = Path(root)
    decisions = _decisions(decisions_file)
    xml_root = ET.fromstring(_xml_bytes(Path(cvat_export)))
    if xml_root.tag != "annotations" or xml_root.findtext("version") != "1.1":
        raise ValueError("Expected CVAT for images 1.1 XML")
    image_elements = xml_root.findall("image")
    if not image_elements:
        raise ValueError("CVAT export contains no images")
    db = setup(root)
    _review_tables(db)
    prepared = []
    try:
        seen = set()
        for image in image_elements:
            filename = PurePosixPath(image.attrib.get("name", "")).name
            image_id = filename.split(".", 1)[0]
            if not _IMAGE_ID.fullmatch(image_id) or image_id in seen:
                raise ValueError(f"Unknown or duplicate CVAT image name: {filename}")
            seen.add(image_id)
            row = db.execute("SELECT width,height,review_status FROM images WHERE image_id=?", (image_id,)).fetchone()
            if row is None:
                raise ValueError(f"CVAT image is not registered: {image_id}")
            width, height, current_status = row
            if (int(image.attrib.get("width", -1)), int(image.attrib.get("height", -1))) != (width, height):
                raise ValueError(f"CVAT image dimensions changed: {image_id}")
            if current_status in {"approved", "corrected"} and not allow_supersede:
                raise PermissionError(f"Already approved; explicit supersede required: {image_id}")
            for shape in image:
                if shape.tag == "mask":
                    decode_cvat_mask(shape, width, height)
                elif shape.tag == "polyline":
                    _parse_points(shape.attrib.get("points", ""), width, height)
                elif shape.tag != "tag":
                    raise ValueError(f"Unsupported CVAT shape {shape.tag}: {image_id}")
                if not shape.attrib.get("label"):
                    raise ValueError(f"Shape without label: {image_id}")
            canonical = ET.tostring(image, encoding="utf-8")
            decision = decisions.get(image_id)
            if decision is None:
                raise ValueError(f"Missing explicit review decision: {image_id}")
            digest = _digest(canonical)
            event = f"{image_id}:{reviewer}:{decision['decision']}:{digest}"
            review_id = _digest(event.encode())[:32]
            destination = root / "annotations/reviewed" / image_id / f"{review_id}.xml"
            prepared.append((image_id, decision, digest, review_id, destination, canonical))
        if set(decisions) != seen:
            raise ValueError("Decision file contains images absent from CVAT export")
        written = []
        try:
            db.execute("BEGIN IMMEDIATE")
            imported = skipped = 0
            for image_id, decision, digest, review_id, path, canonical in prepared:
                previous = db.execute("SELECT annotation_sha256 FROM human_reviews WHERE review_id=?",
                                      (review_id,)).fetchone()
                if previous:
                    if previous[0] != digest or not path.is_file() or path.read_bytes() != canonical:
                        raise ValueError(f"Existing immutable review was altered: {image_id}")
                    skipped += 1
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with path.open("xb") as output:
                        output.write(canonical)
                except FileExistsError as error:
                    raise ValueError(f"Review path already exists without matching event: {path}") from error
                written.append(path)
                db.execute("""INSERT INTO human_reviews
                    (review_id,image_id,reviewer,decision,annotation_sha256,annotation_path,
                     review_seconds,reason,scene_category) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (review_id, image_id, reviewer, decision["decision"], digest,
                     path.relative_to(root).as_posix(), decision.get("review_seconds"),
                     decision.get("reason"), decision.get("scene_category")))
                db.execute("UPDATE images SET review_status=? WHERE image_id=?",
                           (decision["decision"], image_id))
                imported += 1
            db.commit()
        except Exception:
            db.rollback()
            for path in written:
                path.unlink(missing_ok=True)
            raise
        return {"images": len(prepared), "imported": imported, "skipped": skipped,
                "reviewer": reviewer, "status": "explicit human decisions only"}
    finally:
        db.close()
