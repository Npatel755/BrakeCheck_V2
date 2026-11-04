"""Teacher interface and the runner that executes one teacher at a time over a set of images.

Every teacher returns per-pixel probabilities in [0, 1] at the image's own resolution:
  lane  - painted lane markings
  road  - drivable road surface (including the paint on it)
  extra - optional named maps (e.g. 'movable', 'my_car', or per-prompt SAM masks) kept for reviewers
Outputs are saved as 8-bit PNGs and registered with the exact model version + checkpoint hash.
Only one teacher is resident on the GPU at a time; `unload()` frees it before the next starts.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np

from ..config import Settings
from ..db import Registry
from ..licenses import LicenseRegistry
from ..quality import read_gray_u8, read_rgb, write_png

log = logging.getLogger(__name__)


@dataclass
class TeacherOutput:
    lane: np.ndarray | None = None
    road: np.ndarray | None = None
    extra: dict[str, np.ndarray] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)


class Teacher:
    name = "teacher"
    license_ids: list[str] = []
    needs_gpu = False

    def __init__(self, settings: Settings):
        self.settings = settings
        self.cfg = settings.get(f"teachers.{self.name}", {}) or {}

    @property
    def version(self) -> str:
        """Identifies the exact weights + preprocessing. Changing either makes a new version."""
        return f"{self.name}:{self.checkpoint_sha()[:12]}:{_cfg_hash(self.cfg)}"

    def checkpoint_sha(self) -> str:
        return "none"

    def load(self) -> None: ...

    def unload(self) -> None: ...

    def predict(self, rgb: np.ndarray, row) -> TeacherOutput:
        raise NotImplementedError


def amp_context(torch, device: str, enabled: bool):
    """bf16/fp16 autocast on CUDA, a no-op elsewhere."""
    import contextlib
    if enabled and device == "cuda":
        return torch.autocast("cuda", dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16)
    return contextlib.nullcontext()


def _cfg_hash(cfg: dict) -> str:
    return hashlib.sha1(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()[:8]


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def to_u8(prob: np.ndarray) -> np.ndarray:
    return np.clip(np.round(prob * 255.0), 0, 255).astype(np.uint8)


def from_u8(img: np.ndarray) -> np.ndarray:
    return img.astype(np.float32) / 255.0


def output_dir(settings: Settings, teacher: str, version: str) -> Path:
    safe = version.replace(":", "_")
    return settings.path("annotations", "teacher_predictions", teacher, safe)


def save_output(settings: Settings, teacher: str, version: str, image_id: str, out: TeacherOutput) -> dict[str, str]:
    d = output_dir(settings, teacher, version)
    paths = {}
    for key, arr in (("lane", out.lane), ("road", out.road), *out.extra.items()):
        if arr is None:
            continue
        p = d / f"{image_id}_{key}.png"
        write_png(p, to_u8(arr))
        paths[key] = str(p)
    meta_path = d / f"{image_id}.json"
    meta_path.write_text(json.dumps({"maps": paths, "meta": out.meta}, default=str))
    paths["_meta"] = str(meta_path)
    return paths


def load_output(meta_path: str | Path) -> TeacherOutput:
    info = json.loads(Path(meta_path).read_text())
    maps = {k: from_u8(read_gray_u8(p)) for k, p in info["maps"].items() if not k.startswith("_")}
    return TeacherOutput(maps.pop("lane", None), maps.pop("road", None), maps, info.get("meta", {}))


def run_teacher(settings: Settings, registry: Registry, licenses: LicenseRegistry, teacher: Teacher,
                rows: Iterable, force: bool = False, allow_unverified: bool = False) -> dict:
    for lic in teacher.license_ids:
        licenses.check_model(lic, allow_unverified=allow_unverified)
    rows = list(rows)
    version = teacher.version
    todo = [r for r in rows if force or not registry.has_annotation(r["image_id"], "teacher", teacher.name, version)]
    todo = [r for r in todo if not r["locked"]]          # reviewed images keep their human labels
    counts = {"teacher": teacher.name, "version": version, "images": len(rows), "todo": len(todo), "done": 0, "failed": 0}
    if not todo:
        return counts
    t0 = time.time()
    teacher.load()
    try:
        for r in todo:
            rgb = read_rgb(r["path"])
            if rgb is None:
                counts["failed"] += 1
                continue
            try:
                out = teacher.predict(rgb, r)
            except Exception as exc:
                log.exception("%s failed on %s: %s", teacher.name, r["image_id"], exc)
                counts["failed"] += 1
                continue
            if out.lane is None and out.road is None:
                counts["failed"] += 1
                continue
            paths = save_output(settings, teacher.name, version, r["image_id"], out)
            registry.add_annotation(r["image_id"], "teacher", teacher.name, version, paths["_meta"],
                                    license_ids=[r["license_id"], *teacher.license_ids],
                                    checkpoint_sha=teacher.checkpoint_sha(), params={"maps": list(paths)})
            counts["done"] += 1
    finally:
        teacher.unload()
    dt = time.time() - t0
    counts["seconds"] = round(dt, 1)
    counts["images_per_second"] = round(counts["done"] / dt, 3) if dt > 0 else None
    return counts
