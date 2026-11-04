"""Import the official CC0 College Station lane datasets into an isolated audit pool.

Only paired, front camera JPEG + native SCNN polylines are fetched. HTTP ranges
read the publisher's split ZIPs without downloading the much larger raw videos.
No registry, human decision, original conversion, model, or training data is changed.
"""
from __future__ import annotations

import argparse
from collections import Counter, OrderedDict, defaultdict
from contextlib import contextmanager
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import time
import zipfile

import requests
from PIL import Image

SOURCE = "college_station_lane_marking"
DOI = "doi:10.15787/VTT1/5FGGKD"
API = "https://dataverse.vtti.vt.edu/api"
DATASET_URL = "https://dataverse.vtti.vt.edu/dataset.xhtml?persistentId=" + DOI
METADATA_URL = API + "/datasets/:persistentId/?persistentId=" + DOI
CONVERTER = "college-station-native-lines-v2"
ARCHIVES = ("CollegeStation_dataset", "RELLIS_3M_Panel_Dataset", "US290")
COVERAGE_POLICY = {
    "id": "us290-west-w_b-partial-native-v1",
    "source_member_prefix": "US290/US290_dataset/west/w_b/",
    "reason": "native_coverage_incomplete",
    "evidence": "reports/coverage_us290/west-w_b-1.jpg",
    "finding": "Beginning/middle/end native labels omit visible interior paint or use an outer boundary as ego boundary; preserve source and hold the material subset",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def atomic_json(path: Path, value) -> None:
    atomic_bytes(path, json_bytes(value))


class OfficialHTTP:
    def __init__(self, session=None, *, attempts=12, sleep=time.sleep):
        self.session = session or requests.Session()
        self.attempts = attempts
        self.sleep = sleep
        self.network_bytes = 0

    def get(self, url: str, *, start=None, end=None) -> bytes:
        headers = {}
        if start is not None:
            headers["Range"] = f"bytes={start}-{end}"
        for attempt in range(self.attempts):
            try:
                response = self.session.get(url, headers=headers, timeout=(15, 90), stream=True)
                try:
                    if response.status_code in (429, 500, 502, 503, 504):
                        if attempt + 1 == self.attempts:
                            response.raise_for_status()
                        retry = response.headers.get("Retry-After", "")
                        delay = min(600, float(retry)) if retry.isdigit() else (
                            min(300, 60 * 2 ** attempt) if response.status_code == 429 else min(30, 2 ** attempt))
                        print(json.dumps({"event": "publisher_retry", "http_status": response.status_code,
                                          "delay_seconds": delay}), flush=True)
                        self.sleep(delay)
                        continue
                    response.raise_for_status()
                    if start is not None:
                        expected = f"bytes {start}-{end}/"
                        if response.status_code != 206 or not response.headers.get("Content-Range", "").startswith(expected):
                            raise ValueError("Publisher did not honor the exact byte range; refusing whole archive")
                        maximum = end - start + 1
                    else:
                        maximum = 8 * 1024 * 1024  # Metadata/specification only, never a bulk archive.
                    chunks = []
                    count = 0
                    for chunk in response.iter_content(256 * 1024):
                        count += len(chunk)
                        if count > maximum:
                            raise ValueError("Publisher response exceeded the requested size")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    if start is not None and len(data) != maximum:
                        raise IOError("Truncated ranged response")
                    self.network_bytes += len(data)
                    return data
                finally:
                    response.close()
            except (requests.RequestException, IOError):
                if attempt + 1 == self.attempts:
                    raise
                self.sleep(min(30, 2 ** attempt))
        raise RuntimeError("Unreachable HTTP retry state")


class RangedSplitZIP(io.RawIOBase):
    """Seekable virtual concatenation of publisher ZIP.001/.002/... files.

    A small bounded memory cache coalesces neighboring JPEG/label reads. ZIP's
    CRC checks validate every decompressed entry; whole-part MD5 is deliberately
    not claimed because raw video bytes are never downloaded.
    """
    def __init__(self, parts, http, *, block_size=4 * 1024 * 1024, cache_blocks=4):
        super().__init__()
        self.parts = sorted(parts, key=lambda part: part["filename"])
        if not self.parts:
            raise ValueError("Split ZIP has no parts")
        numbers = [int(part["filename"].rsplit(".", 1)[-1]) for part in self.parts]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("Split ZIP parts must be complete and ordered from .001")
        self.starts = []
        self.size = 0
        for part in self.parts:
            self.starts.append(self.size)
            self.size += part["filesize"]
        self.http = http
        self.block_size = block_size
        self.cache_blocks = cache_blocks
        self.cache = OrderedDict()
        self.position = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        position = offset if whence == 0 else self.position + offset if whence == 1 else self.size + offset
        if whence not in (0, 1, 2) or position < 0:
            raise ValueError("Invalid ZIP seek")
        self.position = position
        return position

    def _block(self, number):
        if number in self.cache:
            self.cache.move_to_end(number)
            return self.cache[number]
        first = number * self.block_size
        last = min(self.size, first + self.block_size)
        chunks = []
        for part, start in zip(self.parts, self.starts):
            if first >= start + part["filesize"] or last <= start:
                continue
            a, b = max(first, start) - start, min(last, start + part["filesize"]) - start - 1
            chunks.append(self.http.get(API + "/access/datafile/" + str(part["id"]), start=a, end=b))
        value = b"".join(chunks)
        if len(value) != last - first:
            raise IOError("Split ZIP range did not cover requested bytes")
        self.cache[number] = value
        while len(self.cache) > self.cache_blocks:
            self.cache.popitem(last=False)
        return value

    def read(self, size=-1):
        if size is None or size < 0:
            size = self.size - self.position
        end = min(self.size, self.position + size)
        chunks = []
        while self.position < end:
            number, offset = divmod(self.position, self.block_size)
            block = self._block(number)
            count = min(end - self.position, len(block) - offset)
            if count <= 0:
                raise IOError("ZIP range reader made no progress")
            chunks.append(block[offset:offset + count])
            self.position += count
        return b"".join(chunks)


def source_identity(archive: str, name: str):
    """Keep the same physical road/site in one fold across lighting conditions."""
    parts = PurePosixPath(name).parts
    if archive == "CollegeStation_dataset":
        start = parts.index("CollegeStation_dataset_annotated") + 1
        lighting, road = parts[start:start + 2]
        road = road.lower().replace("maia", "maria")
        session = "college-" + road
        sequence = session + ":" + lighting
        bucket = int(hashlib.sha256(session.encode()).hexdigest()[:8], 16) % 100
        split = "train" if bucket < 70 else "val" if bucket < 85 else "test"
    elif archive == "RELLIS_3M_Panel_Dataset":
        start = parts.index("3M_Panels_dataset") + 1
        session = "rellis-3m-closed-course"
        sequence = "rellis:" + ":".join(parts[start:-1])
        split = "train"
    elif archive == "US290":
        start = parts.index("US290_dataset") + 1
        session = "us290-highway"
        sequence = "us290:" + ":".join(parts[start:-1])
        split = "train"
    else:
        raise ValueError("Unknown archive or non-front source")
    return session, sequence, split


def parse_lines(data: bytes, width: int, height: int):
    """Native SCNN xy pairs -> unchanged observed polylines; never extend paint.

    Native SCNN has no marking type, ego identity, or visibility attribute. Lane
    slots are a derived ordering hint only; ambiguous geometry is held for audit.
    """
    lanes, reasons = [], set()
    for line_number, text in enumerate(data.decode("utf-8-sig").splitlines(), 1):
        if not text.strip():
            continue
        try:
            values = [float(value) for value in text.split()]
        except ValueError:
            reasons.add("invalid_native_number")
            continue
        if len(values) < 4 or len(values) % 2 or not all(math.isfinite(value) for value in values):
            reasons.add("invalid_native_polyline")
            continue
        points = list(dict.fromkeys(zip(values[::2], values[1::2])))
        if len(points) < 2:
            reasons.add("degenerate_native_polyline")
            continue
        ordered = sorted(points, key=lambda p: (p[1], p[0]))
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        dy, dx = max(ys) - min(ys), max(xs) - min(xs)
        flags = []
        if len({point[1] for point in ordered}) != len(ordered):
            flags.append("native_polyline_not_single_valued")
        if any(x < 0 or x >= width or y < 0 or y >= height for x, y in points):
            flags.append("native_points_outside_image")
        if dy < max(12, height * .035):
            flags.append("short_native_polyline")
        if dx > 6 * max(1, dy):
            flags.append("potential_transverse_marking")
        reference_y = .95 * (height - 1)
        if reference_y <= ordered[-1][1]:
            # Interpolation only within native support.
            ref = ordered[-1][0]
            for a, b in zip(ordered, ordered[1:]):
                if a[1] <= reference_y <= b[1] and b[1] != a[1]:
                    ref = a[0] + (b[0] - a[0]) * (reference_y - a[1]) / (b[1] - a[1])
                    break
        else:
            a, b = ordered[-2:]
            ref = b[0] if b[1] == a[1] else b[0] + (b[0] - a[0]) * (reference_y - b[1]) / (b[1] - a[1])
            if reference_y - b[1] > .35 * height:
                flags.append("ego_slot_long_extrapolation")
        if abs(ref - .5 * width) < .035 * width:
            flags.append("ego_slot_center_ambiguous")
        reasons.update(flags)
        lanes.append({"id": len(lanes), "source_line": line_number, "slot": None,
                      "kind": "unknown", "physical": True, "visibility": "unknown",
                      "physical_evidence": "publisher_annotation_only_unverified",
                      "visible_paint_verified": False,
                      "points": [[x, y, True] for x, y in reversed(ordered)],
                      "fit": {"basis": "interp", "xs": [p[0] for p in ordered], "ys": [p[1] for p in ordered]},
                      "y_top": min(ys), "y_bottom": max(ys), "x_ref": ref,
                      "gaps": [], "flags": sorted(flags)})
    candidates = [lane for lane in lanes if not any(reason in lane["flags"] for reason in
                  ("native_points_outside_image", "potential_transverse_marking"))]
    left = sorted((lane for lane in candidates if lane["x_ref"] < .5 * width), key=lambda lane: -lane["x_ref"])
    right = sorted((lane for lane in candidates if lane["x_ref"] >= .5 * width), key=lambda lane: lane["x_ref"])
    for lane, slot in zip(left[:2], (2, 1)):
        lane["slot"] = slot
    for lane, slot in zip(right[:2], (3, 4)):
        lane["slot"] = slot
    drawable = [lane for lane in lanes if lane["slot"] in (1, 2, 3, 4)
                and "native_polyline_not_single_valued" not in lane["flags"]]
    geometry = {"schema": "native_lane_geometry:v1", "width": width, "height": height,
                "horizon_y": 0, "valid_bottom": height - 1, "lanes": drawable,
                "converter": CONVERTER, "coordinate_system": "original_image_pixels",
                "label_policy": "native_painted_longitudinal_polylines_no_road_edges_no_extension",
                "tags": {"lane_count": len(drawable),
                         "native_annotation_count": len(lanes), "no_lanes": not lanes and not reasons},
                "omitted_native_source_lines": [lane["source_line"] for lane in lanes if lane not in drawable],
                "source_annotation": {"format": "SCNN.lines.txt", "human_source": True,
                                      "marking_pattern": "not_provided", "ego_slots": "derived_not_source",
                                      "label_status": "source_only_awaiting_audit",
                                      "visible_paint_verified": False,
                                      "physical_semantics": "publisher_intends_a_physical_boundary; visible_paint_not_independently_verified"}}
    return geometry, sorted(reasons)


def paired_entries(zipped: zipfile.ZipFile, archive: str):
    entries = {item.filename: item for item in zipped.infolist()}
    pairs = []
    for name, item in entries.items():
        if not name.lower().endswith(".jpg") or item.is_dir():
            continue
        label = name[:-4] + ".lines.txt"
        if label not in entries:
            continue
        source_identity(archive, name)  # Reject raw/non-front paths rather than guess.
        pairs.append((item, entries[label]))
    return sorted(pairs, key=lambda pair: pair[0].header_offset)


def image_record(archive: str, name: str, image: bytes, native: bytes, *, image_crc=None, label_crc=None):
    image_id = "college-" + sha256((archive + ":" + name).encode())[:16]
    with Image.open(io.BytesIO(image)) as picture:
        width, height = picture.size
        picture.load()  # Decode every pixel; header-only verify misses truncated JPEG scan data.
    geometry, reasons = parse_lines(native, width, height)
    geometry_data = json_bytes(geometry)
    session, sequence, split = source_identity(archive, name)
    risk = "+".join(reasons) if reasons else "clean"
    final_split = "hold" if reasons else split
    group = sequence + ":" + risk + ":lanes-" + str(geometry["tags"]["lane_count"])
    record = {"image_id": image_id, "source": SOURCE, "split": final_split, "intended_split": split,
              "session_id": session, "sequence_id": sequence, "group_key": group,
              "image": "images/" + image_id + ".jpg", "geometry": "geometry/" + image_id + ".json",
              "source_label": "native/" + image_id + ".lines.txt",
              "source_sha256": sha256(image), "geometry_sha256": sha256(geometry_data),
              "source_label_sha256": sha256(native), "qc_reasons": reasons,
              "pool": "commercial", "synthetic": False, "camera": "front",
              "license": "CC0-1.0", "converter": CONVERTER,
              "source_archive": archive, "source_member": name,
              "source_label_member": name[:-4] + ".lines.txt",
              "zip_image_crc32": image_crc, "zip_label_crc32": label_crc}
    return record, geometry_data


def temporal_key(row):
    return [int(part) if part.isdigit() else part
            for part in re.split(r"(\d+)", row.get("source_member", row["image_id"]).lower())]


def clean_audit_base(record):
    archive = record["source_archive"]
    parts = PurePosixPath(record["source_member"]).parts
    annotation = "native-empty" if record.get("source_label_sha256") == sha256(b"") else "native-positive"
    if archive == "CollegeStation_dataset":
        return record["sequence_id"] + ":" + annotation
    if archive == "RELLIS_3M_Panel_Dataset":
        start = parts.index("3M_Panels_dataset") + 1
        gap, lighting = parts[start:start + 2]
        beam = parts[start + 2] if lighting == "night" else "daylight"
        direction = re.search(r"(?:^|_)(nb|sb)(?:_|$)", parts[-1].lower())
        return "rellis:" + ":".join((gap, lighting, beam, direction.group(1) if direction else "unknown-direction", annotation))
    if archive == "US290":
        start = parts.index("US290_dataset") + 1
        return "us290:" + ":".join(parts[start:start + 2]) + ":" + annotation
    raise ValueError("Unknown source for audit grouping")


def apply_coverage_policy(records):
    result = []
    for original in records:
        row = dict(original)
        if row["source_member"].startswith(COVERAGE_POLICY["source_member_prefix"]):
            row["qc_reasons"] = sorted(set(row["qc_reasons"]) | {COVERAGE_POLICY["reason"]})
            row["split"] = "hold"
            row["coverage_policy"] = COVERAGE_POLICY["id"]
            row["coverage_evidence"] = COVERAGE_POLICY["evidence"]
        result.append(row)
    return result


def assign_audit_groups(records, *, clean_cohort_limit=250):
    """Bound clean audit cohorts; combine held cases by physical site + cause.

    A failed polyline does not create a separate audit for every frame count or
    small tape segment. All original per-image QC evidence remains unchanged.
    """
    priority = ("native_coverage_incomplete", "invalid_native_number", "invalid_native_polyline", "degenerate_native_polyline",
                "native_points_outside_image", "native_polyline_not_single_valued",
                "potential_transverse_marking", "ego_slot_center_ambiguous",
                "ego_slot_long_extrapolation", "short_native_polyline")
    clean = defaultdict(list)
    result = []
    for original in records:
        record = dict(original)
        if record["qc_reasons"]:
            cause = next((item for item in priority if item in record["qc_reasons"]), record["qc_reasons"][0])
            record["group_key"] = "held:" + record["session_id"] + ":" + cause
            result.append(record)
        else:
            clean[(record["split"], clean_audit_base(record))].append(record)
    for (split, base), rows in sorted(clean.items()):
        for index, record in enumerate(sorted(rows, key=temporal_key)):
            record["group_key"] = "clean:" + base + ":" + split + f":cohort-{index // clean_cohort_limit:03}"
            result.append(record)
    return result


def make_audits(records):
    grouped = defaultdict(list)
    for record in records:
        grouped[record["group_key"]].append(record)
    groups = []
    for key, rows in sorted(grouped.items()):
        ordered = sorted(rows, key=temporal_key)
        positions = [0, len(ordered) // 2, len(ordered) - 1] if len(ordered) >= 3 else list(range(len(ordered)))
        sample_ids = [ordered[position]["image_id"] for position in positions]
        splits = {row["split"] for row in rows}
        if len(splits) != 1:
            raise ValueError("Audit group crosses splits")
        groups.append({"group_key": key, "split": next(iter(splits)), "count": len(rows),
                       "sample_ids": sample_ids, "qc_reasons": sorted({reason for row in rows for reason in row["qc_reasons"]}),
                       "session_id": rows[0]["session_id"], "sequence_id": rows[0]["sequence_id"]})
    return {"schema": "real_lane_audits:v1", "source": SOURCE, "policy": CONVERTER,
            "groups": groups, "sample_count": sum(len(group["sample_ids"]) for group in groups)}


def verify_record(root: Path, record, *, require_converter=True):
    if require_converter and record.get("converter") != CONVERTER:
        return False
    for path_key, hash_key in (("image", "source_sha256"), ("geometry", "geometry_sha256"),
                               ("source_label", "source_label_sha256")):
        relative = PurePosixPath(record.get(path_key, ""))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            return False
        path = root.joinpath(*relative.parts)
        if not path.is_file() or sha256(path.read_bytes()) != record.get(hash_key):
            return False
    return True


@contextmanager
def import_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".import.lock"
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        handle.close()


def load_journal(root):
    path = root / "progress.jsonl"
    records = {}
    if path.exists():
        raw = path.read_bytes()
        lines = raw.splitlines()
        valid_lines = []
        for index, line in enumerate(lines):
            try:
                row = json.loads(line)
            except ValueError:
                if index != len(lines) - 1:
                    raise
                # Only an interrupted final journal write may be incomplete.
                continue
            records[row["source_member"]] = row
            valid_lines.append(line)
        repaired = b"\n".join(valid_lines) + (b"\n" if valid_lines else b"")
        if raw != repaired:
            atomic_bytes(path, repaired)
    return records


def run(root: Path, *, limit=None, archives=ARCHIVES, http=None):
    root = root.resolve()
    http = http or OfficialHTTP()
    with import_lock(root):
        atomic_json(root / "status.json", {"state": "metadata_pending", "processed": 0,
                                           "updated_at": time.time(), "pid": os.getpid(),
                                           "note": "Publisher 429 responses receive a 60–300 second cooldown; no training changes"})
        cache_path = root / "official_metadata.json"
        pinned_path = root / "pinned_inventory.json"
        metadata_kind = "exact_official_api_response"
        if cache_path.is_file():
            metadata_data = cache_path.read_bytes()
        elif pinned_path.is_file():
            metadata_data = pinned_path.read_bytes()
            metadata_kind = "observed_official_api_inventory_extract"
        else:
            metadata_data = http.get(METADATA_URL)
        document = json.loads(metadata_data)
        metadata = document["data"]["latestVersion"]
        license_data = metadata.get("license", {})
        if license_data.get("rightsIdentifier") != "CC0-1.0":
            raise ValueError("Official dataset license no longer matches verified CC0-1.0")
        if metadata_kind == "exact_official_api_response":
            atomic_bytes(root / "official_metadata.json", metadata_data)
        files = [entry["dataFile"] for entry in metadata["files"] if not entry.get("restricted")]
        specification = next(item for item in files if item["filename"] == "04-115_Data_Specification.pdf")
        spec_data = http.get(API + "/access/datafile/" + str(specification["id"]))
        if (len(spec_data) != specification["filesize"] or not spec_data.startswith(b"%PDF-")
                or b"%%EOF" not in spec_data[-1024:]):
            raise ValueError("Official specification is not a complete PDF of the observed size")
        atomic_bytes(root / "04-115_Data_Specification.pdf", spec_data)
        notice = ("College Station Lane Marking Datasets (Texas A&M / VTTI), CC0-1.0.\n"
                  "Nayak, Abhishek; Pike, Adam; Rathinam, Sivakumar; Gopalswamy, Swaminathan (2021).\n"
                  "DOI: https://doi.org/10.15787/VTT1/5FGGKD\n"
                  "License: https://creativecommons.org/publicdomain/zero/1.0/\n"
                  "Only paired front-camera JPEGs and publisher SCNN lane annotations are imported.\n"
                  "TX21 and 426 unpaired College Station JPEGs are excluded: no native lane labels.\n"
                  "Raw driving videos and model-rendered outputs are not imported.\n"
                  "Original pixels and native annotations are retained unchanged. Derived lane slots require audit.\n")
        atomic_bytes(root / "NOTICE.txt", notice.encode())
        prior = load_journal(root)
        imported = []
        inventory, expected, skipped = {}, 0, 0
        initial = {"schema": "real_lane_import:v1", "source": SOURCE, "state": "importing",
                   "license": "CC0-1.0", "pool": "commercial", "synthetic": False,
                   "official_url": DATASET_URL, "doi": DOI, "converter": CONVERTER,
                   "metadata_sha256": sha256(metadata_data), "specification_sha256": sha256(spec_data),
                   "metadata_document": "official_metadata.json" if metadata_kind == "exact_official_api_response" else "pinned_inventory.json",
                   "metadata_provenance": metadata_kind,
                   "metadata_refresh_pending": metadata_kind != "exact_official_api_response",
                   "metadata_observation": document.get("observation", {}),
                   "specification_verification": "Official datafile 2011, observed size, PDF header/EOF, SHA256",
                   "audit_policy": "Clean source/lighting/condition cohorts <=250; held cases by physical site and primary cause; beginning/middle/end samples",
                   "coverage_policy": COVERAGE_POLICY,
                   "coverage_policy_sha256": sha256(json_bytes(COVERAGE_POLICY)),
                   "created_at": time.time(), "trainable": False,
                   "verification": "ZIP entry CRC32 and SHA256 of every retained artifact; whole ZIP part MD5 unverified",
                   "split_policy": "Physical College Station road stays in one fold across lighting; RELLIS and US290 each one train-only physical site; uncertain conversion held",
                   "excluded": ["TX21: no native lane annotations", "Unpaired image members", "All raw videos"]}
        atomic_json(root / "import.json", initial)
        try:
            for archive in archives:
                parts = sorted((item for item in files if re.fullmatch(re.escape(archive) + r"\.zip\.\d{3}", item["filename"])), key=lambda item: item["filename"])
                with RangedSplitZIP(parts, http) as reader, zipfile.ZipFile(reader) as zipped:
                    pairs = paired_entries(zipped, archive)
                    inventory[archive] = {"paired_images": len(pairs),
                                          "selected_compressed_bytes": sum(a.compress_size + b.compress_size for a, b in pairs),
                                          "parts": [{key: part[key] for key in ("id", "filename", "filesize", "checksum") if key in part} for part in parts]}
                    expected += len(pairs)
                    for picture, label in pairs:
                        if limit is not None and len(imported) >= limit:
                            break
                        previous = prior.get(picture.filename)
                        if previous and verify_record(root, previous):
                            imported.append(previous)
                            skipped += 1
                            continue
                        retained = (previous and previous.get("converter") == "college-station-native-lines-v1"
                                    and verify_record(root, previous, require_converter=False))
                        if retained:
                            # Upgrade geometry semantics without downloading already
                            # CRC/SHA-verified original photographs a second time.
                            image_data = (root / previous["image"]).read_bytes()
                            label_data = (root / previous["source_label"]).read_bytes()
                            skipped += 1
                        else:
                            image_data = zipped.read(picture)  # zipfile validates the official entry CRC32.
                            label_data = zipped.read(label)
                        record, geometry_data = image_record(archive, picture.filename, image_data, label_data,
                                                             image_crc=f"{picture.CRC:08x}", label_crc=f"{label.CRC:08x}")
                        if not retained:
                            atomic_bytes(root / record["image"], image_data)
                            atomic_bytes(root / record["source_label"], label_data)
                        atomic_bytes(root / record["geometry"], geometry_data)
                        with (root / "progress.jsonl").open("ab") as journal:
                            journal.write(json_bytes(record))
                        imported.append(record)
                        if len(imported) % 50 == 0:
                            status = {"state": "importing", "processed": len(imported), "expected_so_far": expected,
                                      "archive": archive, "network_bytes": http.network_bytes, "resumed": skipped,
                                      "updated_at": time.time()}
                            atomic_json(root / "status.json", status)
                            print(json.dumps(status), flush=True)
            imported = assign_audit_groups(apply_coverage_policy(imported))
            manifest = b"".join(json_bytes(row) for row in sorted(imported, key=lambda row: row["image_id"]))
            audits = json_bytes(make_audits(imported))
            atomic_bytes(root / "manifest.jsonl", manifest)
            atomic_bytes(root / "audits.json", audits)
            final = {**initial, "state": "awaiting_audit", "completed_at": time.time(), "images": len(imported),
                     "expected_paired_images": expected, "pilot": limit is not None and len(imported) < expected,
                     "inventory": inventory, "manifest_sha256": sha256(manifest), "audits_sha256": sha256(audits),
                     "counts_by_split": dict(Counter(row["split"] for row in imported)),
                     "qc_counts": dict(Counter(reason for row in imported for reason in row["qc_reasons"])),
                     "audit_groups": len(json.loads(audits)["groups"]), "audit_samples": json.loads(audits)["sample_count"]}
            atomic_json(root / "import.json", final)
            atomic_json(root / "status.json", {"state": "awaiting_audit", "processed": len(imported),
                                                "expected": expected, "network_bytes": http.network_bytes,
                                                "resumed": skipped, "updated_at": time.time()})
            print(json.dumps({key: value for key, value in final.items() if key not in ("inventory",)}), flush=True)
            return final
        except Exception as error:
            atomic_json(root / "status.json", {"state": "error", "processed": len(imported),
                                                "expected_so_far": expected, "error": str(error),
                                                "updated_at": time.time(), "resume": "Rerun same command; retained artifacts are SHA256 checked"})
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("BrakeCheck_Dataset/experiments/college_station_lane_marking"))
    parser.add_argument("--limit", type=int, help="Isolated pilot size; omit to fetch all labeled front images")
    parser.add_argument("--archives", nargs="+", choices=ARCHIVES, default=ARCHIVES)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    standard = Path("BrakeCheck_Dataset/experiments/college_station_lane_marking").resolve()
    if args.limit is not None and args.output.resolve() == standard:
        parser.error("--limit needs a separate --output pilot folder; the full source cannot publish partial audits")
    run(args.output, limit=args.limit, archives=tuple(args.archives))


if __name__ == "__main__":
    main()
