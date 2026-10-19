"""Licensed comma10k acquisition and deterministic first-pass curation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import sqlite3
import time
from urllib.parse import quote

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError
import requests


REPOSITORY = "commaai/comma10k"
API = f"https://api.github.com/repos/{REPOSITORY}"
DIRECTORIES = (
    "config", "sources", "raw/comma10k", "raw/comma2k19", "raw/carla", "raw/zod",
    "extracted_frames", "curated_images", "annotations/teacher_predictions",
    "annotations/fused_predictions", "annotations/reviewed", "lane_geometry",
    "road_masks", "review_queue", "exports/ufld_v2",
    "exports/semantic_segmentation", "reports", "models", "logs", "tests",
)


def setup(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    for directory in DIRECTORIES:
        (root / directory).mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "metadata.sqlite3")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS images (
        image_id TEXT PRIMARY KEY, source TEXT NOT NULL, session_id TEXT NOT NULL,
        source_commit TEXT NOT NULL, image_path TEXT NOT NULL, mask_path TEXT,
        image_sha256 TEXT NOT NULL, mask_sha256 TEXT, dhash TEXT NOT NULL,
        width INTEGER NOT NULL, height INTEGER NOT NULL, split TEXT NOT NULL,
        quality_flags TEXT NOT NULL, near_duplicate_of TEXT,
        annotation_provenance TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'unreviewed',
        imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS images_sha ON images(image_sha256)")
    db.execute("CREATE INDEX IF NOT EXISTS images_session ON images(source, session_id)")
    # A dataset created on Windows must remain readable from WSL and Linux.
    db.execute("UPDATE images SET image_path=replace(image_path, char(92), '/')")
    db.execute("UPDATE images SET mask_path=replace(mask_path, char(92), '/') WHERE mask_path IS NOT NULL")
    db.execute("""CREATE TABLE IF NOT EXISTS predictions (
        image_id TEXT NOT NULL, model_name TEXT NOT NULL, checkpoint_sha256 TEXT NOT NULL,
        lane_mask_path TEXT NOT NULL, road_mask_path TEXT NOT NULL,
        lane_pixels INTEGER NOT NULL, road_pixels INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'provisional',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (image_id, model_name, checkpoint_sha256),
        FOREIGN KEY (image_id) REFERENCES images(image_id)
    )""")
    db.commit()
    return db


def license_registry(project: Path) -> dict:
    return json.loads((project / "config/sources.json").read_text(encoding="utf-8"))


def require_clearance(project: Path, source: str) -> None:
    entry = license_registry(project).get(source)
    if not entry or entry.get("pool") != "commercial" or entry.get("license_status") != "cleared":
        raise PermissionError(f"{source} is not cleared for the commercial-training pool")


def github_json(url: str) -> dict:
    response = requests.get(url, headers={"Accept": "application/vnd.github+json"}, timeout=45)
    response.raise_for_status()
    return response.json()


def comma10k_plan(project: Path, root: Path, count: int = 100) -> dict:
    require_clearance(project, "comma10k")
    if not 1 <= count <= 10000:
        raise ValueError("count must be between 1 and 10000")
    commit = github_json(f"{API}/commits/master")["sha"]
    tree = github_json(f"{API}/git/trees/{commit}?recursive=1")
    if tree.get("truncated"):
        raise RuntimeError("GitHub tree was truncated; refusing incomplete source inventory")
    files = {item["path"]: item for item in tree["tree"] if item["type"] == "blob"}
    pairs = []
    for path, item in sorted(files.items()):
        if not path.startswith("imgs/") or not path.endswith(".png"):
            continue
        mask_path = "masks/" + path.removeprefix("imgs/")
        if mask_path not in files:
            continue
        mask = files[mask_path]
        pairs.append({"image": path, "mask": mask_path,
                      "image_blob": item["sha"], "mask_blob": mask["sha"],
                      "image_bytes": item["size"], "mask_bytes": mask["size"]})
    if len(pairs) < count:
        raise RuntimeError(f"Only {len(pairs)} paired source files found")
    # Evenly span the source inventory rather than choosing 100 adjacent files.
    indices = [round(i * (len(pairs) - 1) / max(1, count - 1)) for i in range(count)]
    selected = [pairs[i] for i in indices]
    total = sum(p["image_bytes"] + p["mask_bytes"] for p in selected)
    plan = {"source": "comma10k", "commit": commit, "pair_count": count,
            "total_bytes": total, "destination": str(root / "raw/comma10k"),
            "license": "MIT", "pairs": selected}
    destination = root / "config/comma10k-plan.json"
    destination.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return plan


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def fetch_file(commit: str, remote_path: str, destination: Path, expected_blob: str,
               expected_size: int) -> None:
    parts = PurePosixPath(remote_path).parts
    if not parts or ".." in parts or "\\" in remote_path or remote_path.startswith("/"):
        raise ValueError(f"Unsafe source path: {remote_path}")
    if expected_size < 0 or expected_size > 100_000_000:
        raise ValueError(f"Unexpected source file size: {remote_path}")
    if destination.exists():
        data = destination.read_bytes()
        if len(data) == expected_size and git_blob_sha1(data) == expected_blob:
            return
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/{quote(remote_path)}"
    temporary = destination.with_name(destination.name + ".part")
    for attempt in range(3):
        try:
            with requests.get(url, stream=True, timeout=(15, 90)) as response:
                response.raise_for_status()
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1 << 20):
                        if chunk:
                            output.write(chunk)
            data = temporary.read_bytes()
            if len(data) != expected_size or git_blob_sha1(data) != expected_blob:
                raise ValueError(f"Checksum/size mismatch: {remote_path}")
            os.replace(temporary, destination)
            return
        except (requests.RequestException, ValueError, OSError):
            temporary.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(1 + attempt)


def dhash(image: Image.Image) -> int:
    pixels = np.asarray(image.convert("L").resize((9, 8)), dtype=np.uint8)
    return sum(int(pixels[y, x] > pixels[y, x + 1]) << (y * 8 + x)
               for y in range(8) for x in range(8))


def session_from_name(filename: str) -> str:
    parts = Path(filename).stem.split("_")
    return "_".join(parts[1:3]) if len(parts) >= 3 else Path(filename).stem


def split_for(source: str, session: str) -> str:
    bucket = int.from_bytes(hashlib.sha256(f"{source}:{session}".encode()).digest()[:4], "big") % 100
    return "train" if bucket < 80 else "validation" if bucket < 90 else "test"


def inspect_pair(image_path: Path, mask_path: Path) -> tuple[int, int, int, list[str]]:
    with Image.open(image_path) as opened:
        opened.verify()
    with Image.open(image_path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    with Image.open(mask_path) as mask:
        mask.verify()
    with Image.open(mask_path) as mask:
        if mask.size != image.size:
            raise ValueError(f"Image/mask dimensions differ: {image_path.name}")
    pixels = np.asarray(image)
    gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
    flags = []
    if gray.mean() < 25:
        flags.append("dark_review")
    if gray.mean() > 232:
        flags.append("bright_review")
    if cv2.Laplacian(gray, cv2.CV_64F).var() < 10:
        flags.append("blur_review")
    return image.width, image.height, dhash(image), flags


def import_pair(db: sqlite3.Connection, root: Path, pair: dict, commit: str) -> str:
    image_path = root / "raw/comma10k" / pair["image"]
    mask_path = root / "raw/comma10k" / pair["mask"]
    width, height, perceptual_hash, flags = inspect_pair(image_path, mask_path)
    image_sha = hashlib.sha256(image_path.read_bytes()).hexdigest()
    mask_sha = hashlib.sha256(mask_path.read_bytes()).hexdigest()
    image_id = hashlib.sha256(f"comma10k:{commit}:{pair['image']}".encode()).hexdigest()[:24]
    existing = db.execute("SELECT image_id FROM images WHERE image_sha256=? AND image_id<>?",
                          (image_sha, image_id)).fetchone()
    if existing:
        flags.append("exact_duplicate")
    near = None
    if not existing:
        for other_id, other_hash in db.execute("SELECT image_id,dhash FROM images"):
            if other_id != image_id and (perceptual_hash ^ int(other_hash, 16)).bit_count() <= 4:
                near = other_id
                flags.append("near_duplicate_review")
                break
    session = session_from_name(pair["image"])
    db.execute("""INSERT INTO images
        (image_id,source,session_id,source_commit,image_path,mask_path,image_sha256,
         mask_sha256,dhash,width,height,split,quality_flags,near_duplicate_of,
         annotation_provenance,review_status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(image_id) DO UPDATE SET image_sha256=excluded.image_sha256,
          mask_sha256=excluded.mask_sha256,quality_flags=excluded.quality_flags""",
        (image_id, "comma10k", session, commit, image_path.relative_to(root).as_posix(),
         mask_path.relative_to(root).as_posix(), image_sha, mask_sha, f"{perceptual_hash:016x}",
         width, height, split_for("comma10k", session), json.dumps(flags), near,
         "original comma10k human semantic mask", "unreviewed"))
    db.commit()
    if not existing:
        destination = root / "curated_images" / f"{image_id}.png"
        if not destination.exists():
            try:
                os.link(image_path, destination)
            except OSError:
                shutil.copy2(image_path, destination)
    return image_id


def fetch_comma10k(project: Path, root: Path, maximum_bytes: int = 5_000_000_000) -> dict:
    require_clearance(project, "comma10k")
    plan = json.loads((root / "config/comma10k-plan.json").read_text(encoding="utf-8"))
    if plan["source"] != "comma10k" or plan["total_bytes"] > maximum_bytes:
        raise ValueError("Plan exceeds the configured download cap or has an unexpected source")
    db = setup(root)
    notice = root / "sources/comma10k-LICENSE.txt"
    if not notice.exists():
        url = f"https://raw.githubusercontent.com/{REPOSITORY}/{plan['commit']}/LICENSE"
        response = requests.get(url, timeout=45)
        response.raise_for_status()
        if "Copyright" not in response.text or "Comma.ai" not in response.text:
            raise ValueError("Unexpected upstream comma10k license notice")
        notice.write_text(response.text, encoding="utf-8")
    (root / "sources/comma10k-provenance.json").write_text(json.dumps({
        "repository": f"https://github.com/{REPOSITORY}",
        "commit": plan["commit"], "license": "MIT",
        "license_notice": notice.relative_to(root).as_posix(),
        "image_count_planned": plan["pair_count"],
    }, indent=2) + "\n", encoding="utf-8")
    imported = 0
    for pair in plan["pairs"]:
        for role in ("image", "mask"):
            remote = pair[role]
            expected_prefix = "imgs/" if role == "image" else "masks/"
            if not remote.startswith(expected_prefix) or not remote.endswith(".png"):
                raise ValueError(f"Unexpected {role} source path: {remote}")
            destination = root / "raw/comma10k" / remote
            fetch_file(plan["commit"], remote, destination, pair[f"{role}_blob"], pair[f"{role}_bytes"])
        try:
            import_pair(db, root, pair, plan["commit"])
            imported += 1
        except (UnidentifiedImageError, OSError, ValueError) as error:
            (root / "logs/import-errors.jsonl").open("a", encoding="utf-8").write(
                json.dumps({"pair": pair["image"], "error": str(error)}) + "\n")
    result = {"planned_pairs": plan["pair_count"], "imported_pairs": imported,
              "database_rows": db.execute("SELECT COUNT(*) FROM images").fetchone()[0],
              "unique_sha256": db.execute("SELECT COUNT(DISTINCT image_sha256) FROM images").fetchone()[0],
              "split_counts": dict(db.execute("SELECT split,COUNT(*) FROM images GROUP BY split"))}
    (root / "reports/milestone1.json").write_text(json.dumps(result, indent=2) + "\n")
    db.close()
    return result
