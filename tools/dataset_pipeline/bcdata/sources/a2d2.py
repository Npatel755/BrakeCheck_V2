"""Auditable front-camera import of Audi A2D2's original semantic labels.

Only the official public S3 objects are accepted. Originals remain unchanged;
images stay ``ingested`` until the caller has prepared geometry, QC and the
source-conversion audit. A source annotation is never a human review decision.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from urllib.parse import quote
import xml.etree.ElementTree as ET

import numpy as np
import requests

from ..bootstrap import atomic_json
from ..config import Settings
from ..db import Registry
from ..licenses import LicenseRegistry
from ..quality import read_rgb, sha256_file
from ..splits import split_for
from .base import UA, check_download_budget, download, register_image

log = logging.getLogger(__name__)
BASE_URL = "https://audi-autonomous-driving-dataset.s3.eu-central-1.amazonaws.com/"
PREFIX = "camera_lidar_semantic/"
PALETTE_KEY = PREFIX + "class_list.json"
CALIBRATION_KEY = "cams_lidars.json"
DRIVE_RE = re.compile(r"\d{8}_\d{6}\Z")
IMAGE_RE = re.compile(r"(\d{14})_camera_frontcenter_(\d+)\.png\Z")
MD5_RE = re.compile(r"[0-9a-f]{32}\Z")

# Exact source RGB colours, without a tolerance that could turn another class
# into lane paint. Painted driving instructions and zebra crossings are absent.
LANE_COLORS = ((255, 193, 37), (128, 0, 255))  # Solid line / Dashed line
ROAD_COLOR = (255, 0, 255)
CLASS_COLORS = {1: (ROAD_COLOR,), 2: LANE_COLORS}


def safe_path(root: Path, key: str) -> Path:
    """Resolve one S3 object under the import root; reject platform escapes."""
    pure = PurePosixPath(key)
    if (not key or "\\" in key or ":" in key or pure.is_absolute()
            or any(part in ("", ".", "..") for part in key.split("/"))):
        raise ValueError(f"Unsafe A2D2 object key: {key!r}")
    target = root.joinpath(*pure.parts).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("A2D2 object escaped the dataset directory")
    return target


def _etag(value: str) -> str:
    value = str(value).strip('"').lower()
    if not MD5_RE.fullmatch(value):
        raise ValueError("A2D2 individual files require a single-part MD5 ETag")
    return value


def _object(node: ET.Element, ns: str) -> dict:
    return {"key": node.findtext(ns + "Key"),
            "size": int(node.findtext(ns + "Size") or 0),
            "etag": _etag(node.findtext(ns + "ETag") or "")}


def list_objects(prefix: str, http=None, *, delimiter: str | None = None) -> tuple[list[dict], list[str]]:
    """Read paginated public S3 inventory; do not download dataset archives."""
    if not prefix.startswith(PREFIX) and prefix != CALIBRATION_KEY:
        raise ValueError("Only A2D2's semantic dataset prefix is supported")
    http = http or requests.Session()
    objects, folders, token, seen_tokens = [], [], None, set()
    while True:
        params = {"list-type": "2", "prefix": prefix, "max-keys": 1000}
        if delimiter:
            params["delimiter"] = delimiter
        if token:
            params["continuation-token"] = token
        response = http.get(BASE_URL, params=params, headers=UA, timeout=60)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        ns = root.tag.partition("}")[0] + "}" if root.tag.startswith("{") else ""
        objects.extend(_object(node, ns) for node in root.findall(ns + "Contents")
                       if not (node.findtext(ns + "Key") or "").endswith("/"))
        folders.extend(node.findtext(ns + "Prefix") for node in root.findall(ns + "CommonPrefixes"))
        if root.findtext(ns + "IsTruncated", "false").lower() != "true":
            break
        token = root.findtext(ns + "NextContinuationToken")
        if not token or token in seen_tokens:
            raise ValueError("Invalid repeated or absent A2D2 inventory continuation token")
        seen_tokens.add(token)
    return objects, folders


def inventory(http=None) -> list[dict]:
    """List just front-centre image/label/JSON files and shared source metadata."""
    http = http or requests.Session()
    top, folders = list_objects(PREFIX, http, delimiter="/")
    result = [obj for obj in top if obj["key"] in (PALETTE_KEY, CALIBRATION_KEY)]
    calibration, _ = list_objects(CALIBRATION_KEY, http)
    result.extend(obj for obj in calibration if obj["key"] == CALIBRATION_KEY)
    for folder in sorted(folders):
        drive = PurePosixPath(folder.rstrip("/")).name
        if not DRIVE_RE.fullmatch(drive):
            continue
        for subfolder in ("camera", "label"):
            objects, _ = list_objects(folder + subfolder + "/cam_front_center/", http)
            result.extend(objects)
    return result


def _spread_order(items: list[dict], min_frame_gap: int) -> list[dict]:
    """Space adjacent frames, then cover each drive before filling its gaps."""
    spaced, previous = [], None
    for item in sorted(items, key=lambda item: item["frame"]):
        if previous is None or item["frame"] - previous >= min_frame_gap:
            spaced.append(item)
            previous = item["frame"]
    result, intervals = [], deque([(0, len(spaced))])
    while intervals:
        start, stop = intervals.popleft()
        if start >= stop:
            continue
        middle = (start + stop) // 2
        result.append(spaced[middle])
        intervals.extend(((start, middle), (middle + 1, stop)))
    return result


def _drive_splits(sessions: list[str], fractions: dict, seed: int) -> dict[str, str]:
    """Allocate complete drives by hash rank over the full paired inventory.

    Ranking the inventory before applying an image limit prevents a small
    import from silently changing which drives are reserved for evaluation.
    Largest remainders keep a 23-drive 80/10/10 dataset at 19/2/2 drives.
    """
    names = ("train", "val", "test")
    weights = [float(fractions.get(name, default))
               for name, default in zip(names, (0.8, 0.1, 0.1))]
    if any(not math.isfinite(weight) or weight < 0 for weight in weights) or sum(weights) <= 0:
        raise ValueError("A2D2 split fractions must be finite, non-negative and have a positive sum")
    ordered = sorted(set(sessions), key=lambda key: hashlib.sha256(f"{seed}:{key}".encode()).hexdigest())
    targets = [len(ordered) * weight / sum(weights) for weight in weights]
    counts = [math.floor(target) for target in targets]
    remainder_order = sorted(range(3), key=lambda index: (-(targets[index] - counts[index]), index))
    for index in remainder_order[:len(ordered) - sum(counts)]:
        counts[index] += 1
    # With enough drives, keep every requested holdout non-empty even when
    # rounding a tiny dataset would otherwise allocate all its drives to train.
    if len(ordered) >= sum(weight > 0 for weight in weights):
        for index, weight in enumerate(weights):
            if weight > 0 and counts[index] == 0:
                donor = max(range(3), key=lambda candidate: counts[candidate] - targets[candidate])
                if counts[donor] <= 1:
                    donor = max(range(3), key=lambda candidate: counts[candidate])
                counts[donor] -= 1
                counts[index] += 1
    result, offset = {}, 0
    for name, count in zip(names, counts):
        result.update({session_id: name for session_id in ordered[offset:offset + count]})
        offset += count
    return result


def plan(settings: Settings, session=None, limit: int | None = None, *, objects=None) -> list[dict]:
    """A deterministic, drive-balanced plan containing only fully paired frames."""
    cfg = settings.get("sources.a2d2", {})
    objects = list(objects) if objects is not None else inventory(session)
    lookup = {}
    for obj in objects:
        key = obj["key"]
        safe_path(settings.path("raw", "a2d2"), key)
        if key in lookup and lookup[key] != obj:
            raise ValueError(f"Conflicting A2D2 inventory for {key}")
        lookup[key] = {**obj, "etag": _etag(obj["etag"])}
    for key in (PALETTE_KEY, CALIBRATION_KEY):
        if key not in lookup:
            raise ValueError(f"Missing official A2D2 metadata: {key}")
    grouped = {}
    for key in sorted(lookup):
        parts = PurePosixPath(key).parts
        if len(parts) != 5 or parts[0] != PREFIX.rstrip("/") or parts[2:4] != ("camera", "cam_front_center"):
            continue
        match = IMAGE_RE.fullmatch(parts[4])
        if not match or not DRIVE_RE.fullmatch(parts[1]) or match[1] != parts[1].replace("_", ""):
            continue
        mask = f"{PREFIX}{parts[1]}/label/cam_front_center/{parts[4].replace('_camera_', '_label_')}"
        metadata = key[:-4] + ".json"
        if mask not in lookup:
            continue
        # Two published drives have no per-frame JSON. Keep their original
        # labelled pairs, and record that omission rather than invent metadata.
        metadata = metadata if metadata in lookup else None
        session_id = "a2d2:" + parts[1]
        item = {"img": key, "mask": mask, "metadata": metadata, "name": parts[4],
                "session": session_id, "frame": int(match[2]),
                "palette_meta": lookup[PALETTE_KEY], "calibration_meta": lookup[CALIBRATION_KEY]}
        item.update({name + "_meta": lookup[item[name]] if item[name] else None
                     for name in ("img", "mask", "metadata")})
        grouped.setdefault(session_id, []).append(item)
    policy = cfg.get("split_policy", "drive_rank")
    fractions, seed = settings.get("export.splits", {}), int(settings.get("seed", 0))
    if policy == "drive_rank":
        assignments = _drive_splits(list(grouped), fractions, seed)
    elif policy == "session_hash":
        assignments = {session_id: split_for(session_id, False, fractions, seed) for session_id in grouped}
    else:
        raise ValueError("A2D2 split_policy must be 'drive_rank' or 'session_hash'")
    universe_sha256 = hashlib.sha256(json.dumps(sorted(grouped), separators=(",", ":")).encode()).hexdigest()
    for session_id, items in grouped.items():
        for item in items:
            item.update(split=assignments[session_id], split_policy=policy, session_universe_sha256=universe_sha256)
    spacing = int(cfg.get("min_frame_gap", 30))
    if spacing < 1:
        raise ValueError("A2D2 min_frame_gap must be positive")
    grouped = {key: _spread_order(value, spacing) for key, value in grouped.items()}
    order = sorted(grouped, key=lambda key: hashlib.sha256(f"{settings.get('seed', 0)}:{key}".encode()).hexdigest())
    ordered = [grouped[key][depth] for depth in range(max(map(len, grouped.values()), default=0))
               for key in order if depth < len(grouped[key])]
    limit = int(cfg.get("limit", 0) if limit is None else limit)
    if limit < 0:
        raise ValueError("A2D2 limit cannot be negative")
    return ordered[:limit] if limit else ordered


def _md5_file(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download_object(obj: dict, root: Path, http=None) -> Path:
    """Resume .part files, then verify source size and S3 MD5 before use."""
    target = safe_path(root, obj["key"])
    expected_md5 = _etag(obj["etag"])
    expected_size = int(obj["size"])
    if expected_size <= 0:
        raise ValueError("A2D2 file has no positive expected size")
    if target.exists():
        if target.stat().st_size == expected_size and _md5_file(target) == expected_md5:
            return target
        target.unlink()
    url = BASE_URL + quote(obj["key"], safe="/")
    for attempt in range(2):
        download(url, target, expected_size=expected_size, session=http)
        if _md5_file(target) == expected_md5:
            return target
        target.unlink()
        target.with_suffix(target.suffix + ".part").unlink(missing_ok=True)
    raise IOError(f"A2D2 source checksum mismatch: {obj['key']}")


def mask_to_classes(mask_rgb: np.ndarray, palette: dict | None = None) -> np.ndarray:
    """Map exact source classes; road extent cannot become a painted lane."""
    if mask_rgb.ndim != 3 or mask_rgb.shape[2] != 3:
        raise ValueError("A2D2 labels must be RGB")
    result = np.zeros(mask_rgb.shape[:2], dtype=np.uint8)
    for cls, colors in CLASS_COLORS.items():
        for color in colors:
            result[np.all(mask_rgb == color, axis=2)] = cls
    # Preserve source-labelled vehicle/ego masks when the official palette is
    # supplied. The two lane colours above are the only lane-eligible classes.
    for hex_color, label in (palette or {}).items():
        name = str(label).strip().casefold()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", hex_color):
            continue
        cls = (5 if name == "ego car" else 4 if re.fullmatch(
            r"(?:car|truck|small vehicles|pedestrian|bicycle)\s*\d*", name) else
            3 if name == "non-drivable street" else 0)
        if cls:
            color = tuple(int(hex_color[offset:offset + 2], 16) for offset in (1, 3, 5))
            result[np.all(mask_rgb == color, axis=2)] = cls
    return result


def _validate_palette(path: Path) -> dict:
    palette = json.loads(path.read_text(encoding="utf-8"))
    normalized = {key.casefold(): str(value).strip().casefold() for key, value in palette.items()}
    if normalized.get("#ffc125") != "solid line" or normalized.get("#8000ff") != "dashed line":
        raise ValueError("A2D2 source palette does not match the verified painted-line taxonomy")
    if normalized.get("#ff00ff") != "rd normal street":
        raise ValueError("A2D2 source palette does not match the verified road taxonomy")
    return palette


def ingest(settings: Settings, registry: Registry, licenses: LicenseRegistry, limit: int | None = None,
           approved_gb: float | None = None, workers: int = 4, *, items=None, http=None) -> dict:
    """Download and register source pairs, keeping all new rows out of review."""
    if not 1 <= workers <= 16:
        raise ValueError("A2D2 workers must be between 1 and 16")
    # Resolve source permissions before any dataset download or registration.
    licenses.pool_for_source("a2d2")
    http = http or requests.Session()
    items = list(items) if items is not None else plan(settings, http, limit)
    raw = settings.path("raw", "a2d2")
    objects = {obj["key"]: obj for item in items for obj in
               [item[name + "_meta"] for name in ("img", "mask", "metadata", "palette", "calibration")] if obj}
    needed_bytes = sum(obj["size"] for obj in objects.values()
                       if not safe_path(raw, obj["key"]).exists())
    check_download_budget(settings, needed_bytes, raw, approved_gb)
    counts = {"planned": len(items), "downloaded": 0, "added": 0, "duplicate": 0,
              "rejected": 0, "failed": 0, "staged": 0, "image_ids": [], "errors": []}
    if not items:
        return counts
    palette_path = download_object(items[0]["palette_meta"], raw, http)
    calibration_path = download_object(items[0]["calibration_meta"], raw, http)
    palette = _validate_palette(palette_path)
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    if not isinstance(calibration, dict):
        raise ValueError("A2D2 calibration metadata must be an object")
    shared = {"palette_path": str(palette_path), "palette_sha256": sha256_file(palette_path),
              "calibration_path": str(calibration_path), "calibration_sha256": sha256_file(calibration_path)}
    known_splits = {}
    for row in registry.images("source='a2d2' AND split IS NOT NULL"):
        if row["session_id"] in known_splits and known_splits[row["session_id"]] != row["split"]:
            raise ValueError("Existing A2D2 session crosses splits")
        known_splits[row["session_id"]] = row["split"]

    def fetch(item):
        paths = {name: download_object(item[name + "_meta"], raw, http)
                 for name in ("img", "mask", "metadata") if item[name + "_meta"]}
        rgb, mask = read_rgb(paths["img"]), read_rgb(paths["mask"])
        if rgb is None or mask is None or rgb.shape != mask.shape:
            raise ValueError("A2D2 image and RGB label dimensions do not match")
        if "metadata" in paths:
            metadata = json.loads(paths["metadata"].read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                raise ValueError("A2D2 per-frame source metadata must be an object")
        return paths

    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Register in plan order so reruns preserve deterministic selection.
        futures = [(item, pool.submit(fetch, item)) for item in items]
        for item, future in futures:
            try:
                paths = future.result()
                counts["downloaded"] += 1
                source_ref = BASE_URL + quote(item["img"], safe="/")
                result = register_image(settings, registry, licenses, paths["img"], source="a2d2",
                                        session_id=item["session"], source_ref=source_ref,
                                        frame_index=item["frame"], extra_tags={"camera_domain": "vehicle_front_center",
                                            "phone_capture_verified": False, "a2d2_original_distorted": True})
                counts[result.status] += 1
                if not result.image_id:
                    continue
                row = registry.image(result.image_id)
                if row["source"] != "a2d2" or row["source_ref"] != source_ref:
                    continue  # an exact duplicate must not acquire unrelated labels
                if row["locked"] or registry.annotation(result.image_id, "human") is not None:
                    continue  # rerunning an import cannot touch a reviewed image
                source_ann = registry.annotation(result.image_id, "source_label", "a2d2")
                expected_version = "s3:" + _etag(item["mask_meta"]["etag"])
                if source_ann is not None and source_ann["model_version"] != expected_version:
                    raise ValueError("A2D2 source label revision changed; preserve the registered source before migration")
                if source_ann is None:
                    provenance = {"source": "a2d2", "camera": "front_center", "originals_unchanged": True,
                                  "recording_session": item["session"], "frame_index": item["frame"],
                                  "files": {name: {**item[name + "_meta"], "sha256": sha256_file(path)}
                                            for name, path in paths.items()},
                                  "per_frame_metadata_available": item["metadata_meta"] is not None, **shared}
                    provenance_path = safe_path(raw, f"provenance/{item['session'].split(':', 1)[1]}/{Path(item['img']).stem}.json")
                    atomic_json(provenance_path, provenance)
                    registry.add_annotation(result.image_id, "source_label", "a2d2", expected_version,
                                            str(paths["mask"]), license_ids=["a2d2"],
                                            params={"format": "a2d2_rgb", "human": True, "camera": "front_center",
                                                    "originals_unchanged": True, "provenance_path": str(provenance_path),
                                                    "provenance_sha256": sha256_file(provenance_path), **shared})
                split = known_splits.setdefault(item["session"], item["split"])
                if row["split"] and row["split"] != split:
                    raise ValueError("A2D2 import would change a frozen split")
                registry.update_image(result.image_id, has_source_label=1, split=split)
                counts["image_ids"].append(result.image_id)
                if row["status"] == "ingested":
                    counts["staged"] += 1
            except Exception as exc:
                counts["failed"] += 1
                counts["errors"].append({"image": item["img"], "error_type": type(exc).__name__, "error": str(exc)})
                log.error("A2D2 %s: %s", item["name"], exc)
    return counts
