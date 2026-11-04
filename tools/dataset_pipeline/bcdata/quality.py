"""Image validity, blur, exposure, exact and near-duplicate checks.

Hard rejects are only for frames nobody could label: undecodable, tiny, nearly uniform, pitch black,
blown-out white, or severely blurred. Dark, rainy, foggy or glary frames are *tagged*, never rejected:
they are the hard cases the lane model most needs.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def read_rgb(path: str | Path) -> np.ndarray | None:
    data = np.fromfile(str(path), dtype=np.uint8)  # handles non-ASCII Windows paths
    if data.size == 0:
        return None
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        return None
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def write_png(path: str | Path, array: np.ndarray) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".png", array)
    if not ok:
        raise IOError(f"could not encode {path}")
    buf.tofile(str(path))


def write_jpg(path: str | Path, rgb: np.ndarray, quality: int = 95) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise IOError(f"could not encode {path}")
    buf.tofile(str(path))


def read_gray_u8(path: str | Path) -> np.ndarray | None:
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_UNCHANGED)


# ---- perceptual hash ---------------------------------------------------------------------------

def phash(rgb: np.ndarray) -> int:
    """64-bit DCT perceptual hash (the classic pHash: 32x32 gray -> DCT -> top-left 8x8 vs median)."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY) if rgb.ndim == 3 else rgb
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    dct = cv2.dct(small)[:8, :8].flatten()
    med = np.median(dct[1:])
    bits = dct > med
    value = 0
    for b in bits:
        value = (value << 1) | int(b)
    return value


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def near_duplicate_groups(hashes: dict[str, int], max_distance: int) -> list[list[str]]:
    """Group ids whose pHash is within `max_distance` bits, using 8 x 8-bit bands.

    Two 64-bit hashes that differ in at most 7 bits must agree exactly on at least one of 8 bands
    (pigeonhole), so band buckets find every pair for max_distance <= 7 without an O(n^2) scan.
    """
    if max_distance > 7:
        raise ValueError("banding guarantees recall only up to 7 bits")
    ids = list(hashes)
    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for band in range(8):
        buckets: dict[int, list[str]] = {}
        shift = 8 * band
        for i in ids:
            buckets.setdefault((hashes[i] >> shift) & 0xFF, []).append(i)
        for members in buckets.values():
            if len(members) < 2:
                continue
            for a_i in range(len(members)):
                for b_i in range(a_i + 1, len(members)):
                    a, b = members[a_i], members[b_i]
                    if find(a) != find(b) and hamming(hashes[a], hashes[b]) <= max_distance:
                        parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)
    return [g for g in groups.values() if len(g) > 1]


# ---- per-image quality -------------------------------------------------------------------------

@dataclass
class QualityReport:
    ok: bool
    reason: str | None
    metrics: dict = field(default_factory=dict)
    tags: dict = field(default_factory=dict)


def assess(rgb: np.ndarray | None, cfg: dict) -> QualityReport:
    if rgb is None:
        return QualityReport(False, "undecodable")
    h, w = rgb.shape[:2]
    if w < cfg.get("min_width", 320) or h < cfg.get("min_height", 240):
        return QualityReport(False, "too_small", {"width": w, "height": h})
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    scale = 640.0 / w
    small = cv2.resize(gray, (640, max(1, int(round(h * scale)))), interpolation=cv2.INTER_AREA)
    std = float(small.std())
    p1, p50, p99 = (float(v) for v in np.percentile(small, [1, 50, 99]))
    lap = cv2.Laplacian(small, cv2.CV_64F)
    lap_var = float(lap.var())
    # Blur is judged per tile: an overcast sky and smooth asphalt have little texture, but if lane paint,
    # a car or a sign anywhere is crisp the frame is not motion-blurred. Use the sharpest tiles.
    th, tw = small.shape[0] // 4, small.shape[1] // 4
    tiles = [lap[r * th:(r + 1) * th, c * tw:(c + 1) * tw] for r in range(4) for c in range(4)]
    tile_var = sorted(float(t.var()) for t in tiles)
    sharp_tiles = float(np.mean(tile_var[-3:]))
    norm_focus = sharp_tiles / (std * std + 1.0)
    saturated = float((small >= 250).mean())
    dark = float((small <= 20).mean())
    # brightness of the scene itself (top 60%: sky, buildings, far road), not the dashboard or hood
    scene_p50 = float(np.percentile(small[: int(small.shape[0] * 0.6)], 50))
    # lower 55% of the frame is where lanes are; glare there matters most
    road = small[int(small.shape[0] * 0.45):]
    road_sat = float((road >= 250).mean())
    metrics = {"width": w, "height": h, "std": round(std, 2), "p1": p1, "p50": p50, "p99": p99, "scene_p50": scene_p50,
               "lap_var": round(lap_var, 2), "sharp_tiles": round(sharp_tiles, 2), "norm_focus": round(norm_focus, 4),
               "saturated": round(saturated, 4), "dark": round(dark, 4), "road_saturated": round(road_sat, 4)}

    if p99 < cfg.get("black_p99", 14):
        return QualityReport(False, "black_frame", metrics)
    if p1 > cfg.get("white_p1", 242):
        return QualityReport(False, "white_frame", metrics)
    if std < cfg.get("invalid_std", 3.0):
        return QualityReport(False, "uniform_frame", metrics)
    if sharp_tiles < cfg.get("blur_abs", 12.0) and norm_focus < cfg.get("blur_norm", 0.01):
        return QualityReport(False, "severe_blur", metrics)

    tags = {}
    # cut-offs set by eye on comma10k (its camera exposes dark); tags only, never a reason to reject
    if scene_p50 < 32:
        tags["lighting"] = "night"
    elif scene_p50 < 42:
        tags["lighting"] = "dusk_or_dark"
    else:
        tags["lighting"] = "day"
    if saturated > 0.02 or road_sat > 0.01:
        tags["glare"] = True
    if sharp_tiles < 20:
        tags["soft_focus"] = True          # rain, fog or motion: keep, but note it
    return QualityReport(True, None, metrics, tags)
