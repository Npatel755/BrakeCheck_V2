"""commaai/comma10k-segnet (MIT): U-Net / efficientnet-b2, 5 classes in comma10k order.

Preprocessing follows the model's own albumentations_config_eval.json: resize to 512x384, ToTensorV2,
no normalisation (pixel values 0-255). Class index i = comma10k class i+1:
0 road, 1 lane markings, 2 undrivable, 3 movable, 4 my car.
"""
from __future__ import annotations

import cv2
import numpy as np

from .base import Teacher, TeacherOutput, amp_context


class CommaSegnetTeacher(Teacher):
    name = "comma_segnet"
    license_ids = ["comma_segnet"]

    def __init__(self, settings):
        super().__init__(settings)
        self.model = None
        self._sha = None

    def checkpoint_sha(self) -> str:
        if self.cfg.get("revision"):
            return self.cfg["revision"]
        if self._sha is None:
            try:
                from huggingface_hub import HfApi
                info = HfApi().model_info(self.cfg.get("hub_id", "commaai/comma10k-segnet"))
                self._sha = info.sha or "unknown"
            except Exception:
                self._sha = "unknown"
        return self._sha

    def load(self) -> None:
        import segmentation_models_pytorch as smp
        import torch
        self.torch = torch
        dev = self.settings.get("teachers.device", "auto")
        self.device = ("cuda" if torch.cuda.is_available() else "cpu") if dev == "auto" else dev
        options = {"revision": self.cfg["revision"]} if self.cfg.get("revision") else {}
        self.model = smp.from_pretrained(self.cfg.get("hub_id", "commaai/comma10k-segnet"), **options).to(self.device).eval()

    def unload(self) -> None:
        if self.model is not None:
            del self.model
            self.model = None
            if getattr(self, "device", "cpu") == "cuda":
                self.torch.cuda.empty_cache()

    def predict(self, rgb: np.ndarray, row) -> TeacherOutput:
        torch = self.torch
        h, w = rgb.shape[:2]
        iw, ih = int(self.cfg.get("input_width", 512)), int(self.cfg.get("input_height", 384))
        x = cv2.resize(rgb, (iw, ih), interpolation=cv2.INTER_LINEAR)
        t = torch.from_numpy(x).permute(2, 0, 1)[None].float().to(self.device)
        with torch.inference_mode(), amp_context(torch, self.device, self.settings.get("teachers.amp", True)):
            logits = self.model(t)
        prob = torch.softmax(logits.float(), dim=1)[0].cpu().numpy()          # 5 x ih x iw
        prob = np.stack([cv2.resize(p, (w, h), interpolation=cv2.INTER_LINEAR) for p in prob])
        return TeacherOutput(lane=prob[1], road=np.clip(prob[0] + prob[1], 0, 1),
                             extra={"movable": prob[3], "my_car": prob[4]},
                             meta={"input": [iw, ih]})
