"""comma10k: 10k road images with human-checked segmentation masks (MIT).

Downloads a session-balanced subset straight from GitHub (no full clone), verifies every file
against its git blob hash, and registers the mask as a `source_label` annotation. Mask colours:
road #402020, lane markings #ff0000, undrivable #808060, movable #00ff66, my car #cc00ff.
"""
from __future__ import annotations

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import requests

from ..config import Settings
from ..db import Registry
from ..licenses import LicenseRegistry
from .base import UA, check_download_budget, download, register_image

log = logging.getLogger(__name__)

API = "https://api.github.com/repos/{repo}/git/trees/{sha}"
RAW = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"

# comma10k class colours (RGB) -> class id used across the pipeline
CLASS_COLORS = {
    1: (0x40, 0x20, 0x20),   # road
    2: (0xFF, 0x00, 0x00),   # lane markings
    3: (0x80, 0x80, 0x60),   # undrivable
    4: (0x00, 0xFF, 0x66),   # movable
    5: (0xCC, 0x00, 0xFF),   # my car
    6: (0x00, 0xCC, 0xFF),   # movable in my car (driver cam only)
}
ROAD, LANE, UNDRIVABLE, MOVABLE, MY_CAR = 1, 2, 3, 4, 5
NAME_RE = re.compile(r"(?:^|_)([0-9a-f]{16})_(\d{4}-\d{2}-\d{2}--\d{2}-\d{2}-\d{2})_(\d+)_(\d+)")


def session_of(filename: str) -> tuple[str, int | None]:
    """'0001_a23b..._2018-06-24--00-29-19_17_79.png' -> ('a23b..._2018-06-24--00-29-19', 79)."""
    m = NAME_RE.search(filename)
    if not m:
        return Path(filename).stem, None
    dongle, route, segment, frame = m.groups()
    return f"{dongle}_{route}", int(segment) * 1200 + int(frame)


def mask_to_classes(mask_rgb: np.ndarray) -> np.ndarray:
    """RGB mask -> uint8 class map (0 = unknown colour). Nearest colour within a small tolerance."""
    out = np.zeros(mask_rgb.shape[:2], np.uint8)
    m = mask_rgb.astype(np.int16)
    for cls, rgb in CLASS_COLORS.items():
        d = np.abs(m - np.array(rgb, np.int16)).sum(axis=2)
        out[d <= 24] = cls
    return out


def _tree(repo: str, sha: str, session: requests.Session) -> list[dict]:
    r = session.get(API.format(repo=repo, sha=sha), headers=UA, timeout=60)
    r.raise_for_status()
    return r.json()["tree"]


def list_files(repo: str, branch: str, folders: list[str], session: requests.Session) -> dict[str, dict]:
    top = {e["path"]: e for e in _tree(repo, branch, session)}
    files: dict[str, dict] = {}
    for folder in list(folders) + [f.replace("imgs", "masks") for f in folders]:
        if folder not in top:
            raise RuntimeError(f"{repo}: folder {folder} not found")
        for e in _tree(repo, top[folder]["sha"], session):
            if e["type"] == "blob":
                files[f"{folder}/{e['path']}"] = {"sha": e["sha"], "size": e["size"]}
    return files


def plan(settings: Settings, session: requests.Session | None = None, limit: int | None = None) -> list[dict]:
    cfg = settings.get("sources.comma10k", {})
    repo, branch = cfg.get("repo", "commaai/comma10k"), cfg.get("branch", "master")
    http = session or requests.Session()
    files = list_files(repo, branch, cfg.get("folders", ["imgs"]), http)
    trainable: set[str] | None = None
    if cfg.get("only_trainable", True):
        text = http.get(RAW.format(repo=repo, branch=branch, path="files_trainable"), headers=UA, timeout=60).text
        trainable = {line.strip() for line in text.splitlines() if line.strip()}
    pairs = []
    for path, meta in files.items():
        folder, name = path.split("/", 1)
        if not folder.startswith("imgs"):
            continue
        mask_path = f"{folder.replace('imgs', 'masks')}/{name}"
        if mask_path not in files:
            continue
        if trainable is not None and mask_path not in trainable:
            continue
        session_id, frame = session_of(name)
        pairs.append({"img": path, "mask": mask_path, "img_meta": meta, "mask_meta": files[mask_path],
                      "session": session_id, "frame": frame, "name": name})
    # Session-balanced, deterministic order: round-robin over sessions so a small limit still spans many drives.
    by_session: dict[str, list[dict]] = {}
    for p in sorted(pairs, key=lambda p: p["name"]):
        by_session.setdefault(p["session"], []).append(p)
    order = sorted(by_session, key=lambda s: hashlib.sha1((s + str(settings.get("seed", 0))).encode()).hexdigest())
    ordered, depth = [], 0
    while len(ordered) < len(pairs):
        for s in order:
            if depth < len(by_session[s]):
                ordered.append(by_session[s][depth])
        depth += 1
    limit = int(limit or cfg.get("limit", 0) or 0)       # the command-line limit wins over the config
    return ordered[:limit] if limit else ordered


def ingest(settings: Settings, registry: Registry, licenses: LicenseRegistry, limit: int | None = None,
           approved_gb: float | None = None, workers: int = 8) -> dict:
    cfg = settings.get("sources.comma10k", {})
    repo, branch = cfg.get("repo", "commaai/comma10k"), cfg.get("branch", "master")
    http = requests.Session()
    items = plan(settings, http, limit)
    raw = settings.path("raw", "comma10k")
    todo = [i for i in items if not (raw / i["img"]).exists() or not (raw / i["mask"]).exists()]
    planned = sum(i["img_meta"]["size"] + i["mask_meta"]["size"] for i in todo)
    check_download_budget(settings, planned, raw, approved_gb)
    counts = {"planned": len(items), "downloaded": 0, "added": 0, "duplicate": 0, "rejected": 0, "failed": 0}

    def fetch(item: dict) -> dict:
        for key, meta in (("img", item["img_meta"]), ("mask", item["mask_meta"])):
            download(RAW.format(repo=repo, branch=branch, path=item[key]), raw / item[key],
                     expected_size=meta["size"], git_sha=meta["sha"], session=http)
        return item

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, i): i for i in todo}
        for fut in as_completed(futures):
            try:
                fut.result()
                counts["downloaded"] += 1
            except Exception as exc:  # keep going; the run is resumable
                counts["failed"] += 1
                log.error("comma10k %s: %s", futures[fut]["name"], exc)

    for item in items:
        img_path, mask_path = raw / item["img"], raw / item["mask"]
        if not img_path.exists() or not mask_path.exists():
            continue
        res = register_image(settings, registry, licenses, img_path, source="comma10k",
                             session_id=item["session"], source_ref=f"{repo}@{branch}:{item['img']}",
                             frame_index=item["frame"])
        counts[res.status] += 1
        # attach the human mask even to rejected frames: a later `requalify` may restore them
        if res.image_id and registry.annotation(res.image_id, "source_label") is None:
            registry.update_image(res.image_id, has_source_label=1)
            registry.add_annotation(res.image_id, "source_label", "comma10k", f"git:{item['mask_meta']['sha'][:12]}",
                                    str(mask_path), license_ids=["comma10k"],
                                    params={"format": "comma10k_rgb", "human": True})
    return counts
