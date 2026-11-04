"""SAM 2.1 (Apache-2.0) refinement and video propagation of lane-marking masks.

Refinement prompts SAM 2 per connected lane component with its box + points sampled along it, and
keeps the refined component only if it stays plausible (limited growth, enough overlap with the
candidate). Whether refinement helps overall is measured by `bcdata evaluate --refine`, not assumed.
"""
from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

from .base import amp_context

log = logging.getLogger(__name__)


def component_prompts(mask: np.ndarray, min_area: int = 30, points_per_component: int = 5):
    """Yield (component_mask, box[x0,y0,x1,y1], points Nx2) for each lane component."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < min_area:
            continue
        comp = labels == i
        ys, xs = np.nonzero(comp)
        order = np.argsort(ys)
        idx = np.linspace(0, len(order) - 1, min(points_per_component, len(order))).astype(int)
        pts = np.stack([xs[order[idx]], ys[order[idx]]], 1).astype(np.float32)
        yield comp, np.array([x, y, x + w, y + h], np.float32), pts


class Sam2Refiner:
    license_ids = ["sam2"]

    def __init__(self, settings):
        self.settings = settings
        self.cfg = settings.get("teachers.sam2_refine", {})
        self.predictor = None

    def load(self) -> None:
        import torch
        from sam2.build_sam import build_sam2  # type: ignore
        from sam2.sam2_image_predictor import SAM2ImagePredictor  # type: ignore
        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        ckpt = Path(self.cfg.get("checkpoint", "models/sam2/sam2.1_hiera_large.pt"))
        ckpt = ckpt if ckpt.is_absolute() else self.settings.root / ckpt
        self.predictor = SAM2ImagePredictor(build_sam2(self.cfg.get("model_cfg", "configs/sam2.1/sam2.1_hiera_l.yaml"),
                                                       str(ckpt), device=self.device))

    def unload(self) -> None:
        self.predictor = None
        if getattr(self, "device", "cpu") == "cuda":
            self.torch.cuda.empty_cache()

    def refine(self, rgb: np.ndarray, lane_mask: np.ndarray) -> tuple[np.ndarray, dict]:
        growth = float(self.cfg.get("max_area_growth", 1.6))
        min_iou = float(self.cfg.get("min_iou_with_candidate", 0.3))
        out = np.zeros_like(lane_mask, dtype=bool)
        stats = {"components": 0, "refined": 0, "kept_original": 0}
        with self.torch.inference_mode(), amp_context(self.torch, self.device, True):
            self.predictor.set_image(rgb)
            for comp, box, pts in component_prompts(lane_mask > 0):
                stats["components"] += 1
                masks, scores, _ = self.predictor.predict(point_coords=pts, point_labels=np.ones(len(pts), np.int32),
                                                          box=box, multimask_output=False)
                cand = np.asarray(masks[0]) > 0
                inter, union = (cand & comp).sum(), (cand | comp).sum()
                iou = inter / union if union else 0.0
                if cand.sum() <= growth * comp.sum() and iou >= min_iou:
                    out |= cand
                    stats["refined"] += 1
                else:
                    out |= comp
                    stats["kept_original"] += 1
        return out.astype(np.float32), stats


class Sam2RefineTeacher:
    """Wraps the refiner as a teacher named 'sam2_refine': it refines another teacher's lane mask
    (teachers.sam2_refine.base, default comma_segnet). Its fusion weight stays 0 until `bcdata evaluate`
    shows it beats its base on the evaluation set."""
    name = "sam2_refine"
    license_ids = ["sam2"]
    needs_gpu = True

    def __init__(self, settings, registry):
        self.settings, self.registry = settings, registry
        self.cfg = settings.get("teachers.sam2_refine", {}) or {}
        self.refiner = Sam2Refiner(settings)

    @property
    def version(self) -> str:
        import hashlib
        import json
        ck = Path(self.cfg.get("checkpoint", ""))
        return "sam2_refine:" + hashlib.sha1(json.dumps([str(ck.name), self.cfg], sort_keys=True, default=str).encode()).hexdigest()[:12]

    def checkpoint_sha(self) -> str:
        return Path(self.cfg.get("checkpoint", "")).name or "unknown"

    def load(self) -> None:
        self.refiner.load()

    def unload(self) -> None:
        self.refiner.unload()

    def predict(self, rgb: np.ndarray, row):
        from .base import TeacherOutput, load_output
        base = self.cfg.get("base", "comma_segnet")
        ann = self.registry.annotation(row["image_id"], "teacher", base)
        if ann is None:
            return TeacherOutput()
        base_out = load_output(ann["path"])
        if base_out.lane is None:
            return TeacherOutput()
        refined, stats = self.refiner.refine(rgb, base_out.lane >= 0.5)
        return TeacherOutput(lane=refined, meta={"base": base, "base_version": ann["model_version"], **stats})


def propagate_video(settings, frame_paths: list[Path], key_index: int, key_mask: np.ndarray) -> dict[int, np.ndarray]:
    """Track the lane components of one labelled frame through neighbouring frames (temporal consistency)."""
    import torch
    from sam2.build_sam import build_sam2_video_predictor  # type: ignore
    cfg = settings.get("teachers.sam2_refine", {})
    ckpt = Path(cfg.get("checkpoint", "models/sam2/sam2.1_hiera_large.pt"))
    ckpt = ckpt if ckpt.is_absolute() else settings.root / ckpt
    device = "cuda" if torch.cuda.is_available() else "cpu"
    predictor = build_sam2_video_predictor(cfg.get("model_cfg", "configs/sam2.1/sam2.1_hiera_l.yaml"), str(ckpt), device=device)
    import tempfile
    from ..quality import read_rgb, write_jpg
    results: dict[int, np.ndarray] = {}
    tmp = tempfile.TemporaryDirectory(prefix="sam2_frames_")
    folder = Path(tmp.name)
    for i, p in enumerate(frame_paths):            # SAM 2 reads a folder of JPEGs named 00000.jpg, 00001.jpg...
        write_jpg(folder / f"{i:05d}.jpg", read_rgb(p), quality=95)
    with tmp, torch.inference_mode(), amp_context(torch, device, True):
        state = predictor.init_state(video_path=str(folder))
        for obj_id, (comp, _, _) in enumerate(component_prompts(key_mask > 0), start=1):
            predictor.add_new_mask(state, frame_idx=key_index, obj_id=obj_id, mask=comp)
        for frame_idx, obj_ids, mask_logits in predictor.propagate_in_video(state):
            merged = (mask_logits > 0).any(dim=0).squeeze().cpu().numpy()
            results[frame_idx] = merged.astype(np.float32)
    return results
