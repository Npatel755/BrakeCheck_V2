"""Isolated, resumable PandaSet+ front-camera lane-candidate import.

Only the authors' paired 2D front-camera JSON and unchanged PandaSet JPEGs are
read. No production labels, registry, collections, models or training are touched.
The native labels do not identify paint/curbs, visible/occluded points or lane
slots: converted curves are candidates requiring the separate source audit.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import threading
from urllib.parse import quote, unquote
import zipfile

import requests
from PIL import Image

from .http_range_zip import HTTPRangeReader, request

SOURCE = "pandaset_plus"
SCHEMA = "real_lane_import:v1"
CONVERTER_VERSION = "pandaset-plus-front-candidates-v1"
COMMIT = "736d83a3b772bd3aa3c3928025a19de7d98a7698"
DATASET_VERSION = 1
WIDTH, HEIGHT = 1920, 1080
RAW = f"https://raw.githubusercontent.com/leiqin1/pandaset_plus/{COMMIT}/"
DOWNLOAD = "https://www.kaggle.com/api/v1/datasets/download/pz19930809/pandaset"
AUTHOR_URL = "https://github.com/leiqin1/pandaset_plus"
BASE_URL = "https://www.kaggle.com/datasets/pz19930809/pandaset/data"
ANNOTATION_POLICY_RISKS = ["native_visibility_unspecified", "native_boundary_type_unspecified",
                           "native_occlusion_unspecified"]
PARTS = (
    ("lanes_front.zip.001", 26214400, "edafd3197d7d6899bc2ab702dc3f8a67adef3997452ef899ad5d41d416def05c"),
    ("lanes_front.zip.002", 26214400, "0b2b3db487b48747acf1abb6082a22a0b33bacab55dd02f8a4d95b00c53d84df"),
    ("lanes_front.zip.003", 3184293, "a440f9d2cb877c351767fbde2a291b0fe157cc922fd61b754a33dedaa6eee194"),
)
DOCUMENTS = {
    "README.md": (RAW + "README.md", "cc7978c944973365b83c7eba92c794f0d6a9ec91172d1f9810b8def5c5290061"),
    "CC-BY-4.0": (RAW + "CC-BY-4.0", "cce5d01fa4a83b794271bd2c28cffdf99afd43c803e6ddefddae39b591ea7448"),
    "CODE-LICENSE-MIT": (RAW + "LICENSE", "1a7e6a34f3cef5af81fb0421e7295666fea963e77778290097c73a23e8817423"),
    "lane_train_test_split.ipynb": (RAW + "devkit/lane_train_test_split.ipynb", "e2f236c2d95130e60f3c2261a9aaf67df6e754ab6cfe0a9e07e38b38e42bad2c"),
    "PandaSet-LICENSE.txt": (DOWNLOAD + "/" + quote("PandaSet/001/LICENSE.txt", safe="") + "?datasetVersionNumber=1",
                            "b198d2cb6ce184a0cc586ffd4513dd7e9060b0e10686b94768c845f8b94cdcf7"),
}
_MEMBER = re.compile(r"([0-9]{3})/([0-7][0-9])\.json\Z")
_IMAGE = re.compile(r"PandaSet/([0-9]{3})/camera/front_camera/([0-7][0-9])\.jpg\Z")
_HTTP = threading.local()


def now():
    return datetime.now(timezone.utc).isoformat()


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def atomic_bytes(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.part")
    with temporary.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def atomic_json(path, value):
    atomic_bytes(path, json_bytes(value))


def fetch(url, *, attempts=7):
    if not hasattr(_HTTP, "session"):
        _HTTP.session = requests.Session()
    response = request(_HTTP.session, url, attempts=attempts,
                       headers={"User-Agent": "BrakeCheck-PandaSetPlus-Import/1.0"})
    try:
        raw = response.raw.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError("Selective PandaSet+ download exceeds the 32 MB bound")
        return raw
    finally:
        response.close()


def ensure_upstream(output, *, fetcher=fetch):
    """Pin licenses, official split and all three front-only annotation parts."""
    upstream = Path(output) / "upstream"
    sources = dict(DOCUMENTS)
    sources.update({name: (RAW + "annotations/lanes/lanes_2D/front_camera/" + name, digest)
                    for name, _, digest in PARTS})
    receipts = []
    for name, (url, expected) in sources.items():
        path = upstream / name
        if path.exists():
            raw = path.read_bytes()
            if sha256(raw) != expected:
                raise ValueError(f"Pinned upstream checksum mismatch: {name}")
        else:
            raw = fetcher(url)
            if sha256(raw) != expected:
                raise ValueError(f"Pinned upstream checksum mismatch: {name}")
            atomic_bytes(path, raw)
        receipts.append({"file": "upstream/" + name, "url": url, "sha256": expected, "bytes": len(raw)})
    atomic_json(Path(output) / "provenance.json", {
        "schema": "pandaset_plus_provenance:v1", "authors_repository": AUTHOR_URL,
        "authors_commit": COMMIT, "base_dataset": BASE_URL, "base_dataset_version": DATASET_VERSION,
        "license": "CC-BY-4.0", "additional_base_terms": "upstream/PandaSet-LICENSE.txt",
        "attribution": "PandaSet+: Enhanced and Augmented Annotations for Panoptic Perception (ICAIBD 2024), "
                       "Lei Qin and coauthors; PandaSet by Hesai and Scale AI.",
        "changes": "Unchanged JPEG/native JSON; front-only selection and separate candidate coordinate conversion.",
        "sources": receipts})
    return upstream


def official_splits(raw):
    """Read literal split lists; never execute the upstream notebook."""
    notebook = json.loads(raw)
    found = {}
    for cell in notebook.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        text = "".join(cell.get("source", []))
        if "train_seq_front" not in text and "test_seq_front" not in text:
            continue
        for node in ast.parse(text).body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"train_seq_front", "test_seq_front"}:
                    values = ast.literal_eval(node.value)
                    if target.id in found or not isinstance(values, list) or any(
                            not isinstance(s, str) or not re.fullmatch(r"[0-9]{3}", s) for s in values):
                        raise ValueError("Ambiguous official front-camera split")
                    if len(values) != len(set(values)):
                        raise ValueError("Duplicate official front-camera sequence")
                    found[target.id] = set(values)
    if set(found) != {"train_seq_front", "test_seq_front"}:
        raise ValueError("Official front-camera split is absent")
    train, test = found["train_seq_front"], found["test_seq_front"]
    if not train or not test or train & test:
        raise ValueError("Official front-camera TRAIN/TEST overlap or empty split")
    return train, test


def image_id(sequence, frame):
    if not re.fullmatch(r"[0-9]{3}", sequence) or not re.fullmatch(r"[0-7][0-9]", frame):
        raise ValueError("Expected a three-digit PandaSet sequence and frame 00..79")
    return "panda-" + sha256(f"pandaset_plus|v1|front_camera|{sequence}|{frame}".encode())[:16]


def download_url(sequence, frame):
    image_id(sequence, frame)
    member = f"PandaSet/{sequence}/camera/front_camera/{frame}.jpg"
    return DOWNLOAD + "/" + quote(member, safe="") + "?datasetVersionNumber=1"


def source_records(upstream, *, expected=PARTS, expected_count=6720):
    upstream = Path(upstream)
    chunks = []
    for name, length, digest in expected:
        raw = (upstream / name).read_bytes()
        if len(raw) != length or sha256(raw) != digest:
            raise ValueError(f"Pinned front-camera annotation checksum differs: {name}")
        chunks.append(raw)
    train, test = official_splits((upstream / "lane_train_test_split.ipynb").read_bytes())
    records, seen = [], set()
    with zipfile.ZipFile(io.BytesIO(b"".join(chunks))) as archive:
        for info in archive.infolist():
            if info.orig_filename != info.filename:
                raise ValueError("Noncanonical front-camera annotation member")
            if info.is_dir():
                if not re.fullmatch(r"[0-9]{3}/", info.filename):
                    raise ValueError("Unexpected native annotation archive directory")
                continue
            match = _MEMBER.fullmatch(info.filename)
            if (not match or info.filename in seen or info.flag_bits & 1
                    or not 0 < info.file_size <= 4 * 1024 * 1024):
                raise ValueError("Unexpected/duplicate/unsafe front-camera annotation member")
            seen.add(info.filename)
            sequence, frame = match.groups()
            raw = archive.read(info)  # ZipFile verifies each member's CRC.
            native = json.loads(raw)
            if not isinstance(native, list):
                raise ValueError("Native PandaSet+ labels must be lane lists")
            split = "train" if sequence in train else "test" if sequence in test else "hold"
            records.append({"image_id": image_id(sequence, frame), "sequence": sequence, "frame": frame,
                            "split": split, "session_id": f"pandaset_plus/{sequence}/front_camera",
                            "native_raw": raw, "native_member": info.filename})
    if expected_count is not None and len(records) != expected_count:
        raise ValueError("Pinned front-camera annotation count differs")
    return sorted(records, key=lambda row: (row["sequence"], row["frame"]))


def _finite_number(value):
    return type(value) in {float, int} and math.isfinite(value)


def _reference_x(points, reference_y, height):
    """Extrapolation is for slot choice only; never extend the emitted curve."""
    if reference_y <= points[-1][1]:
        for a, b in zip(points, points[1:]):
            if a[1] <= reference_y <= b[1]:
                return a[0] + (reference_y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
    end = points[-1][1]
    fit = [p for p in points if p[1] >= end - max(height * .10, 1)]
    if len(fit) < 2:
        fit = points[-2:]
    mean_y = sum(p[1] for p in fit) / len(fit)
    mean_x = sum(p[0] for p in fit) / len(fit)
    denominator = sum((p[1] - mean_y) ** 2 for p in fit)
    slope = sum((p[0] - mean_x) * (p[1] - mean_y) for p in fit) / denominator if denominator else 0
    return mean_x + (reference_y - mean_y) * slope


def native_geometry(native, *, identifier, width=WIDTH, height=HEIGHT):
    """Convert observable coordinates, without claiming unknown paint semantics.

    Never fills the road from geometry or creates a road-edge target. Native
    projections are kept verbatim separately. Out-of-frame samples and malformed
    curves are held; no clamping or fabricated endpoints. Minor annotation jitter
    is sorted by y for interpolation, with substantial folds held for correction.
    """
    if not isinstance(native, list) or len(native) > 128:
        raise ValueError("Native PandaSet+ label must be a bounded list of lane polylines")
    reasons, candidates = set(), []
    for index, line in enumerate(native):
        if (not isinstance(line, list) or len(line) > 20000 or any(
                not isinstance(p, list) or len(p) != 2 or not all(_finite_number(v) for v in p) for p in line)):
            reasons.add("native_malformed_lane_coordinates")
            continue
        if len(line) < 2:
            reasons.add("native_lane_has_fewer_than_two_points")
            continue
        in_frame = [p for p in line if 0 <= p[0] < width and 0 <= p[1] < height]
        if len(in_frame) != len(line):
            reasons.add("native_points_outside_image")
        if len(in_frame) < 2:
            reasons.add("native_lane_has_fewer_than_two_in_frame_points")
            continue
        # Duplicate y samples cannot define x(y); retain a deterministic mean.
        by_y = defaultdict(list)
        for x, y in in_frame:
            by_y[float(y)].append(float(x))
        points = [[sum(xs) / len(xs), y] for y, xs in sorted(by_y.items())]
        if len(points) < 2:
            reasons.add("native_lane_has_no_vertical_extent")
            continue
        y_span = points[-1][1] - points[0][1]
        x_span = max(p[0] for p in points) - min(p[0] for p in points)
        if y_span < .05 * height:
            reasons.add("native_lane_short_vertical_span")
        if x_span > .10 * width and y_span / height < .4 * x_span / width:
            reasons.add("native_transverse_or_turning_projection")
        direction = 1 if in_frame[-1][1] >= in_frame[0][1] else -1
        backwards = max([0.] + [-(b[1] - a[1]) * direction for a, b in zip(in_frame, in_frame[1:])])
        if backwards > .01 * height:
            reasons.add("native_lane_folds_in_y")
        reference_y = height * .90
        if reference_y - points[-1][1] > .35 * height:
            reasons.add("native_slot_reference_too_far_from_observed_curve")
        x_reference = _reference_x(points, reference_y, height)
        candidates.append({"native_lane_index": index, "points": points,
                           "polyline_points": [list(p) for p in in_frame],
                           "y_top": points[0][1], "y_bottom": points[-1][1],
                           "fit": {"basis": "interp", "ys": [p[1] for p in points], "xs": [p[0] for p in points]},
                           "physical": False, "kind": "unknown", "origin": "pandaset_plus_native_candidate",
                           "paint_semantics_verified": False, "slot_reference_x": x_reference})
    if not native:
        reasons.add("native_empty_projection_not_verified_no_lanes")
    left = sorted([lane for lane in candidates if lane["slot_reference_x"] < width / 2],
                  key=lambda lane: lane["slot_reference_x"], reverse=True)
    right = sorted([lane for lane in candidates if lane["slot_reference_x"] >= width / 2],
                   key=lambda lane: lane["slot_reference_x"])
    if len(left) > 2 or len(right) > 2:
        reasons.add("native_more_than_two_boundaries_on_one_side")
    result = []
    for side, slots in ((left, (2, 1)), (right, (3, 4))):
        for lane, slot in zip(side, slots):
            lane["slot"] = slot
            result.append(lane)
    # Crossing native candidates must not silently become an approved ego pair.
    ordered = sorted(result, key=lambda lane: lane["slot"])
    for a, b in zip(ordered, ordered[1:]):
        top, bottom = max(a["y_top"], b["y_top"]), min(a["y_bottom"], b["y_bottom"])
        if bottom - top >= .05 * height:
            for ratio in (.25, .5, .75):
                y = top + ratio * (bottom - top)
                if _reference_x(a["points"], y, height) >= _reference_x(b["points"], y, height):
                    reasons.add("native_lane_order_crossing")
    return {"image_id": identifier, "width": width, "height": height, "version": CONVERTER_VERSION,
            "origin": "pandaset_plus_native_candidate", "annotation_policy_risks": list(ANNOTATION_POLICY_RISKS),
            "annotation_policy": "Native lane candidates; paint/curb and visible/occluded semantics require audit.",
            "native_lane_count": len(native), "lanes": ordered}, sorted(reasons)


def verify_jpeg(raw):
    if len(raw) > 12 * 1024 * 1024:
        raise ValueError("PandaSet front JPEG exceeds the 12 MB bound")
    with Image.open(io.BytesIO(raw)) as image:
        if image.format != "JPEG" or image.size != (WIDTH, HEIGHT):
            raise ValueError("Expected an unchanged 1920 x 1080 PandaSet front JPEG")
        image.load()


class FrontImageArchive:
    """Select paired front JPEG members by HTTPS range, never other sensors.

    One bounded reader per download thread avoids thousands of rate-limited
    per-file API redirects. Archive metadata is read, but only permitted JPEG
    members are extracted. Signed storage URLs stay in memory.
    """
    transport = "pinned_v1_front_jpeg_zip_ranges"

    def __init__(self, records, *, reader_factory=HTTPRangeReader):
        self.allowed = {f"PandaSet/{r['sequence']}/camera/front_camera/{r['frame']}.jpg" for r in records}
        self._reader_factory, self._local, self._lock = reader_factory, threading.local(), threading.Lock()
        self._readers = []

    def __call__(self, url):
        prefix, suffix = DOWNLOAD + "/", "?datasetVersionNumber=1"
        if not url.startswith(prefix) or not url.endswith(suffix):
            raise ValueError("Archive permits only pinned paired PandaSet front JPEG URLs")
        member = unquote(url[len(prefix):-len(suffix)])
        if not _IMAGE.fullmatch(member) or member not in self.allowed:
            raise ValueError("Archive member is not a paired front-camera image")
        sequence, frame = _IMAGE.fullmatch(member).groups()
        if url != download_url(sequence, frame):
            raise ValueError("Noncanonical front-camera image URL")
        if not hasattr(self._local, "archive"):
            reader = self._reader_factory(DOWNLOAD + suffix, block_size=1024 * 1024, max_blocks=3)
            try:
                archive = zipfile.ZipFile(reader)
                names = archive.namelist()
                if len(names) != len(set(names)):
                    raise ValueError("Ambiguous duplicate members in official PandaSet archive")
            except Exception:
                reader.close()
                raise
            self._local.archive = archive
            with self._lock:
                self._readers.append((reader, archive))
        info = self._local.archive.getinfo(member)
        if info.is_dir() or info.flag_bits & 1 or not 0 < info.file_size <= 12 * 1024 * 1024:
            raise ValueError("Invalid or encrypted front-camera archive member")
        return self._local.archive.read(info)  # CRC checked, unchanged JPEG bytes.

    def stats(self):
        return {"transport": self.transport, "archive_bytes_downloaded": sum(r.bytes_downloaded for r, _ in self._readers),
                "archive_range_requests": sum(r.range_requests for r, _ in self._readers)}

    def close(self):
        for reader, archive in self._readers:
            archive.close()
            reader.close()
        self._readers.clear()


def convert_record(output, record, *, fetcher=fetch):
    output = Path(output)
    identifier = record["image_id"]
    image_rel, native_rel, geometry_rel = (f"images/{identifier}.jpg", f"native/{identifier}.json", f"geometry/{identifier}.json")
    receipt_path = output / "receipts" / f"{identifier}.json"
    path = output / image_rel
    raw = None
    if path.is_file():
        try:
            raw = path.read_bytes()
            verify_jpeg(raw)
            if receipt_path.is_file():
                receipt = json.loads(receipt_path.read_bytes())
                if (receipt.get("source_sha256") != sha256(raw) or receipt.get("dataset_version") != DATASET_VERSION
                        or receipt.get("native_member") != record["native_member"]):
                    raise ValueError("PandaSet receipt differs from the existing JPEG")
        except (ValueError, OSError, SyntaxError):
            raw = None
    if raw is None:
        raw = fetcher(download_url(record["sequence"], record["frame"]))
        verify_jpeg(raw)
        atomic_bytes(path, raw)
    native_raw = record["native_raw"]
    geometry, reasons = native_geometry(json.loads(native_raw), identifier=identifier)
    geometry_raw = json_bytes(geometry)
    atomic_bytes(output / native_rel, native_raw)
    atomic_bytes(output / geometry_rel, geometry_raw)
    atomic_json(receipt_path, {"source_sha256": sha256(raw), "bytes": len(raw), "dataset_version": DATASET_VERSION,
                              "native_member": record["native_member"], "source_url": download_url(record["sequence"], record["frame"])})
    # Keep clean groups scene-specific. Held combinations are diagnostic cohorts
    # across scenes, never eligible; do not demand hundreds of tiny blocked audits.
    risk = "+".join(reasons) if reasons else "native_geometry_clean"
    group_key = (f"{record['split']}/qc-held/{risk}" if reasons
                 else f"{record['split']}/{record['sequence']}/{risk}")
    return {"image_id": identifier, "source": SOURCE, "split": record["split"],
            "session_id": record["session_id"], "group_key": group_key,
            "image": image_rel, "geometry": geometry_rel, "source_label": native_rel,
            "source_sha256": sha256(raw), "geometry_sha256": sha256(geometry_raw), "source_label_sha256": sha256(native_raw),
            "qc_reasons": reasons, "annotation_policy_risks": list(ANNOTATION_POLICY_RISKS),
            "pool": "commercial", "synthetic": False, "sequence": record["sequence"], "frame": record["frame"],
            "native_member": record["native_member"], "dataset_version": DATASET_VERSION,
            "converter_version": CONVERTER_VERSION, "label_origin": "pandaset_plus_native_candidate"}


def make_audits(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row["group_key"]].append(row)
    result = []
    for key, values in sorted(groups.items()):
        ordered = sorted(values, key=lambda row: (row["sequence"], row["frame"]))
        indices = sorted({0, (len(ordered) - 1) // 2, len(ordered) - 1})
        if len({r["split"] for r in ordered}) != 1:
            raise ValueError("An audit group cannot mix official data splits")
        result.append({"group_key": key, "split": ordered[0]["split"], "count": len(ordered),
                       "sample_ids": [ordered[i]["image_id"] for i in indices], "qc_reasons": ordered[0]["qc_reasons"]})
    return {"schema": "real_lane_audits:v1", "source": SOURCE, "groups": result,
            "policy": "Up to three spread samples per clean sequence; held cohorts grouped by split and exact QC combination for diagnostics, never release. All at once.",
            "annotation_warning": "Native lane candidates can include curbs, unpainted or hidden geometry. Approve only visible painted longitudinal boundaries.",
            "eligible_train_images": 0}


@contextmanager
def import_lock(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    stream = (output / "import.lock").open("a+b")
    stream.seek(0)
    stream.write(b"0")
    stream.flush()
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise RuntimeError("An isolated PandaSet+ import is already running") from None
    try:
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def run_import(output, *, records=None, workers=4, fetcher=None, limit=None):
    output = Path(output)
    with import_lock(output):
        if records is None:
            upstream = ensure_upstream(output)
            records = source_records(upstream)
        full_count = len(records)
        if limit is not None:
            records = records[:limit]
        rows, errors = [], []
        stats = {"schema": SCHEMA, "source": SOURCE, "state": "importing", "license": "CC-BY-4.0",
                 "authors_commit": COMMIT, "dataset_version": DATASET_VERSION, "converter_version": CONVERTER_VERSION,
                 "converter_sha256": sha256(Path(__file__).read_bytes()),
                 "annotation_policy_risks": list(ANNOTATION_POLICY_RISKS), "total_paired_images": full_count,
                 "target_images": len(records), "completed_images": 0, "eligible_train_images": 0,
                 "requires_human_audit": True, "auto_promote": False, "pool": "commercial", "synthetic": False,
                 "base_additional_terms": "upstream/PandaSet-LICENSE.txt", "started_at": now(), "pid": os.getpid()}
        atomic_json(output / "import.json", stats)
        archive = FrontImageArchive(records) if fetcher is None else None
        downloader = archive if archive is not None else fetcher
        try:
            with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as executor:
                futures = {executor.submit(convert_record, output, row, fetcher=downloader): row for row in records}
                for future in as_completed(futures):
                    record = futures[future]
                    try:
                        rows.append(future.result())
                    except Exception as error:
                        errors.append({"image_id": record["image_id"], "native_member": record["native_member"],
                                       "error_type": type(error).__name__})
                    if (len(rows) + len(errors)) % 25 == 0 or len(rows) + len(errors) == len(records):
                        stats.update(completed_images=len(rows), error_count=len(errors), updated_at=now())
                        if archive is not None:
                            stats.update(archive.stats())
                        atomic_json(output / "import_status.json", stats)
                        print(json.dumps({"completed": len(rows), "target": len(records), "errors": len(errors)}), flush=True)
            rows.sort(key=lambda row: (row["sequence"], row["frame"]))
            manifest_raw, audits_raw = b"".join(json_bytes(row) for row in rows), json_bytes(make_audits(rows))
            atomic_bytes(output / "manifest.jsonl", manifest_raw)
            atomic_bytes(output / "audits.json", audits_raw)
            atomic_json(output / "errors.json", errors)
            stats.update(state="awaiting_audit" if not errors and len(rows) == full_count else "partial_import",
                         completed_images=len(rows), error_count=len(errors), finished_at=now(),
                         manifest_sha256=sha256(manifest_raw), audits_sha256=sha256(audits_raw),
                         split_counts=dict(Counter(row["split"] for row in rows)),
                         qc_hold_images=sum(bool(row["qc_reasons"]) for row in rows),
                         audit_groups=len(json.loads(audits_raw)["groups"]),
                         audit_samples=sum(len(g["sample_ids"]) for g in json.loads(audits_raw)["groups"]))
            if archive is not None:
                stats.update(archive.stats())
            atomic_json(output / "import.json", stats)
            atomic_json(output / "import_status.json", stats)
            return stats
        finally:
            if archive is not None:
                archive.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("BrakeCheck_Dataset/experiments/pandaset_plus"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, help="Pilot only; resulting state remains partial_import")
    args = parser.parse_args()
    print(json.dumps(run_import(args.output, workers=args.workers, limit=args.limit), indent=2))


if __name__ == "__main__":
    main()
