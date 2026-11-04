"""Import only CARLANE's commercially licensed synthetic TuLane SOURCE data.

This importer never opens the production registry or launches a model. Native
simulator projections are retained verbatim and are NOT verified visible paint.
The completed dataset stays isolated and ineligible until separate human audits.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
import time
from urllib.parse import quote, unquote
import zipfile

import requests
from PIL import Image

from .http_range_zip import HTTPRangeReader, request

SCHEMA = "tulane_simulation_import:v1"
VERSION = 2
SOURCE = "carlane_tulane"
WIDTH, HEIGHT = 1280, 720
SLOTS = (2, 3, 1, 4)  # EGO_LEFT, EGO_RIGHT, OUTER_LEFT, OUTER_RIGHT.
SOURCE_URL = "https://carlanebenchmark.github.io/"
DOWNLOAD = "https://www.kaggle.com/api/v1/datasets/download/carlanebenchmark/carlane-benchmark/"
PINNED = {
    "train": ("source_train.json", 24000, "090e06029738a8b13fd7047e084c6b97b9bdd9b2e157b2cd1ebb0526105cfc76"),
    "val": ("source_val.json", 2400, "b6fc8b692b33a83cb80bc6b62ff1d0282cf8069b9122574913ba043f305d81d5"),
}
_FRAME = re.compile(r"[0-9]+_image\.jpg\Z")
_TOWN = re.compile(r"Town[0-9]+(?:_Opt)?\Z")
_HTTP = threading.local()


def now():
    return datetime.now(timezone.utc).isoformat()


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


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


def validate_raw_file(value, split):
    """Accept only version-2 synthetic source JPEGs; never target/TuSimple."""
    if not isinstance(value, str) or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("Invalid native image path")
    relative = PurePosixPath(value)
    parts = relative.parts
    if (relative.is_absolute() or ".." in parts or len(parts) != 5
            or value != relative.as_posix() or parts[0] != split or parts[1] != "source"
            or not _TOWN.fullmatch(parts[2]) or parts[3] not in {"straight", "left_curve", "right_curve"}
            or not _FRAME.fullmatch(parts[4])):
        raise ValueError("Only pinned TuLane train/source or val/source images are permitted")
    return value


def image_id(raw_file):
    return "tulane-" + sha256(raw_file.encode("utf-8"))[:16]


def native_geometry(native, split):
    raw_file = validate_raw_file(native.get("raw_file"), split)
    ys, lanes = native.get("h_samples"), native.get("lanes")
    if not isinstance(ys, list) or ys != list(range(160, 720, 10)):
        raise ValueError("Native h_samples differ from the pinned TuLane sample grid")
    if not isinstance(lanes, list) or len(lanes) != 4:
        raise ValueError("Pinned TuLane source requires exactly four native lane arrays")
    result, reasons = [], set()
    native_values = {}
    for slot, xs in zip(SLOTS, lanes):
        if not isinstance(xs, list) or len(xs) != len(ys):
            raise ValueError("Native lane arrays must match h_samples")
        if any(type(x) is not int or (x != -2 and not 0 <= x <= WIDTH) for x in xs):
            raise ValueError("Unexpected native lane coordinate or missing-value sentinel")
        if WIDTH in xs:
            reasons.add("native_point_at_right_image_edge")
        valid_indices = [index for index, x in enumerate(xs) if 0 <= x < WIDTH]
        missing_runs = []
        if valid_indices:
            start, end = min(valid_indices), max(valid_indices)
            absent = [index for index in range(start, end + 1) if xs[index] == -2]
            if absent:
                reasons.add("native_interior_missing_samples")
                missing_runs = [ys[index] for index in absent]
        points = [[xs[index], ys[index]] for index in valid_indices]
        if not points:
            continue
        if len(points) < 2:
            reasons.add("native_lane_has_fewer_than_two_points")
            # Keep the exact native sample in source_label, but do not emit an
            # invalid drawable line that would prevent even exclude/skip review.
            continue
        segments, segment = [], []
        for x, y in zip(xs, ys):
            if 0 <= x < WIDTH:
                segment.append([x, y])
            elif segment:
                segments.append(segment)
                segment = []
        if segment:
            segments.append(segment)
        native_values[slot] = dict((y, x) for x, y in points)
        result.append({"slot": slot, "points": points, "polyline_points": points,
                       "segments": segments, "missing_sample_ys": missing_runs,
                       "fit": {"basis": "interp", "ys": [p[1] for p in points], "xs": [p[0] for p in points]},
                       "y_top": points[0][1], "y_bottom": points[-1][1], "physical": True,
                       "origin": "simulator", "kind": "lane_marking"})
    if not result:
        reasons.add("native_no_projected_lane_geometry")
    for y in ys:
        ordered = [native_values[slot][y] for slot in (1, 2, 3, 4)
                   if slot in native_values and y in native_values[slot]]
        if any(left >= right for left, right in zip(ordered, ordered[1:])):
            reasons.add("native_lane_order_crossing")
    geometry = {"image_id": image_id(raw_file), "width": WIDTH, "height": HEIGHT,
                "version": "tulane-source-v1", "origin": "simulator",
                "annotation_policy": "projected_simulator_geometry_not_verified_visible_paint",
                "native_lane_slot_order": list(SLOTS), "lanes": sorted(result, key=lambda row: row["slot"])}
    return geometry, sorted(reasons)


def source_records(upstream, *, expected=PINNED):
    records, seen = [], set()
    for split in ("train", "val"):
        filename, count, expected_sha = expected[split]
        raw = (Path(upstream) / filename).read_bytes()
        if sha256(raw) != expected_sha:
            raise ValueError(f"Pinned source annotation SHA256 differs: {filename}")
        lines = raw.splitlines(keepends=True)
        if len(lines) != count:
            raise ValueError(f"Pinned annotation count differs: {filename}")
        for line in lines:
            native = json.loads(line)
            if not isinstance(native, dict):
                raise ValueError("Native label must be a JSON object")
            geometry, reasons = native_geometry(native, split)
            identifier = geometry["image_id"]
            if identifier in seen:
                raise ValueError("Duplicate synthetic image ID")
            seen.add(identifier)
            raw_file = native["raw_file"]
            town, curve = PurePosixPath(raw_file).parts[2:4]
            lane_count = len(geometry["lanes"])
            risk = "+".join(reasons) if reasons else "native_geometry_clean"
            records.append({"image_id": identifier, "raw_file": raw_file, "split": split,
                            "session_id": f"tulane/{split}/{town}/{curve}",
                            "group_key": f"{split}/{town}/{curve}/lanes-{lane_count}/{risk}",
                            "geometry": geometry, "qc_reasons": reasons,
                            "native_line": line, "source_file": filename})
    return records


def download_url(raw_file):
    return DOWNLOAD + quote("CARLANE/TuLane/data/" + raw_file, safe="") + f"?datasetVersionNumber={VERSION}"


def fetch(url, *, attempts=7, session=None):
    """Bounded single-file fallback with a process-wide Retry-After cooldown."""
    if session is None:
        if not hasattr(_HTTP, "session"):
            _HTTP.session = requests.Session()
        session = _HTTP.session
    response = request(session, url, attempts=attempts,
                       headers={"User-Agent": "BrakeCheck-TuLane-Import/2.0"})
    try:
        raw = response.raw.read(80 * 1024 * 1024 + 1)
        if len(raw) > 80 * 1024 * 1024:
            raise ValueError("Pinned single-file download unexpectedly exceeds 80 MB")
        return raw
    finally:
        response.close()


class SyntheticSourceArchive:
    """Reuse one pinned official archive, reading only authorized SOURCE members.

    This never extracts the archive to disk or opens target/MoLane/MuLane images.
    The normal storage server's HTTP range transport avoids tens of thousands of
    rate-limited Kaggle download API requests. JPEG verification, native label
    SHA256 pins, existing receipts and the immutable import plan remain unchanged.
    """
    transport = "official_v2_zip_ranges"

    def __init__(self, output, *, reader_factory=HTTPRangeReader):
        self.output = Path(output)
        self._reader_factory = reader_factory
        self._lock = threading.Lock()
        self._archive = self._reader = None
        self._error = None
        self._allowed = set()

    def __call__(self, url):
        if not url.startswith(DOWNLOAD):
            return fetch(url)
        encoded = url[len(DOWNLOAD):]
        suffix = f"?datasetVersionNumber={VERSION}"
        if not encoded.endswith(suffix):
            raise ValueError("Archive fetch requires the pinned version")
        member = unquote(encoded[:-len(suffix)])
        prefix = "CARLANE/TuLane/data/"
        if not member.startswith(prefix):
            # Native annotations are normally cached before opening this archive.
            return fetch(url)
        raw_file = member[len(prefix):]
        split = raw_file.split("/", 1)[0]
        if split not in PINNED:
            raise ValueError("Archive fetch permits only synthetic train/val source")
        validate_raw_file(raw_file, split)
        if download_url(raw_file) != url:
            raise ValueError("Archive member URL does not match its pinned encoding")
        with self._lock:
            if self._error is not None:
                raise RuntimeError(self._error)
            if self._archive is None:
                try:
                    records = source_records(self.output / "upstream")
                    self._allowed = {prefix + row["raw_file"] for row in records}
                    self._reader = self._reader_factory(DOWNLOAD.rstrip("/") + f"?datasetVersionNumber={VERSION}")
                    self._archive = zipfile.ZipFile(self._reader)
                    names = self._archive.namelist()
                    if len(names) != len(set(names)):
                        raise ValueError("Official archive has ambiguous duplicate member names")
                    for name in self._allowed:
                        info = self._archive.getinfo(name)
                        if info.is_dir() or info.flag_bits & 1 or not 0 < info.file_size <= 12 * 1024 * 1024:
                            raise ValueError("Pinned synthetic archive member has invalid size or encryption")
                except Exception as error:
                    self._error = f"Official synthetic archive could not be opened: {error}"
                    self.close()
                    raise RuntimeError(self._error) from None
            if member not in self._allowed:
                raise ValueError("Image is absent from the pinned synthetic annotation files")
            # ZipFile verifies the member CRC. ensure_image also fully decodes the
            # original JPEG and preserves/checks each already-downloaded receipt.
            return self._archive.read(member)

    def close(self):
        if self._archive is not None:
            self._archive.close()
            self._archive = None
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    def transport_stats(self):
        reader = self._reader
        return {"archive_bytes_downloaded": getattr(reader, "bytes_downloaded", 0),
                "archive_range_requests": getattr(reader, "range_requests", 0)}


def verify_jpeg(raw):
    if len(raw) > 12 * 1024 * 1024:
        raise ValueError("Synthetic JPEG unexpectedly exceeds 12 MB")
    with Image.open(io.BytesIO(raw)) as image:
        if image.format != "JPEG" or image.size != (WIDTH, HEIGHT):
            raise ValueError("Expected an unchanged 1280 x 720 TuLane JPEG")
        # JPEG verify() alone checks headers without decoding scan data.
        # Decode fully so truncated transfers never become accepted receipts.
        image.load()


def ensure_image(output, record, *, fetcher=fetch):
    image_rel = "images/" + record["raw_file"]
    image_path = output / image_rel
    receipt_path = output / "receipts" / (record["image_id"] + ".json")
    raw = None
    if image_path.is_file():
        try:
            existing = image_path.read_bytes()
            verify_jpeg(existing)
            digest = sha256(existing)
            if receipt_path.is_file():
                receipt = json.loads(receipt_path.read_bytes())
                if (receipt.get("source_sha256") != digest or receipt.get("raw_file") != record["raw_file"]
                        or receipt.get("dataset_version") != VERSION):
                    raise ValueError("Downloaded image receipt does not match existing bytes")
            raw = existing
        except (OSError, ValueError, SyntaxError):
            raw = None
    if raw is None:
        if shutil.disk_usage(output).free < 512 * 1024 * 1024:
            raise OSError("Less than 512 MB free; pause the isolated import before the disk fills")
        raw = fetcher(download_url(record["raw_file"]))
        verify_jpeg(raw)
        atomic_bytes(image_path, raw)
    digest = sha256(raw)
    receipt = {"raw_file": record["raw_file"], "dataset_version": VERSION,
               "download_url": download_url(record["raw_file"]), "source_sha256": digest, "bytes": len(raw)}
    atomic_json(receipt_path, receipt)
    return image_rel, digest


def convert_record(output, record, *, fetcher=fetch):
    output = Path(output)
    image_rel, digest = ensure_image(output, record, fetcher=fetcher)
    geometry_rel = "geometry/" + record["image_id"] + ".json"
    label_rel = "labels/records/" + record["image_id"] + ".json"
    geometry_raw = json_bytes(record["geometry"])
    atomic_bytes(output / geometry_rel, geometry_raw)
    atomic_bytes(output / label_rel, record["native_line"])
    return {"image_id": record["image_id"], "raw_file": record["raw_file"], "split": record["split"],
            "session_id": record["session_id"], "source": SOURCE, "synthetic": True, "pool": "commercial",
            "image": image_rel, "geometry": geometry_rel, "source_label": label_rel,
            "source_sha256": digest, "geometry_sha256": sha256(geometry_raw),
            "source_label_sha256": sha256(record["native_line"]), "group_key": record["group_key"],
            "qc_reasons": record["qc_reasons"], "annotation_policy_risks": ["projected_simulator_geometry"],
            "label_origin": "simulator_projected_gt", "dataset_version": VERSION}


def make_audits(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row["group_key"]].append(row)
    audits = []
    for key, group in sorted(groups.items()):
        ordered = sorted(group, key=lambda row: row["raw_file"])
        # Spread examples across recording time, rather than adjacent frames.
        indices = sorted({0, (len(ordered) - 1) // 2, len(ordered) - 1})
        audits.append({"group_key": key, "sample_ids": [ordered[index]["image_id"] for index in indices],
                       "count": len(group), "split": ordered[0]["split"],
                       "qc_reasons": ordered[0]["qc_reasons"], "state": "pending"})
    return {"schema": "tulane_simulation_audits:v1", "groups": audits,
            "created_at": now(), "policy": "Three spread examples per group; all audit groups generated at once",
            "annotation_warning": "Simulator road projections can include hidden, unpainted or road-edge geometry; check visible paint policy.",
            "eligible_train_images": 0}


@contextmanager
def import_lock(output):
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
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        stream.close()
        raise RuntimeError("Another TuLane importer is already running") from error
    try:
        yield
    finally:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


def ensure_notices(output, *, fetcher=fetch):
    notices = output / "provenance"
    license_path = notices / "LICENSE-Apache-2.0.txt"
    if not license_path.is_file():
        license_raw = fetcher("https://www.apache.org/licenses/LICENSE-2.0.txt")
        if b"Apache License" not in license_raw or b"END OF TERMS AND CONDITIONS" not in license_raw:
            raise ValueError("Canonical Apache license text did not validate")
        atomic_bytes(license_path, license_raw)
    proof_path = notices / "publisher-page.html"
    if not proof_path.is_file():
        proof = fetcher(SOURCE_URL)
        if b"Apache" not in proof or b"commercial" not in proof.lower():
            raise ValueError("Publisher page did not confirm commercial data license")
        atomic_bytes(proof_path, proof)
    notice = ("TuLane synthetic SOURCE subset, CARLANE Benchmark, dataset version 2.\n"
              "Copyright 2022 CARLANE Team. Licensed by the publisher under Apache License 2.0.\n"
              "Publisher: https://carlanebenchmark.github.io/\n"
              "Distribution: https://www.kaggle.com/datasets/carlanebenchmark/carlane-benchmark\n"
              "Only CARLA-rendered train/source and val/source images are included.\n"
              "TuSimple target images and all other CARLANE domains are excluded.\n"
              "JPEGs and source JSONL records remain unchanged. Derived geometry maps native lane order\n"
              "to UFLDv2 slots [2,3,1,4], omits out-of-image samples, and reports native annotation risks.\n"
              "Projected simulator road geometry is not a guarantee of visible paint. Human audits are required.\n")
    atomic_bytes(notices / "NOTICE.txt", notice.encode("utf-8"))
    atomic_json(notices / "provenance.json", {"source": SOURCE, "source_url": SOURCE_URL,
                "dataset_version": VERSION, "license": "Apache-2.0", "domain": "synthetic",
                "copyright": "Copyright 2022 CARLANE Team", "license_sha256": sha256(license_path.read_bytes()),
                "publisher_page_sha256": sha256(proof_path.read_bytes()), "download_scope": ["train/source", "val/source"],
                "native_annotation_kind": "simulator_projected_gt", "retrieved_at": now()})


def ensure_upstream(output, *, expected=PINNED, fetcher=fetch):
    for _, (filename, _, expected_sha) in expected.items():
        path = output / "upstream" / filename
        if path.is_file() and sha256(path.read_bytes()) == expected_sha:
            continue
        url = DOWNLOAD + quote("CARLANE/TuLane/splits/" + filename, safe="") + f"?datasetVersionNumber={VERSION}"
        raw = fetcher(url)
        if raw.startswith(b"PK"):
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                matches = [info for info in archive.infolist() if not info.is_dir()]
                if len(matches) != 1 or PurePosixPath(matches[0].filename).name != filename:
                    raise ValueError("Native annotation archive must contain exactly its expected JSONL file")
                raw = archive.read(matches[0])
        if sha256(raw) != expected_sha:
            raise ValueError("Downloaded native annotations differ from pinned version-2 SHA256")
        atomic_bytes(path, raw)


def run_import(output, *, workers=8, limit=None, expected=PINNED, fetcher=fetch, with_notices=True):
    output = Path(output).resolve()
    started = now()
    if not 1 <= workers <= 24:
        raise ValueError("Use 1 to 24 download workers")
    with import_lock(output):
        report = {"schema": SCHEMA, "source": SOURCE, "domain": "synthetic", "license": "Apache-2.0",
                  "dataset_version": VERSION, "state": "starting", "pid": os.getpid(), "started_at": started,
                  "updated_at": started, "downloaded": 0, "converted": 0, "train_images": 0, "val_images": 0,
                  "audit_samples": 0, "audits_remaining": 0, "eligible_train_images": 0,
                  "total_expected": sum(value[1] for value in expected.values()), "errors": [], "workers": workers,
                  "isolated": True, "auto_promote": False, "training_started": False,
                  "annotation_policy_risks": ["projected_simulator_geometry"]}
        report["transport"] = getattr(fetcher, "transport", "single_files")
        old_path = output / "import.json"
        if old_path.is_file():
            old = json.loads(old_path.read_bytes())
            if old.get("state") in {"awaiting_audit", "ready"} and (output / "manifest.jsonl").is_file() and (output / "audits.json").is_file():
                old_plan = json.loads((output / "plan.json").read_bytes())
                expected_sources = {split: {"filename": row[0], "count": row[1], "sha256": row[2]} for split, row in expected.items()}
                if (old_plan.get("source_annotations") != expected_sources or old_plan.get("pilot") != (limit is not None)
                        or old_plan.get("total_expected") != (limit if limit is not None else report["total_expected"])):
                    raise ValueError("Completed import plan differs; use another isolated output directory")
                if old.get("manifest_sha256") != sha256((output / "manifest.jsonl").read_bytes()):
                    raise ValueError("Completed manifest changed; refusing to overwrite it")
                if old.get("audits_sha256") != sha256((output / "audits.json").read_bytes()):
                    raise ValueError("Completed audit plan changed; refusing to overwrite it")
                return old
        atomic_json(old_path, report)
        try:
            if with_notices:
                ensure_notices(output, fetcher=fetcher)
            ensure_upstream(output, expected=expected, fetcher=fetcher)
            records = source_records(output / "upstream", expected=expected)
            if limit is not None:
                if limit < 1:
                    raise ValueError("Pilot limit must be positive")
                records = records[:limit]
                report["total_expected"] = len(records)
                report["pilot"] = True
            if shutil.disk_usage(output).free < 2 * 1024 ** 3 + len(records) * 384 * 1024:
                raise OSError("Insufficient free space for this isolated import; allow 2 GB plus roughly 384 KB per image")
            plan = {"schema": "tulane_simulation_plan:v1", "dataset_version": VERSION,
                    "source": SOURCE, "source_annotations": {split: {"filename": row[0], "count": row[1], "sha256": row[2]}
                                                              for split, row in expected.items()},
                    "image_ids_sha256": sha256(json_bytes([row["image_id"] for row in records])),
                    "total_expected": len(records), "lane_slot_mapping": list(SLOTS), "pilot": limit is not None}
            plan_path = output / "plan.json"
            if plan_path.is_file() and json.loads(plan_path.read_bytes()) != plan:
                raise ValueError("Existing import plan differs; use another isolated output directory")
            if not plan_path.is_file():
                atomic_json(plan_path, plan)
            report["state"] = "downloading"
            report["phase"] = "download_and_convert"
            atomic_json(old_path, report)
            completed = {}
            last_report = 0.0
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tulane") as pool:
                futures = {pool.submit(convert_record, output, row, fetcher=fetcher): row for row in records}
                for future in as_completed(futures):
                    row = futures[future]
                    try:
                        converted = future.result()
                        completed[row["image_id"]] = converted
                        report["downloaded"] += 1
                        report["converted"] += 1
                        report[row["split"] + "_images"] += 1
                    except Exception as error:
                        if len(report["errors"]) < 30:
                            report["errors"].append({"image_id": row["image_id"], "error": str(error)[:600]})
                        report["failed"] = report.get("failed", 0) + 1
                    if time.monotonic() - last_report >= 3 or report["converted"] == len(records):
                        if hasattr(fetcher, "transport_stats"):
                            report.update(fetcher.transport_stats())
                        report["updated_at"] = now()
                        report["images_per_second"] = round(report["converted"] / max(.001, (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds()), 3)
                        atomic_json(old_path, report)
                        last_report = time.monotonic()
            if len(completed) != len(records):
                raise RuntimeError(f"{len(records) - len(completed)} images failed; rerun to resume verified completed files")
            report["state"] = "converting"
            report["phase"] = "freezing_manifest_and_all_audits"
            atomic_json(old_path, report)
            rows = [completed[row["image_id"]] for row in records]
            manifest_raw = b"".join(json_bytes(row) for row in rows)
            audits = make_audits(rows)
            audit_raw = json_bytes(audits)
            # No partial manifests are exposed to reviewers. These freeze together
            # before import.json advertises the final awaiting_audit state.
            atomic_bytes(output / "manifest.jsonl", manifest_raw)
            atomic_bytes(output / "audits.json", audit_raw)
            report.update({"state": "awaiting_audit", "phase": "awaiting_all_conversion_audits",
                           "manifest_sha256": sha256(manifest_raw), "audits_sha256": sha256(audit_raw),
                           "audit_groups": len(audits["groups"]), "audit_samples": sum(len(group["sample_ids"]) for group in audits["groups"]),
                           "audits_remaining": sum(len(group["sample_ids"]) for group in audits["groups"]),
                           "qc_flagged_images": sum(bool(row["qc_reasons"]) for row in rows),
                           "completed_at": now(), "updated_at": now(), "errors": []})
            atomic_json(old_path, report)
            return report
        except BaseException as error:
            report.update({"state": "error", "last_error": str(error)[:1000], "updated_at": now()})
            atomic_json(old_path, report)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", type=Path, help="Separate isolated output directory beneath data-root/experiments")
    parser.add_argument("--limit", type=int, help="Pilot only; requires --output pointing to a separate isolated directory")
    parser.add_argument("--transport", choices=("archive", "files"), default="archive",
                        help="Default: one official pinned ZIP with selective HTTP range reads")
    args = parser.parse_args()
    data_root = args.data_root.resolve()
    experiments = data_root / "experiments"
    standard = experiments / "simulated_tulane"
    output = args.output.resolve() if args.output else standard
    if not output.is_relative_to(experiments.resolve()) or output == experiments.resolve():
        parser.error("Output must be an isolated child of data-root/experiments")
    if args.limit is not None and (args.output is None or output == standard.resolve()):
        parser.error("--limit requires a separate --output; never publish a partial full-dataset manifest")
    fetcher = SyntheticSourceArchive(output) if args.transport == "archive" else fetch
    try:
        result = run_import(output, workers=args.workers, limit=args.limit, fetcher=fetcher)
    finally:
        if isinstance(fetcher, SyntheticSourceArchive):
            fetcher.close()
    print(json.dumps({key: result[key] for key in ("state", "downloaded", "converted", "train_images", "val_images", "audit_samples", "eligible_train_images")}))


if __name__ == "__main__":
    main()
