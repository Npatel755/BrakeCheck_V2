"""Index every mirror frame for individual audits as verified archives arrive."""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import time

from filelock import FileLock
from PIL import Image

from .bootstrap import atomic_json
from .zod_mirror import connect, convert, encoded, insert_annotation, sha
from .zod_mirror_download import FILES, REVISION, digest

ANNOTATION = re.compile(r"single_frames/([0-9]{6})/annotations/lane_markings\.json\Z")
IMAGE = re.compile(r"(1280x720_images|640x360_images)/([0-9]{6})\.jpg\Z")


def run(root):
    root = Path(root).resolve()
    state = {"schema": "zod_mirror_import:v1", "pid": os.getpid(), "source": "zod_mirror",
             "pool": "share_alike", "revision": REVISION, "auto_promote": False,
             "individual_audits": True, "started_at": time.time(), "annotations": 0,
             "ready_images": 0, "small_images": 0}

    def progress(**values):
        state.update(values, updated_at=time.time())
        atomic_json(root / "import_status.json", state)

    def verified(name):
        path = root / "upstream" / name
        if not path.exists():
            return False
        size, expected = FILES[name]
        if path.stat().st_size != size or digest(path) != expected:
            raise ValueError("Archive failed import integrity: " + name)
        return True

    def wait_for(name):
        progress(state="waiting_for_archive", archive=name)
        while not verified(name):
            status = root / "download_status.json"
            if status.exists():
                download = json.loads(status.read_text())
                if download.get("phase") == "download_error":
                    raise RuntimeError("Download needs attention; rerun resumable acquisition")
            time.sleep(5)

    def extract_images(name):
        large = name.startswith("1280")
        resolution = (1280, 720) if large else (640, 360)
        destination = root / "images" / ("1280x720" if large else "640x360")
        destination.mkdir(parents=True, exist_ok=True)
        count, ids = 0, set()
        progress(state="extracting_images", archive=name)
        with tarfile.open(root / "upstream" / name, mode="r|gz") as archive:
            for member in archive:
                if member.isdir():
                    continue
                match = IMAGE.fullmatch(member.name.removeprefix("./"))
                if not match or not member.isfile() or not 0 < member.size <= 25 * 1024 * 1024:
                    raise ValueError("Unexpected image archive entry: " + member.name)
                if match[1] != ("1280x720_images" if large else "640x360_images"):
                    raise ValueError("Image archive resolution mismatch")
                frame_id = match[2]
                if frame_id in ids:
                    raise ValueError("Duplicate archive image identity: " + frame_id)
                ids.add(frame_id)
                if shutil.disk_usage(root).free < member.size + 20 * 2**30:
                    raise OSError("Extraction needs 20 GiB free-space reserve")
                raw = archive.extractfile(member).read()
                with Image.open(io.BytesIO(raw)) as image:
                    if image.size != resolution or image.format != "JPEG":
                        raise ValueError("Mirror image contract mismatch")
                    image.verify()
                path = destination / (frame_id + ".jpg")
                image_sha = sha(raw)
                if path.exists():
                    if sha(path.read_bytes()) != image_sha:
                        raise ValueError("Existing source photo changed: " + frame_id)
                else:
                    partial = path.with_suffix(".jpg.part")
                    partial.write_bytes(raw)
                    os.replace(partial, path)
                if large:
                    row = db.execute("SELECT * FROM frames WHERE frame_id=?", (frame_id,)).fetchone()
                    if row is None:
                        geometry, warnings = convert(None, "zod-" + frame_id)
                        geometry_json = encoded(geometry)
                        db.execute("INSERT INTO frames(image_id,frame_id,native_json,native_sha,geometry_json,geometry_sha,qc_json) VALUES(?,?,?,?,?,?,?)",
                                   ("zod-" + frame_id, frame_id, "null", sha(b"null"), geometry_json, sha(geometry_json.encode()), encoded(warnings)))
                    elif row["image_sha"] and row["image_sha"] != image_sha:
                        raise ValueError("Audited source identity changed: " + frame_id)
                    db.execute("UPDATE frames SET image_path=?,image_sha=?,image_ready=1 WHERE frame_id=?",
                               (path.relative_to(root).as_posix(), image_sha, frame_id))
                count += 1
                if count % 100 == 0:
                    db.commit()
                    progress(**{"ready_images" if large else "small_images": count})
        db.commit()
        progress(**{"ready_images" if large else "small_images": count})

    with FileLock(str(root / "import.lock"), timeout=0):
        db = connect(root)
        try:
            progress(state="preparing")
            wait_for("annotations.tar.gz")
            progress(state="indexing_annotations", archive="annotations.tar.gz")
            count, ids = 0, set()
            with tarfile.open(root / "upstream/annotations.tar.gz", mode="r|gz") as archive:
                for member in archive:
                    if member.issym() or member.islnk():
                        raise ValueError("Links are not permitted in source archives")
                    match = ANNOTATION.fullmatch(member.name.removeprefix("./"))
                    if not match:
                        continue  # Other annotation tasks remain in the verified archive.
                    if not member.isfile() or not 0 <= member.size <= 8 * 1024 * 1024:
                        raise ValueError("Invalid lane annotation archive member")
                    if match[1] in ids:
                        raise ValueError("Duplicate native lane annotation frame")
                    ids.add(match[1])
                    insert_annotation(db, match[1], archive.extractfile(member).read())
                    count += 1
                    if count % 100 == 0:
                        db.commit()
                        progress(annotations=count)
            db.commit()
            progress(annotations=count)
            wait_for("1280x720_images.tar.gz")
            extract_images("1280x720_images.tar.gz")
            # Both copies use one frame ID and one audit. Small copies are only
            # backup assets and never become additional training examples.
            wait_for("640x360_images.tar.gz")
            extract_images("640x360_images.tar.gz")
            total = db.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
            ready = db.execute("SELECT COUNT(*) FROM frames WHERE image_ready=1").fetchone()[0]
            progress(state="audit_ready", total_frames=total, ready_images=ready,
                     missing_images=total - ready, completed_at=time.time())
        except Exception as error:
            progress(state="import_error", error=str(error))
            raise
        finally:
            db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output)
