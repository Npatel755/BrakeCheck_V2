"""Deterministic, session-aware frame curation for rights-cleared media.

This module does not decide whether media is licensed. Callers must perform
source clearance before passing an image path to ``analyze_image``. The
descriptor is a small, *handcrafted* appearance vector; it is not DINOv3 and
its clusters must not be interpreted as semantic road/weather labels.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError


DESCRIPTOR_VERSION = "spatial-hsv-gradient-v1"


class InvalidImageError(ValueError):
    """The source file cannot be safely used as a training image."""


@dataclass(frozen=True)
class FrameAnalysis:
    image_id: str
    source: str
    session_id: str
    split: str
    file_sha256: str
    phash: int
    descriptor: tuple[float, ...]
    width: int
    height: int
    quality_flags: tuple[str, ...]
    mean_luma: float
    laplacian_variance: float
    descriptor_version: str = DESCRIPTOR_VERSION


@dataclass(frozen=True)
class SelectionResult:
    """Decisions; callers decide how to persist them in the metadata registry.

    ``exact_duplicates`` and ``near_duplicates`` map discarded image IDs to
    retained IDs. ``cluster_overflow`` lists valid, nonduplicate images not
    selected under the per-cluster budget; it is *not* a quality rejection.
    Cluster labels include the split to keep evaluation budgets independent.
    """

    selected_ids: tuple[str, ...]
    exact_duplicates: dict[str, str]
    near_duplicates: dict[str, str]
    cluster_by_id: dict[str, str]
    cluster_overflow: tuple[str, ...]
    descriptor_version: str = DESCRIPTOR_VERSION


def _phash(rgb: np.ndarray) -> int:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    frequencies = cv2.dct(small)[:8, :8].copy()
    median = float(np.median(frequencies.flat[1:]))
    frequencies[0, 0] = median  # DC is dominated by exposure, not structure.
    return sum(int(value > median) << index for index, value in enumerate(frequencies.flat))


def _descriptor(rgb: np.ndarray) -> tuple[float, ...]:
    """Coarse spatial color/edge statistics, stable across small JPEG changes."""
    small = cv2.resize(rgb, (128, 72), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV).astype(np.float32)
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = cv2.magnitude(gx, gy)
    values: list[float] = []
    for row in range(3):
        for column in range(4):
            ys = slice(row * 24, (row + 1) * 24)
            xs = slice(column * 32, (column + 1) * 32)
            hue = hsv[ys, xs, 0] * (2 * np.pi / 180.0)
            saturation = hsv[ys, xs, 1] / 255.0
            lightness = gray[ys, xs]
            edge = magnitude[ys, xs]
            values.extend((
                float(lightness.mean()), float(lightness.std()),
                float(saturation.mean()),
                float((np.sin(hue) * saturation).mean()),
                float((np.cos(hue) * saturation).mean()),
                float(np.minimum(edge, 1).mean()),
                float(np.minimum(np.abs(gx[ys, xs]), 1).mean()),
                float(np.minimum(np.abs(gy[ys, xs]), 1).mean()),
            ))
    # A coarse global histogram prevents uniformly lit and strongly backlit
    # frames with similar spatial averages from collapsing into one scene.
    values.extend(np.histogram(gray, bins=8, range=(0, 1))[0].astype(np.float32) / gray.size)
    vector = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    if norm:
        vector /= norm
    return tuple(float(value) for value in vector)


def analyze_image(path: Path, *, image_id: str, source: str, session_id: str,
                  split: str) -> FrameAnalysis:
    """Verify and describe one already-authorized image, without excluding hard scenes.

    The exact-duplicate key hashes the original file bytes. Near-duplicate
    evidence uses a 64-bit perceptual hash *and* the appearance descriptor.
    Quality flags are review hints only: night, rain and low-texture roads can
    legitimately appear dark or blurry to simple image statistics.
    """
    if not all((image_id, source, session_id)) or split not in {"train", "validation", "test"}:
        raise ValueError("Image ID, provenance and a recognized split are required")
    try:
        data = path.read_bytes()
        with Image.open(BytesIO(data)) as opened:
            opened.verify()
        with Image.open(BytesIO(data)) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            if min(image.size) < 32:
                raise InvalidImageError(f"Image is smaller than 32 pixels: {path}")
            rgb = np.asarray(image)
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError, ValueError) as error:
        if isinstance(error, InvalidImageError):
            raise
        raise InvalidImageError(f"Cannot decode image: {path}") from error
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    measure = cv2.resize(gray, (320, 180), interpolation=cv2.INTER_AREA)
    mean = float(measure.mean())
    dark_fraction = float(np.mean(measure < 12))
    bright_fraction = float(np.mean(measure > 245))
    variance = float(cv2.Laplacian(measure, cv2.CV_64F).var())
    flags: list[str] = []
    if mean < 42 or dark_fraction > 0.65:
        flags.append("dark_review")
    if mean > 215 or bright_fraction > 0.35:
        flags.append("bright_review")
    if variance < 10:
        flags.append("blur_review")
    if dark_fraction > 0.995 or bright_fraction > 0.995:
        flags.append("almost_uniform_review")
    return FrameAnalysis(
        image_id=image_id, source=source, session_id=session_id, split=split,
        file_sha256=hashlib.sha256(data).hexdigest(), phash=_phash(rgb),
        descriptor=_descriptor(rgb), width=rgb.shape[1], height=rgb.shape[0],
        quality_flags=tuple(flags), mean_luma=mean,
        laplacian_variance=variance,
    )


class _HammingTree:
    """BK-tree: query pHash neighbors without a quadratic all-pairs scan."""

    def __init__(self) -> None:
        self.root: tuple[int, str, dict] | None = None

    def add(self, phash: int, image_id: str) -> None:
        if self.root is None:
            self.root = (phash, image_id, {})
            return
        node = self.root
        while True:
            distance = (phash ^ node[0]).bit_count()
            child = node[2].get(distance)
            if child is None:
                node[2][distance] = (phash, image_id, {})
                return
            node = child

    def neighbors(self, phash: int, radius: int) -> list[str]:
        if self.root is None:
            return []
        found: list[str] = []
        stack = [self.root]
        while stack:
            node = stack.pop()
            distance = (phash ^ node[0]).bit_count()
            if distance <= radius:
                found.append(node[1])
            stack.extend(child for edge, child in node[2].items()
                         if distance - radius <= edge <= distance + radius)
        return found


def _normalized_matrix(frames: list[FrameAnalysis]) -> np.ndarray:
    matrix = np.asarray([frame.descriptor for frame in frames], dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[1] == 0 or not np.isfinite(matrix).all():
        raise ValueError("Descriptors must have equal, nonzero lengths and finite values")
    norms = np.linalg.norm(matrix, axis=1)
    if np.any(norms < 1e-8):
        raise ValueError("Descriptors cannot be zero vectors")
    return matrix / norms[:, None]


def _clusters(vectors: np.ndarray, count: int) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic spherical k-means; cluster IDs have no semantic meaning."""
    n = len(vectors)
    count = min(count, n)
    if count == 1:
        return np.zeros(n, dtype=np.int32), vectors.mean(axis=0, keepdims=True)
    # Farthest-first seeds expose unusual lighting/color conditions rather
    # than spending all centroids on the densest, common daytime scene.
    mean = vectors.mean(axis=0)
    seeds = [int(np.argmin(vectors @ mean))]
    nearest = 1 - vectors @ vectors[seeds[0]]
    for _ in range(1, count):
        choice = int(np.argmax(nearest))
        seeds.append(choice)
        nearest = np.minimum(nearest, 1 - vectors @ vectors[choice])
    centers = vectors[seeds].copy()
    labels = np.zeros(n, dtype=np.int32)
    for _ in range(6):
        # Bounded matrix size for large datasets; np.matmul handles CPU SIMD.
        for start in range(0, n, 8192):
            stop = min(start + 8192, n)
            labels[start:stop] = np.argmax(vectors[start:stop] @ centers.T, axis=1)
        for cluster in range(count):
            members = vectors[labels == cluster]
            if len(members):
                centers[cluster] = members.mean(axis=0)
                centers[cluster] /= max(np.linalg.norm(centers[cluster]), 1e-8)
    return labels, centers


