"""Restore the supplied ZIP without treating its absolute paths as portable.

Source ZIPs and the imported registry remain immutable. Working annotations and
human corrections live separately. No model training runs in this module.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict
from pathlib import Path, PureWindowsPath
import hashlib
import json
import logging
import re
import sqlite3
import time
import zipfile
import uuid

import requests

from .db import Registry
from .local import local_settings
from .sources.comma10k import session_of

log = logging.getLogger(__name__)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        for attempt in range(8):
            try:
                temp.replace(path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(.025 * (attempt + 1))
    finally:
        temp.unlink(missing_ok=True)


def extract_archive(archive: Path, destination: Path):
    """Do not extract outside destination; skip complete previously extracted files."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    count = 0
    with zipfile.ZipFile(archive) as source:
        for item in source.infolist():
            if item.is_dir():
                continue
            path = (destination / item.filename).resolve()
            if not path.is_relative_to(root) or ":" in item.filename or "\\" in item.filename:
                raise ValueError(f"Unsafe archive path: {item.filename}")
            if path.exists() and path.stat().st_size == item.file_size:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + ".extracting")
            with source.open(item) as src, temp.open("wb") as dst:
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)
            temp.replace(path)
            count += 1
    return count


def _restore_registry(source: Path, destination: Path):
    # The packaged SQLite is in WAL mode but has no WAL file. Deserialize a copy
    # in rollback mode, then use SQLite backup; never change the imported bytes.
    data = bytearray(source.read_bytes())
    data[18:20] = b"\x01\x01"
    memory = sqlite3.connect(":memory:")
    try:
        memory.deserialize(bytes(data))
        dest = sqlite3.connect(destination)
        try:
            memory.backup(dest)
        finally:
            dest.close()
    finally:
        memory.close()


def _download(job):
    url, destination, expected, hash_kind = job
    if destination.exists():
        payload = destination.read_bytes()
    else:
        payload = None
    for attempt in range(4):
        try:
            if payload is None:
                response = requests.get(url, timeout=(15, 90))
                response.raise_for_status()
                payload = response.content
            digest = (hashlib.sha256(payload).hexdigest() if hash_kind == "sha256" else
                      hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest())
            if not digest.startswith(expected):
                raise ValueError(f"Hash mismatch for {url}: {digest} != {expected}")
            if not destination.exists() or destination.read_bytes() != payload:
                destination.parent.mkdir(parents=True, exist_ok=True)
                tmp = destination.with_name(destination.name + ".download")
                tmp.write_bytes(payload)
                tmp.replace(destination)
            return str(destination)
        except Exception:
            payload = None
            if attempt == 3:
                raise
            time.sleep(attempt + 1)


