"""Frame extraction: FFmpeg at a base rate, then adaptive keep/skip by scene change.

FFmpeg is preferred (it reads comma2k19's raw .hevc). OpenCV is the fallback for containers it can open.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)


@dataclass
class Frame:
    path: Path
    index: int          # frame number at the base rate
    t: float            # seconds from the start


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def extract_ffmpeg(video: Path, out_dir: Path, fps: float, input_format: str | None = None,
                   source_fps: float | None = None) -> list[Frame]:
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(out_dir.glob("f_*.png"))
    if existing:  # idempotent: already extracted
        return [Frame(p, i, i / fps) for i, p in enumerate(existing)]
    cmd = [ffmpeg_path() or "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if input_format:
        cmd += ["-f", input_format]
    if source_fps:
        cmd += ["-r", str(source_fps)]          # raw .hevc has no timestamps
    cmd += ["-i", str(video), "-vf", f"fps={fps}", "-vsync", "vfr", str(out_dir / "f_%06d.png")]
    subprocess.run(cmd, check=True)
    return [Frame(p, i, i / fps) for i, p in enumerate(sorted(out_dir.glob("f_*.png")))]


def extract_opencv(video: Path, out_dir: Path, fps: float) -> list[Frame]:
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(out_dir.glob("f_*.png"))
    if existing:
        return [Frame(p, i, i / fps) for i, p in enumerate(existing)]
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise IOError(f"OpenCV cannot open {video}; install ffmpeg")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 20.0
    step = max(1, int(round(src_fps / fps)))
    frames, n, k = [], 0, 0
    while True:
        ok, bgr = cap.read()
        if not ok:
            break
        if n % step == 0:
            k += 1
            p = out_dir / f"f_{k:06d}.png"
            cv2.imencode(".png", bgr)[1].tofile(str(p))
            frames.append(Frame(p, k - 1, n / src_fps))
        n += 1
    cap.release()
    return frames


def extract(video: Path, out_dir: Path, fps: float, input_format: str | None = None,
            source_fps: float | None = None) -> list[Frame]:
    if ffmpeg_path():
        return extract_ffmpeg(video, out_dir, fps, input_format, source_fps)
    log.warning("ffmpeg not found; using OpenCV for %s", video)
    return extract_opencv(video, out_dir, fps)


def _signature(path: Path) -> np.ndarray | None:
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    return cv2.resize(img, (64, 36), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0


def adaptive_select(frames: list[Frame], min_change: float, max_gap_s: float) -> list[Frame]:
    """Keep a frame when the scene changed enough since the last kept frame, or when too long has passed.

    Stopped at a red light -> one frame; a fast curve -> every base-rate frame.
    """
    kept: list[Frame] = []
    last_sig, last_t = None, -1e9
    for f in frames:
        sig = _signature(f.path)
        if sig is None:
            continue
        change = 1.0 if last_sig is None else float(np.abs(sig - last_sig).mean())
        if change >= min_change or (f.t - last_t) >= max_gap_s:
            kept.append(f)
            last_sig, last_t = sig, f.t
    return kept
