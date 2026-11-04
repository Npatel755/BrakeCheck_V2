"""Human or simulator labels that came with the source (comma10k masks, ZOD polygons, CARLA ground truth).

These are references, not guesses: fusion treats them as the label for their image, and the other
teachers' agreement with them becomes each teacher's measured error on that image.
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from ..quality import read_gray_u8, read_rgb
from ..sources.comma10k import LANE, MOVABLE, MY_CAR, ROAD, mask_to_classes
from .base import Teacher, TeacherOutput

CLASS_UNKNOWN, CLASS_ROAD, CLASS_LANE, CLASS_UNDRIVABLE, CLASS_MOVABLE, CLASS_MY_CAR = 0, 1, 2, 3, 4, 5


def load_classes(ann_row) -> np.ndarray:
    """Return a class map in the shared taxonomy (0 unknown, 1 road, 2 lane, 3 undrivable, 4 movable, 5 my car)."""
    params = json.loads(ann_row["params"] or "{}")
    fmt = params.get("format")
    if fmt == "comma10k_rgb":
        rgb = read_rgb(ann_row["path"])
        if rgb is None:
            raise IOError(ann_row["path"])
        return mask_to_classes(rgb)
    if fmt == "a2d2_rgb":
        from ..sources.a2d2 import mask_to_classes as a2d2_classes
        rgb = read_rgb(ann_row["path"])
        if rgb is None:
            raise IOError(ann_row["path"])
        palette = json.loads(Path(params["palette_path"]).read_text(encoding="utf-8"))
        return a2d2_classes(rgb, palette=palette)
    if fmt == "class_map":
        m = read_gray_u8(ann_row["path"])
        if m is None:
            raise IOError(ann_row["path"])
        return m if m.ndim == 2 else m[:, :, 0]
    raise ValueError(f"unknown source label format {fmt!r}")


class SourceLabelTeacher(Teacher):
    name = "source_labels"
    license_ids: list[str] = []

    def __init__(self, settings, registry):
        super().__init__(settings)
        self.registry = registry

    @property
    def version(self) -> str:
        return "source_labels:v1"

    def predict(self, rgb: np.ndarray, row) -> TeacherOutput:
        ann = self.registry.annotation(row["image_id"], "source_label")
        if ann is None:
            return TeacherOutput()
        cls = load_classes(ann)
        h, w = rgb.shape[:2]
        if cls.shape != (h, w):
            cls = cv2.resize(cls, (w, h), interpolation=cv2.INTER_NEAREST)
        lane = (cls == LANE).astype(np.float32)
        road = ((cls == ROAD) | (cls == LANE)).astype(np.float32)
        extra = {}
        if (cls == MOVABLE).any():
            extra["movable"] = (cls == MOVABLE).astype(np.float32)
        if (cls == MY_CAR).any():
            extra["my_car"] = (cls == MY_CAR).astype(np.float32)
        return TeacherOutput(lane, road, extra, {"producer": ann["producer"], "label_version": ann["model_version"],
                                                 "human": json.loads(ann["params"] or "{}").get("human", False)})
