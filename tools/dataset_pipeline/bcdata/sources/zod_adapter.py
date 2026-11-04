"""ZOD adapter (CC BY-SA 4.0 data). Works without the dataset: nothing runs until `dataset_root` exists.

Imports frames with their original human lane-marking and ego-road annotations as `source_label`
annotations (they are never overwritten by model output) into the separate `share_alike` pool.

The ZOD devkit's object names are read defensively (`geometry` as a shapely polygon or a point list),
because the exact annotation classes should be confirmed against the devkit version you install.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np

from ..config import Settings
from ..db import Registry
from ..licenses import LicenseRegistry
from ..quality import write_png
from .base import register_image

log = logging.getLogger(__name__)

ZOD_NOTICE = ("Zenseact Open Dataset (ZOD) - Copyright Zenseact AB. Licensed under CC BY-SA 4.0. "
              "Any public use, distribution or display of this dataset must contain this notice in full.")


def polygon_points(geometry: Any) -> list[np.ndarray]:
    """Shapely Polygon/MultiPolygon, an object with .points, or a plain list -> list of Nx2 float arrays."""
    if geometry is None:
        return []
    if hasattr(geometry, "geoms"):
        return [p for g in geometry.geoms for p in polygon_points(g)]
    if hasattr(geometry, "exterior"):
        return [np.asarray(geometry.exterior.coords, dtype=np.float64)[:, :2]]
    if hasattr(geometry, "points"):
        return [np.asarray(geometry.points, dtype=np.float64)[:, :2]]
    arr = np.asarray(geometry, dtype=np.float64)
    if arr.ndim == 2 and arr.shape[1] >= 2:
        return [arr[:, :2]]
    return []


def rasterize(annotations: Iterable[Any], shape: tuple[int, int]) -> np.ndarray:
    mask = np.zeros(shape, np.uint8)
    for ann in annotations or []:
        for poly in polygon_points(getattr(ann, "geometry", ann)):
            if len(poly) >= 3:
                cv2.fillPoly(mask, [np.round(poly).astype(np.int32)], 1)
    return mask


def ingest(settings: Settings, registry: Registry, licenses: LicenseRegistry, limit: int | None = None) -> dict:
    cfg = settings.get("sources.zod", {})
    root = Path(cfg.get("dataset_root") or "")
    if not root.exists():
        raise FileNotFoundError("ZOD dataset_root not set or missing. Apply for access at the ZOD site, run "
                                "`zod download --subset=frames`, then set sources.zod.dataset_root.")
    try:
        from zod import ZodFrames  # type: ignore
        from zod.constants import AnnotationProject, Anonymization  # type: ignore
    except ImportError as exc:
        raise RuntimeError('pip install "zod[all]" first') from exc
    frames = ZodFrames(dataset_root=str(root), version=cfg.get("version", "full"))
    out = settings.path("raw", "zod")
    counts = {"frames": 0, "added": 0, "duplicate": 0, "rejected": 0, "no_lane_annotation": 0}
    (out / "NOTICE.txt").parent.mkdir(parents=True, exist_ok=True)
    (out / "NOTICE.txt").write_text(ZOD_NOTICE + "\n", encoding="utf-8")
    for n, frame_id in enumerate(sorted(frames.get_all_ids())):
        if limit and n >= limit:
            break
        frame = frames[frame_id]
        img = frame.get_image(Anonymization.BLUR)
        try:
            lanes = frame.get_annotation(AnnotationProject.LANE_MARKINGS)
        except Exception:
            lanes = None
        if not lanes:
            counts["no_lane_annotation"] += 1
            continue
        try:
            ego_road = frame.get_annotation(AnnotationProject.EGO_ROAD)
        except Exception:
            ego_road = None
        h, w = img.shape[:2]
        classes = np.zeros((h, w), np.uint8)
        classes[rasterize(ego_road, (h, w)) > 0] = 1
        classes[rasterize(lanes, (h, w)) > 0] = 2
        img_path = out / "images" / f"{frame_id}.png"
        write_png(img_path, cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
        cls_path = out / "labels" / f"{frame_id}_classes.png"
        write_png(cls_path, classes)
        res = register_image(settings, registry, licenses, img_path, source="zod", session_id=f"zod_{frame_id}",
                             source_ref=f"zod:frames:{frame_id}")
        counts["frames"] += 1
        counts[res.status] += 1
        if res.status == "added":
            registry.update_image(res.image_id, has_source_label=1)
            registry.add_annotation(res.image_id, "source_label", "zod", cfg.get("version", "full"), str(cls_path),
                                    license_ids=["zod"], params={"format": "class_map", "human": True,
                                                                 "ego_road": ego_road is not None})
    return counts