def prepare(data_root=None, archive=None, offline=False):
    settings = local_settings(data_root)
    archive = Path(archive or Path.home() / "Downloads" / "BrakeCheck_2k_dataset.zip")
    imported = settings.path("imported", "BrakeCheck_Dataset")
    report_path = settings.path("reports", "preparation.json")
    progress = {"state": "running", "stage": "import", "started": time.time(), "training": "deferred"}
    atomic_json(report_path, progress)
    try:
        if not archive.is_file():
            raise FileNotFoundError(archive)
        extracted = extract_archive(archive, settings.path("imported"))
        first_import = not settings.db_path.exists()
        if first_import:
            _restore_registry(imported / "metadata.sqlite", settings.db_path)
        registry = Registry(settings.db_path)
        try:
            with registry.tx() as con:
                con.executescript("""
                    CREATE TABLE IF NOT EXISTS local_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS review_versions(image_id TEXT PRIMARY KEY REFERENCES images(image_id), revision INTEGER NOT NULL DEFAULT 0);
                    CREATE TABLE IF NOT EXISTS review_events(event_id TEXT PRIMARY KEY, image_id TEXT NOT NULL REFERENCES images(image_id), action TEXT NOT NULL, reviewer TEXT NOT NULL, before_state TEXT NOT NULL, after_state TEXT NOT NULL, created_at REAL NOT NULL, undone_event TEXT);
                """)
                con.execute("INSERT OR IGNORE INTO local_meta VALUES ('revision','0')")
                con.execute("INSERT OR IGNORE INTO local_meta VALUES ('dataset_id',?)", (hashlib.sha256((imported / "metadata.sqlite").read_bytes()).hexdigest(),))
            progress.update(stage="relocate", extracted_files=extracted)
            atomic_json(report_path, progress)
            image_index = {}
            for base in [imported / "exports/semantic_segmentation/BrakeCheck_RoadLane/images",
                         imported / "review_queue/batch_001/cvat_task/images"]:
                if base.exists():
                    for path in base.rglob("*.png"):
                        image_index.setdefault(path.stem, path)
            jobs = []
            image_paths = {}
            source_paths = {}
            sessions_before = {}
            for row in registry.images():
                image_id = row["image_id"]
                match = re.fullmatch(r"([^@]+)@([^:]+):(.+)", row["source_ref"])
                if match is None:
                    raise ValueError(f"Unsupported source reference: {row['source_ref']}")
                repo, revision, relative = match.groups()
                filename = PureWindowsPath(relative).name
                photo = image_index.get(image_id) or settings.path("raw", "comma10k", "imgs", filename)
                image_paths[image_id] = photo
                if not photo.exists():
                    jobs.append((f"https://raw.githubusercontent.com/{repo}/{revision}/{relative}", photo, row["sha256"], "sha256"))
                else:
                    digest = hashlib.sha256(photo.read_bytes()).hexdigest()
                    if digest != row["sha256"]:
                        raise ValueError(f"Imported photo does not match registry SHA256: {image_id}")
                source = registry.annotation(image_id, "source_label")
                if source is not None:
                    mask = settings.path("raw", "comma10k", "masks", filename)
                    source_paths[image_id] = mask
                    # The original mask hash is in the registry, unlike the
                    # lossy three-class semantic export. Restore exact masks.
                    mask_relative = relative.replace("imgs", "masks", 1)
                    jobs.append((f"https://raw.githubusercontent.com/{repo}/{revision}/{mask_relative}", mask,
                                 source["model_version"].removeprefix("git:"), "git"))
                session, frame = session_of(filename)
                sessions_before[image_id] = (row["session_id"], session, frame)
            if offline:
                unavailable = [str(job[1]) for job in jobs if not job[1].exists()]
                if unavailable:
                    raise RuntimeError(f"Offline preparation needs {len(unavailable)} missing source files. Run prepare online once.")
            progress.update(stage="restore_source_files", source_files=len(jobs), source_files_done=0)
            atomic_json(report_path, progress)
            errors = []
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {pool.submit(_download, job): job for job in jobs}
                for i, future in enumerate(as_completed(futures), 1):
                    try:
                        future.result()
                    except Exception as exc:
                        errors.append(str(exc))
                    if i % 25 == 0 or i == len(jobs):
                        progress.update(source_files_done=i, errors=errors[:10])
                        atomic_json(report_path, progress)
                        log.info("Source files verified %s/%s", i, len(jobs))
            if errors:
                raise RuntimeError(f"{len(errors)} source files could not be restored: {errors[:3]}")
            with registry.tx() as con:
                for image_id, path in image_paths.items():
                    _, session, frame = sessions_before[image_id]
                    con.execute("UPDATE images SET path=?,session_id=?,frame_index=? WHERE image_id=?", (str(path), session, frame, image_id))
                for image_id, path in source_paths.items():
                    con.execute("UPDATE annotations SET path=? WHERE image_id=? AND kind='source_label'", (str(path), image_id))
                # Imported annotation records describe paths on the author's PC.
                # Retain that registry intact in imported/, and keep only locally
                # available proposal/teacher geometry records in the working DB.
                for ann in con.execute("SELECT id,path,kind,producer,image_id FROM annotations").fetchall():
                    if ann["kind"] == "source_label" or ann["kind"] == "human":
                        continue
                    old = ann["path"].replace("\\", "/")
                    suffix = old.split("/BrakeCheck_Dataset/", 1)[-1]
                    target = imported / suffix
                    if ann["kind"] == "geometry" and ann["producer"] == "geometry":
                        target = imported / "lane_geometry" / f"{ann['image_id']}.json"
                    if target.is_file():
                        con.execute("UPDATE annotations SET path=? WHERE id=?", (str(target), ann["id"]))
                    elif not Path(ann["path"]).is_file():
                        con.execute("DELETE FROM annotations WHERE id=?", (ann["id"],))
                # Correct prefixes in session parsing. Existing membership is
                # retained unless two frames of a route were split differently.
                # If that happens, merge the entire route into the most held-out
                # existing split before any real training starts.
                rank = {"train": 0, "val": 1, "test": 2}
                sessions = defaultdict(list)
                for row in registry.images("split IS NOT NULL"):
                    sessions[row["session_id"]].append(row)
                changed = []
                for session, rows in sessions.items():
                    selected = max((r["split"] for r in rows), key=rank.__getitem__)
                    for row in rows:
                        if row["split"] != selected:
                            changed.append({"image_id": row["image_id"], "from": row["split"], "to": selected})
                            con.execute("UPDATE images SET split=? WHERE image_id=?", (selected, row["image_id"]))
            progress.update(stage="source_annotations")
            atomic_json(report_path, progress)
            from .teachers.source_labels import SourceLabelTeacher
            from .teachers.base import run_teacher
            from .licenses import LicenseRegistry
            from . import fusion, lanes, qc
            rows = registry.images("status='curated'")
            teacher = run_teacher(settings, registry, LicenseRegistry(settings.licenses_path), SourceLabelTeacher(settings, registry), rows)
            if teacher["failed"]:
                raise RuntimeError(f"Source annotations failed: {teacher}")
            progress.update(stage="fusion", source_annotation=teacher)
            atomic_json(report_path, progress)
            fused = fusion.run(settings, registry, rows)
            if fused["no_teacher_output"]:
                raise RuntimeError(f"Missing source masks: {fused}")
            # Keep the supplied proposals for review; fill any absent geometry.
            # Restored fused masks allow source comparison/QC without pretending
            # those source labels are independent model predictions.
            progress.update(stage="lane_geometry", fusion=fused)
            atomic_json(report_path, progress)
            available = [r for r in rows if lanes.current_geometry(registry, r["image_id"]) is not None]
            missing_rows = [r for r in rows if lanes.current_geometry(registry, r["image_id"]) is None]
            counts = lanes.run(settings, registry, missing_rows, per_teacher=False)
            counts["imported_proposals"] = len(available)
            progress.update(stage="quality_control", source_annotation=teacher, fusion=fused, geometry=counts)
            atomic_json(report_path, progress)
            quality = qc.run(settings, registry, rows)
            missing_geometry = quality.get("no_geometry", 0)
            if missing_geometry:
                raise RuntimeError(f"Missing geometry for {missing_geometry} curated images")
            split_counts = dict(registry.conn.execute("SELECT split,count(*) FROM images WHERE status='curated' GROUP BY split").fetchall())
            leakage = registry.count("SELECT count(*) FROM (SELECT session_id FROM images WHERE split IS NOT NULL GROUP BY session_id HAVING count(DISTINCT split)>1)")
            broken = [r["image_id"] for r in registry.images() if not Path(r["path"]).is_file()]
            progress.update(state="ready", stage="awaiting_human_review", finished=time.time(), registered=len(image_paths),
                            curated=len(rows), splits=split_counts, qc=quality, missing_images=broken,
                            session_count=len(sessions), split_leakage=leakage, repaired_split_membership=changed,
                            source="Existing comma10k human masks; lane polylines are automatic until reviewed.",
                            teacher="Imported comma_segnet lane geometry available; dense teacher probabilities were not packaged and were not regenerated.",
                            review_url="http://127.0.0.1:8765", training="deferred_by_user")
            if leakage or broken:
                raise RuntimeError("Dataset integrity check failed")
            atomic_json(report_path, progress)
            return progress
        finally:
            registry.close()
    except Exception as exc:
        progress.update(state="error", error=str(exc), finished=time.time())
        atomic_json(report_path, progress)
        raise
