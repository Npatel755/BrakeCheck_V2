"""SAM 3 (SAM License) text-prompted segmentation. Outputs are candidates, never verified lanes.

Each configured prompt runs separately; masks scoring above the threshold are merged as
score-weighted maxima. Per-prompt maps are saved too, so reviewers and `bcdata sam3-prompts`
can see which wording actually finds paint.
"""
from __future__ import annotations

import re

import cv2
import numpy as np

from .base import Teacher, TeacherOutput, amp_context


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def masks_to_prob(masks, scores, shape: tuple[int, int], threshold: float) -> np.ndarray:
    """SAM 3 output (N masks + N scores, tensors or arrays) -> HxW max(score * mask)."""
    h, w = shape
    prob = np.zeros((h, w), np.float32)
    if masks is None:
        return prob
    m = masks.detach().float().cpu().numpy() if hasattr(masks, "detach") else np.asarray(masks, np.float32)
    s = scores.detach().float().cpu().numpy() if hasattr(scores, "detach") else np.asarray(scores, np.float32)
    m = m.reshape(-1, *m.shape[-2:]) if m.size else np.zeros((0, h, w), np.float32)
    s = s.reshape(-1)
    for mask, score in zip(m, s):
        if score < threshold:
            continue
        if mask.shape != (h, w):
            mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_LINEAR)
        prob = np.maximum(prob, float(score) * (mask > 0.5).astype(np.float32))
    return prob


class Sam3Teacher(Teacher):
    name = "sam3"
    license_ids = ["sam3"]
    needs_gpu = True

    def __init__(self, settings):
        super().__init__(settings)
        self.model = self.processor = None

    def checkpoint_sha(self) -> str:
        try:
            from huggingface_hub import HfApi
            return HfApi().model_info("facebook/sam3").sha or "unknown"
        except Exception:
            return "unknown"

    def load(self) -> None:
        import torch
        from sam3.model.sam3_image_processor import Sam3Processor  # type: ignore
        from sam3.model_builder import build_sam3_image_model  # type: ignore
        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = build_sam3_image_model()
        self.processor = Sam3Processor(self.model)

    def unload(self) -> None:
        self.model = self.processor = None
        if getattr(self, "device", "cpu") == "cuda":
            self.torch.cuda.empty_cache()

    def predict(self, rgb: np.ndarray, row) -> TeacherOutput:
        from PIL import Image
        h, w = rgb.shape[:2]
        thr = float(self.cfg.get("score_threshold", 0.35))
        extra, groups = {}, {}
        with self.torch.inference_mode(), amp_context(self.torch, self.device, self.settings.get("teachers.amp", True)):
            state = self.processor.set_image(Image.fromarray(rgb))
            for group in ("lane_prompts", "road_prompts", "boundary_prompts"):
                maps = []
                for prompt in self.cfg.get(group, []) or []:
                    out = self.processor.set_text_prompt(state=state, prompt=prompt)
                    p = masks_to_prob(out.get("masks"), out.get("scores"), (h, w), thr)
                    extra[f"sam3_{_slug(prompt)}"] = p
                    maps.append(p)
                groups[group] = np.max(maps, axis=0) if maps else None
        return TeacherOutput(lane=groups.get("lane_prompts"), road=groups.get("road_prompts"), extra=extra,
                             meta={"threshold": thr, "prompts": {g: self.cfg.get(g, []) for g in groups}})
