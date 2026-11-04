"""Image embeddings for near-duplicate removal and diversity clustering.

`dinov3` uses the gated Hugging Face checkpoint on GPU (CLS token + mean patch token, L2-normalised).
`cheap` is a deterministic CPU fallback (colour histogram + gradient-orientation histogram + tiny thumbnail):
much weaker, but enough to run and test the pipeline anywhere. The backend name is stored with the vectors,
so the two are never mixed.
"""
from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

from .quality import read_rgb

log = logging.getLogger(__name__)


def cheap_embedding(rgb: np.ndarray) -> np.ndarray:
    small = cv2.resize(rgb, (160, 120), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV)
    hist = cv2.calcHist([hsv], [0, 1, 2], None, [8, 4, 4], [0, 180, 0, 256, 0, 256]).flatten()
    hist /= hist.sum() + 1e-6
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY).astype(np.float32)
    gx, gy = cv2.Sobel(gray, cv2.CV_32F, 1, 0), cv2.Sobel(gray, cv2.CV_32F, 0, 1)
    mag, ang = cv2.cartToPolar(gx, gy)
    feats = [hist]
    for band in (slice(0, 60), slice(60, 120)):            # sky/scenery vs road half
        h, _ = np.histogram(ang[band] % np.pi, bins=12, range=(0, np.pi), weights=mag[band])
        feats.append(h / (h.sum() + 1e-6))
    thumb = cv2.resize(gray, (16, 12), interpolation=cv2.INTER_AREA).flatten()
    thumb = (thumb - thumb.mean()) / (thumb.std() + 1e-6) * 0.05
    v = np.concatenate(feats + [thumb]).astype(np.float32)
    return v / (np.linalg.norm(v) + 1e-8)


class Dinov3Embedder:
    def __init__(self, model_id: str, image_size: int = 448, device: str | None = None):
        import torch
        from transformers import AutoModel
        self.torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model = AutoModel.from_pretrained(model_id, torch_dtype=torch.float16 if self.device == "cuda" else torch.float32)
        self.model.to(self.device).eval()
        self.size = image_size - image_size % 16
        self.mean = torch.tensor([0.485, 0.456, 0.406], device=self.device).view(1, 3, 1, 1)
        self.std = torch.tensor([0.229, 0.224, 0.225], device=self.device).view(1, 3, 1, 1)
        self.n_reg = int(getattr(self.model.config, "num_register_tokens", 0))

    def __call__(self, rgbs: list[np.ndarray]) -> np.ndarray:
        torch = self.torch
        batch = np.stack([cv2.resize(r, (self.size * 2, self.size), interpolation=cv2.INTER_AREA) for r in rgbs])
        x = torch.from_numpy(batch).to(self.device).permute(0, 3, 1, 2).float() / 255.0
        x = ((x - self.mean) / self.std).to(next(self.model.parameters()).dtype)
        with torch.inference_mode():
            hidden = self.model(pixel_values=x).last_hidden_state
        cls = hidden[:, 0]
        patches = hidden[:, 1 + self.n_reg:]
        v = torch.cat([cls, patches.mean(1)], dim=1).float()
        v = torch.nn.functional.normalize(v, dim=1)
        return v.cpu().numpy()

    def close(self) -> None:
        del self.model
        if self.device == "cuda":
            self.torch.cuda.empty_cache()


def resolve_backend(name: str) -> str:
    if name != "auto":
        return name
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
        return "dinov3" if torch.cuda.is_available() else "cheap"
    except ImportError:
        return "cheap"


def embed_paths(paths: dict[str, Path], backend: str, cache_file: Path, model_id: str = "",
                image_size: int = 448, batch_size: int = 16) -> tuple[list[str], np.ndarray, str]:
    """Embed images (id -> path), reusing vectors already in cache_file for the same backend."""
    backend = resolve_backend(backend)
    cache: dict[str, np.ndarray] = {}
    if cache_file.exists():
        data = np.load(cache_file, allow_pickle=False)
        if str(data["backend"]) == backend:
            cache = dict(zip(data["ids"].tolist(), data["vectors"]))
    todo = [i for i in paths if i not in cache]
    if todo:
        log.info("embedding %d images with %s", len(todo), backend)
        if backend == "cheap":
            for i in todo:
                rgb = read_rgb(paths[i])
                if rgb is not None:
                    cache[i] = cheap_embedding(rgb)
        else:
            emb = Dinov3Embedder(model_id, image_size)
            try:
                for s in range(0, len(todo), batch_size):
                    chunk = [i for i in todo[s:s + batch_size]]
                    imgs = [read_rgb(paths[i]) for i in chunk]
                    keep = [(i, im) for i, im in zip(chunk, imgs) if im is not None]
                    if keep:
                        vecs = emb([im for _, im in keep])
                        for (i, _), v in zip(keep, vecs):
                            cache[i] = v
            finally:
                emb.close()
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        ids = sorted(cache)
        np.savez(cache_file, ids=np.array(ids), vectors=np.stack([cache[i] for i in ids]), backend=np.array(backend))
    ids = [i for i in paths if i in cache]
    return ids, (np.stack([cache[i] for i in ids]) if ids else np.zeros((0, 1), np.float32)), backend