def _representatives(indices: np.ndarray, vectors: np.ndarray,
                     center: np.ndarray, budget: int) -> set[int]:
    if len(indices) <= budget:
        return set(int(index) for index in indices)
    local = vectors[indices]
    chosen = [int(np.argmax(local @ center))]
    nearest = 1 - local @ local[chosen[0]]
    for _ in range(1, budget):
        pick = int(np.argmax(nearest))
        chosen.append(pick)
        nearest = np.minimum(nearest, 1 - local @ local[pick])
    return {int(indices[index]) for index in chosen}


def select_frames(frames: list[FrameAnalysis], *, n_clusters: int = 12,
                  per_cluster: int | None = None, phash_radius: int = 4,
                  descriptor_similarity: float = 0.995) -> SelectionResult:
    """Select scene-diverse representatives after exact and near de-duplication.

    De-duplication crosses session/split boundaries, so identical footage
    cannot silently enter training and evaluation. Cluster budgets are applied
    *within each split* to retain independent validation/test coverage. A
    caller should pass the complete candidate inventory for its selection
    window; append-only selection needs a global reconciliation pass.
    """
    if n_clusters < 1 or (per_cluster is not None and per_cluster < 1):
        raise ValueError("Cluster counts and budgets must be positive")
    if not 0 <= phash_radius <= 16 or not 0 <= descriptor_similarity <= 1:
        raise ValueError("Invalid near-duplicate thresholds")
    if not frames:
        return SelectionResult((), {}, {}, {}, ())
    if len({frame.image_id for frame in frames}) != len(frames):
        raise ValueError("Duplicate image IDs in selection input")
    versions = {frame.descriptor_version for frame in frames}
    if versions != {DESCRIPTOR_VERSION}:
        raise ValueError("Mixed or unsupported descriptor versions")
    session_splits: dict[tuple[str, str], str] = {}
    for frame in frames:
        if frame.split not in {"train", "validation", "test"}:
            raise ValueError("Unknown split")
        key = (frame.source, frame.session_id)
        previous = session_splits.setdefault(key, frame.split)
        if previous != frame.split:
            raise ValueError(f"Capture session spans multiple splits: {key}")
    ordered = sorted(frames, key=lambda frame: frame.image_id)
    _normalized_matrix(ordered)  # Validate descriptors before any decisions.
    exact: dict[str, str] = {}
    sha_owner: dict[str, str] = {}
    unique: list[FrameAnalysis] = []
    for frame in ordered:
        owner = sha_owner.get(frame.file_sha256)
        if owner is None:
            sha_owner[frame.file_sha256] = frame.image_id
            unique.append(frame)
        else:
            exact[frame.image_id] = owner
    matrix = _normalized_matrix(unique)
    tree = _HammingTree()
    kept: list[FrameAnalysis] = []
    kept_vectors: dict[str, np.ndarray] = {}
    near: dict[str, str] = {}
    for frame, vector in zip(unique, matrix):
        matched = next((other for other in tree.neighbors(frame.phash, phash_radius)
                        if float(vector @ kept_vectors[other]) >= descriptor_similarity), None)
        if matched is not None:
            near[frame.image_id] = matched
        else:
            kept.append(frame)
            kept_vectors[frame.image_id] = vector
            tree.add(frame.phash, frame.image_id)
    clusters: dict[str, str] = {}
    selected: set[str] = set()
    for split in ("train", "validation", "test"):
        group = [frame for frame in kept if frame.split == split]
        if not group:
            continue
        vectors = _normalized_matrix(group)
        labels, centers = _clusters(vectors, n_clusters)
        for label in range(len(centers)):
            indices = np.flatnonzero(labels == label)
            if not len(indices):
                continue
            for index in indices:
                clusters[group[int(index)].image_id] = f"{split}:{label}"
            budget = len(indices) if per_cluster is None else per_cluster
            selected.update(group[index].image_id for index in
                            _representatives(indices, vectors, centers[label], budget))
    overflow = tuple(sorted(frame.image_id for frame in kept if frame.image_id not in selected))
    return SelectionResult(tuple(sorted(selected)), exact, near, clusters, overflow)
