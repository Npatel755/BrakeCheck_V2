"""comma2k19 (MIT): 33 hours of commute video in ~10 GB chunks distributed by torrent.

The download itself is a torrent from Academic Torrents, so this module:
  * prints what to fetch and how big it is (`describe`), and refuses chunks above the approval threshold
    unless approved; with `aria2c` installed it can run the approved torrent download;
  * ingests an extracted chunk from disk: finds every segment's video.hevc, extracts frames with FFmpeg,
    keeps scene-diverse frames, and registers them with route-level session ids (for leak-free splits).
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from ..config import Settings
from ..db import Registry
from ..frames import adaptive_select, extract
from ..licenses import LicenseRegistry
from .base import ApprovalRequired, check_download_budget, register_image

log = logging.getLogger(__name__)

# The README links the official Academic Torrents entry; use that link rather than a copy from elsewhere.
TORRENT_PAGE = "https://github.com/commaai/comma2k19#readme (Academic Torrents link)"
CHUNK_GB = 10.0
CHUNKS = 10
SOURCE_FPS = 20.0


def describe() -> str:
    return (f"comma2k19: {CHUNKS} chunks of ~{CHUNK_GB:.0f} GB (~{CHUNKS * CHUNK_GB:.0f} GB total) via Academic Torrents:\n"
            f"  {TORRENT_PAGE}\n"
            "Each chunk is above the 5 GB approval threshold. Download one approved chunk, extract it, then run\n"
            "  bcdata ingest comma2k19 --path <extracted chunk folder>\n"
            "Route folders contain '|' which NTFS rejects; extract on WSL ext4, or use the repo's utils/unzip_msft_fs.py.")


def torrent_download(settings: Settings, torrent: str, dest: Path, approved_gb: float | None) -> None:
    check_download_budget(settings, int(CHUNK_GB * 1e9), dest, approved_gb)
    if not shutil.which("aria2c"):
        raise ApprovalRequired("aria2c is not installed; download the approved chunk with any torrent client instead")
    subprocess.run(["aria2c", "--seed-time=0", "-d", str(dest), torrent], check=True)


def find_segments(root: Path) -> list[tuple[str, int, Path]]:
    """Return (route_id, segment_number, video_path) for every segment under root."""
    out = []
    for video in sorted(root.rglob("video.hevc")):
        seg_dir = video.parent
        try:
            segment = int(seg_dir.name)
        except ValueError:
            continue
        route = seg_dir.parent.name.replace("|", "_")
        out.append((route, segment, video))
    return out


def ingest(settings: Settings, registry: Registry, licenses: LicenseRegistry, root: Path | None = None,
           limit_segments: int | None = None) -> dict:
    cfg = settings.get("sources.comma2k19", {})
    root = Path(root or cfg.get("local_root") or "")
    if not root.exists():
        raise FileNotFoundError(f"comma2k19 folder not found: {root}\n{describe()}")
    segments = find_segments(root)
    limit = limit_segments or int(cfg.get("segments_limit", 0) or 0)
    if limit:
        segments = segments[:limit]
    counts = {"segments": len(segments), "frames_extracted": 0, "frames_kept": 0, "added": 0, "duplicate": 0, "rejected": 0}
    out_root = settings.path("extracted_frames", "comma2k19")
    for route, segment, video in segments:
        out_dir = out_root / route / f"{segment:03d}"
        frames = extract(video, out_dir, float(cfg.get("base_fps", 1.0)), input_format="hevc", source_fps=SOURCE_FPS)
        counts["frames_extracted"] += len(frames)
        if cfg.get("adaptive", True):
            frames = adaptive_select(frames, float(cfg.get("min_scene_change", 0.06)), float(cfg.get("max_gap_seconds", 5.0)))
        counts["frames_kept"] += len(frames)
        for f in frames:
            res = register_image(settings, registry, licenses, f.path, source="comma2k19", session_id=route,
                                 source_ref=f"{route}/{segment}/video.hevc@{f.t:.2f}s",
                                 frame_index=segment * 60 * 100 + f.index)
            counts[res.status] += 1
    return counts
