"""DINOv3 segmentation teacher: frozen DINOv3 backbone + a trained decoder.

DINOv3 alone is not a lane segmenter. This module trains a decoder on human-labelled source masks
(comma10k now, ZOD later in its own pool) and refuses to produce pseudo-labels until a checkpoint with
real training steps and validation metrics exists.

Decoder: features from 4 backbone layers (stride 16) are projected, fused and upsampled, then merged
with a small full-resolution RGB "detail" branch, because painted lines are often only 2-6 px wide and
a 16 px patch grid alone smears them.

Classes follow the comma10k taxonomy: 0 unknown (ignored in the loss), 1 road, 2 lane marking,
3 undrivable, 4 movable, 5 my car.
"""
from __future__ import annotations

import json
import logging
import math
import random
import time
from pathlib import Path

import cv2
import numpy as np

from ..quality import read_rgb
from .base import Teacher, TeacherOutput, amp_context, file_sha256

log = logging.getLogger(__name__)
NUM_CLASSES = 6
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)


def _torch():
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    return torch, nn, F


def build_model(backbone_id: str, num_classes: int = NUM_CLASSES, *, revision=None,
                device=None, precision=None, cache_dir=None):
    torch, nn, F = _torch()

    class HFBackbone(nn.Module):
        """Hugging Face DINOv3 ViT; returns 4 intermediate layers as (B, C, H/16, W/16)."""
        def __init__(self, model_id: str):
            super().__init__()
            from transformers import AutoModel
            options = {"revision": revision, "cache_dir": cache_dir}
            if precision == "bfloat16":
                options["dtype"] = torch.bfloat16
            if device == "cuda":
                # Stream shards straight to the GPU; avoid a full FP32 copy
                # in either the 32-GB system RAM or the 24-GB device memory.
                options["device_map"] = {"": "cuda:0"}
            self.model = AutoModel.from_pretrained(model_id, **options)
            cfg = self.model.config
            self.patch = int(getattr(cfg, "patch_size", 16))
            self.n_reg = int(getattr(cfg, "num_register_tokens", 0))
            n_layers = int(getattr(cfg, "num_hidden_layers", 24))
            self.layers = [n_layers // 4, n_layers // 2, 3 * n_layers // 4, n_layers]
            self.channels = [int(cfg.hidden_size)] * 4
            for p in self.model.parameters():
                p.requires_grad = False

        def forward(self, x):
            B, _, H, W = x.shape
            with torch.no_grad():
                out = self.model(pixel_values=x, output_hidden_states=True)
            h, w = H // self.patch, W // self.patch
            feats = []
            for i in self.layers:
                tok = out.hidden_states[i][:, 1 + self.n_reg:]
                feats.append(tok.transpose(1, 2).reshape(B, -1, h, w).float())
            return feats

    class TinyBackbone(nn.Module):
        """Untrained conv stand-in used only by tests and CPU smoke runs ('test:tiny')."""
        def __init__(self):
            super().__init__()
            self.patch = 16
            self.stem = nn.Sequential(nn.Conv2d(3, 16, 4, 4), nn.ReLU(), nn.Conv2d(16, 32, 4, 4), nn.ReLU())
            self.blocks = nn.ModuleList([nn.Conv2d(32, 32, 3, padding=1) for _ in range(4)])
            self.channels = [32] * 4

        def forward(self, x):
            f = self.stem(x)
            feats = []
            for b in self.blocks:
                f = torch.relu(b(f))
                feats.append(f)
            return feats

    class Decoder(nn.Module):
        def __init__(self, channels, num_classes, width=256):
            super().__init__()
            self.proj = nn.ModuleList([nn.Sequential(nn.Conv2d(c, width, 1), nn.BatchNorm2d(width), nn.GELU()) for c in channels])
            self.fuse = nn.Sequential(nn.Conv2d(width * len(channels), width, 3, padding=1), nn.BatchNorm2d(width), nn.GELU())
            self.up1 = nn.Sequential(nn.Conv2d(width, 128, 3, padding=1), nn.BatchNorm2d(128), nn.GELU())   # /8
            self.up2 = nn.Sequential(nn.Conv2d(128, 64, 3, padding=1), nn.BatchNorm2d(64), nn.GELU())      # /4
            self.detail = nn.Sequential(                                                                  # RGB -> /4
                nn.Conv2d(3, 32, 3, stride=2, padding=1), nn.BatchNorm2d(32), nn.GELU(),
                nn.Conv2d(32, 64, 3, stride=2, padding=1), nn.BatchNorm2d(64), nn.GELU())
            self.head = nn.Sequential(nn.Conv2d(128, 64, 3, padding=1), nn.BatchNorm2d(64), nn.GELU(),
                                      nn.Conv2d(64, num_classes, 1))

        def forward(self, feats, image):
            size = feats[-1].shape[-2:]
            x = torch.cat([F.interpolate(p(f), size=size, mode="bilinear", align_corners=False)
                           for p, f in zip(self.proj, feats)], 1)
            x = self.fuse(x)
            x = self.up1(F.interpolate(x, scale_factor=2, mode="bilinear", align_corners=False))
            x = self.up2(F.interpolate(x, scale_factor=2, mode="bilinear", align_corners=False))
            d = self.detail(image)
            x = F.interpolate(x, size=d.shape[-2:], mode="bilinear", align_corners=False)
            logits = self.head(torch.cat([x, d], 1))
            return F.interpolate(logits, size=image.shape[-2:], mode="bilinear", align_corners=False)

    class SegModel(nn.Module):
        def __init__(self):
            super().__init__()
            self.backbone = TinyBackbone() if backbone_id == "test:tiny" else HFBackbone(backbone_id)
            for parameter in self.backbone.parameters():
                parameter.requires_grad = False
            self.decoder = Decoder(self.backbone.channels, num_classes)

        def forward(self, x):
            return self.decoder(self.backbone(x), x)

    return SegModel()


def preprocess(rgb: np.ndarray, width: int, height: int):
    torch, _, _ = _torch()
    x = cv2.resize(rgb, (width, height), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    x = (x - MEAN) / STD
    return torch.from_numpy(x).permute(2, 0, 1)


# ---- training ----------------------------------------------------------------------------------

def _load_pairs(registry, splits: tuple[str, ...], pools: tuple[str, ...]) -> list[tuple[str, object]]:
    from .source_labels import load_classes
    if not splits or any(split not in ("train", "val") for split in splits):
        raise ValueError("Decoder training and model selection use train/val only")
    rows = registry.images(f"has_source_label=1 AND status='curated' AND review_status!='rejected' "
                           f"AND split IN ({','.join('?' * len(splits))}) AND pool IN ({','.join('?' * len(pools))})",
                           [*splits, *pools])
    pairs = []
    for r in rows:
        ann = registry.annotation(r["image_id"], "source_label")
        if ann is not None and json.loads(ann["params"] or "{}").get("human") is True:
            pairs.append((r["path"], lambda a=ann: load_classes(a)))
    return pairs


def iou_per_class(pred: np.ndarray, gt: np.ndarray, classes=(1, 2)) -> dict:
    out = {}
    valid = gt > 0
    for c in classes:
        p, g = (pred == c) & valid, (gt == c)
        union = (p | g).sum()
        out[c] = float((p & g).sum() / union) if union else float("nan")
    return out


def validate_warm_start(path, backbone, revision, width, height):
    """Validate a decoder-only initialization before allocating the 7B model."""
    torch, _, _ = _torch()
    from ..source_mask_training import CLASSES
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    state = torch.load(path, map_location="cpu", weights_only=True)
    if (state.get("backbone") != backbone or state.get("backbone_revision") != revision or
            state.get("input") != [width, height] or state.get("classes") != CLASSES):
        raise ValueError("Warm-start backbone/revision/input/classes mismatch")
    if int(state.get("trained_steps", 0)) <= 0 or not state.get("val_metrics"):
        raise ValueError("Warm-start requires a trained and validated decoder")
    decoder = state.get("decoder")
    if not decoder or any(not torch.is_tensor(v) or not torch.isfinite(v).all() for v in decoder.values()):
        raise ValueError("Warm-start decoder weights are missing or non-finite")
    return state


def train(settings, registry, out_path: Path, backbone_id: str | None = None, epochs: int = 20, batch_size: int = 4,
          lr: float = 3e-4, max_steps: int | None = None, init_from: Path | None = None, extra_pairs=None,
          device: str | None = None, seed: int = 0, progress=None,
          train_limit: int | None = None, val_limit: int | None = None, data_pairs=None, resume_from: Path | None = None,
          evaluate_initial: bool = False, val_sources=None, selection_source=None) -> dict:
    """Train the decoder on source-labelled train images; select on val; save checkpoint + metadata."""
    torch, nn, F = _torch()
    cfg = settings.get("teachers.dinov3_seg", {})
    backbone_id = backbone_id or cfg.get("backbone")
    W, H = int(cfg.get("input_width", 896)), int(cfg.get("input_height", 448))
    if init_from and resume_from:
        raise ValueError("Choose decoder-only warm-start or exact-cohort resume")
    initial = validate_warm_start(init_from, backbone_id, cfg.get("revision"), W, H) if init_from else None
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    random.seed(seed)
    torch.manual_seed(seed)
    pools = tuple(settings.get("export.ufld.pools", ["commercial"]))
    if data_pairs is not None:
        if set(data_pairs) != {"train", "val"} or extra_pairs:
            raise ValueError("A frozen cohort must contain only train and val pairs")
        train_pairs, val_pairs = list(data_pairs["train"]), list(data_pairs["val"])
    else:
        train_pairs = _load_pairs(registry, ("train",), pools) + list(extra_pairs or [])
        val_pairs = _load_pairs(registry, ("val",), pools)
    if train_limit is not None:
        train_pairs = train_pairs[:train_limit]
    if val_limit is not None:
        val_pairs = val_pairs[:val_limit]
        if val_sources is not None:
            val_sources = val_sources[:val_limit]
    if val_sources is not None and len(val_sources) != len(val_pairs):
        raise ValueError("Validation source labels must match the frozen validation pairs")
    if selection_source and (val_sources is None or selection_source not in val_sources):
        raise ValueError("Checkpoint selection source is absent from validation")
    if not train_pairs:
        raise RuntimeError("no source-labelled training images (ingest comma10k and run `bcdata split` first)")
    if not val_pairs:
        raise RuntimeError("A separate source-labelled validation split is required")
    if batch_size < 1 or epochs < 1 or (max_steps is not None and max_steps < 1):
        raise ValueError("Training steps, epochs and batch size must be positive")
    model = build_model(backbone_id, revision=cfg.get("revision"), device=device,
                        precision=cfg.get("precision"), cache_dir=cfg.get("cache_dir")).to(device)
    trainable = [name for name, parameter in model.named_parameters() if parameter.requires_grad]
    if not trainable or any(not name.startswith("decoder.") for name in trainable):
        raise RuntimeError("Only decoder parameters may be trained")
    metadata = {"backbone_revision": cfg.get("revision"), "precision": cfg.get("precision", "float32"),
                "backbone_frozen": True, "trainable_parameters": sum(p.numel() for p in model.parameters() if p.requires_grad),
                "backbone_parameters": sum(p.numel() for p in model.backbone.parameters()),
                "label_source": "original_human_source_masks", "test_images_used": 0, "seed": seed,
                "batch_size": batch_size, "learning_rate": lr,
                "training_cohort_sha256": cfg.get("training_cohort_sha256"),
                "license_sha256": cfg.get("license_sha256"),
                "training_data_license_sha256": cfg.get("training_data_license_sha256"),
                "training_data_licenses": cfg.get("training_data_licenses"),
                "distribution_scope": cfg.get("distribution_scope"), "candidate_only": True, "auto_promote": False,
                "selection_metric": (selection_source + ".lane_iou") if selection_source else "lane_iou",
                "backbone_manifest_sha256": cfg.get("backbone_manifest_sha256")}
    if initial is not None:
        model.decoder.load_state_dict(initial["decoder"], strict=True)
        metadata.update(warm_start_checkpoint=str(Path(init_from).resolve()),
                        warm_start_sha256=file_sha256(Path(init_from)), warm_start_source_steps=int(initial["trained_steps"]),
                        optimizer_restored=False, warm_start_step=0)
        del initial
    params = [p for p in model.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(params, lr=lr, weight_decay=1e-4)
    steps_per_epoch = math.ceil(len(train_pairs) / batch_size)
    total = max_steps or epochs * steps_per_epoch
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(1, total))
    weights = torch.tensor([0, 1.0, 4.0, 1.0, 1.0, 0.5], device=device)      # lane markings are rare and thin
    scaler_enabled = device == "cuda"
    step, best, history = 0, -1.0, []
    if resume_from:
        state = torch.load(resume_from, map_location="cpu", weights_only=True)
        for key in ("backbone_revision", "training_cohort_sha256"):
            if state.get(key) != metadata.get(key):
                raise ValueError("Resume checkpoint mismatch: " + key)
        if state["backbone"] != backbone_id or state["input"] != [W, H]:
            raise ValueError("Resume backbone or input size mismatch")
        model.decoder.load_state_dict(state["decoder"])
        step = int(state["trained_steps"])
        if not 0 <= step < total:
            raise ValueError("Resume checkpoint has already reached the requested total steps")
        history = list(state.get("history", []))
        selection_key = (selection_source + ".lane_iou") if selection_source else "lane_iou"
        if state.get("selection_metric", "lane_iou") != selection_key:
            raise ValueError("Resume checkpoint selection metric mismatch")
        prior_metric = state.get("val_metrics", {})
        prior_score = (prior_metric.get("by_source", {}).get(selection_source, {}).get("lane_iou", -1.0)
                       if selection_source else prior_metric.get("lane_iou", -1.0))
        best = max([item.get("selection_lane_iou", item.get("lane_iou", -1.0)) for item in history] + [prior_score])
        if "optimizer" in state:
            opt.load_state_dict(state["optimizer"])
            sched.load_state_dict(state["scheduler"])
        else:
            # Legacy checkpoints saved decoder weights only. Continue at the
            # scheduled learning rate, explicitly restarting Adam moments.
            sched.last_epoch = step
            for group in opt.param_groups:
                group["lr"] = lr * (1 + math.cos(math.pi * step / total)) / 2
        metadata.update(resumed_from=str(resume_from), resumed_step=step,
                        optimizer_restored="optimizer" in state)
    t0 = time.time()
    if evaluate_initial:
        if not init_from:
            raise ValueError("Initial checkpoint comparison requires a decoder warm-start")
        if progress:
            progress({"phase": "baseline_validation", "step": 0, "total_steps": total})
        initial_metrics = evaluate_pairs(model, val_pairs, W, H, device, sources=val_sources)
        metadata["initial_val_metrics"] = initial_metrics
        from ..bootstrap import atomic_json
        atomic_json(out_path.parent / "initial_validation.json", {
            "checkpoint": str(init_from), "checkpoint_sha256": metadata["warm_start_sha256"],
            "training_cohort_sha256": metadata["training_cohort_sha256"], "metrics": initial_metrics,
            "test_images_used": 0, "candidate_only": True})

    def batch_iter():
        order = list(range(len(train_pairs)))
        while True:
            random.shuffle(order)
            for s in range(0, len(order), batch_size):
                yield [train_pairs[i] for i in order[s:s + batch_size]]

    def augment(rgb, cls):
        if random.random() < 0.5:
            rgb, cls = rgb[:, ::-1], cls[:, ::-1]
        s = random.uniform(0.8, 1.0)                       # random crop keeping the bottom (road) region
        h, w = cls.shape
        ch, cw = int(h * s), int(w * s)
        y0 = random.randint(int((h - ch) * 0.3), h - ch)
        x0 = random.randint(0, w - cw)
        rgb, cls = rgb[y0:y0 + ch, x0:x0 + cw], cls[y0:y0 + ch, x0:x0 + cw]
        if random.random() < 0.5:                          # photometric jitter
            rgb = np.clip(rgb.astype(np.float32) * random.uniform(0.6, 1.3) + random.uniform(-20, 20), 0, 255).astype(np.uint8)
        return np.ascontiguousarray(rgb), np.ascontiguousarray(cls)

    it = batch_iter()
    model.train()
    model.backbone.eval()
    while step < total:
        xs, ys = [], []
        for path, get_cls in next(it):
            rgb, cls = read_rgb(path), get_cls()
            if rgb is None:
                continue
            if cls.shape != rgb.shape[:2]:
                cls = cv2.resize(cls, (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_NEAREST)
            rgb, cls = augment(rgb, cls)
            xs.append(preprocess(rgb, W, H))
            ys.append(torch.from_numpy(cv2.resize(cls, (W, H), interpolation=cv2.INTER_NEAREST).astype(np.int64)))
        if not xs:
            continue
        x, y = torch.stack(xs).to(device), torch.stack(ys).to(device)
        with amp_context(torch, device, scaler_enabled):
            logits = model(x)
        logits = logits.float()
        valid = y != 0
        if not valid.any():
            raise RuntimeError("Training batch has no labelled pixels")
        ce = F.cross_entropy(logits, y, weight=weights, ignore_index=0)
        lane_p = torch.softmax(logits, 1)[:, 2] * valid
        lane_t = (y == 2).float()
        dice = 1 - (2 * (lane_p * lane_t).sum() + 1) / (lane_p.sum() + lane_t.sum() + 1)
        loss = ce + 0.5 * dice
        if not torch.isfinite(loss):
            raise RuntimeError("Non-finite decoder training loss")
        opt.zero_grad(set_to_none=True)
        loss.backward()
        opt.step()
        sched.step()
        step += 1
        if step % 250 == 0 and step % max(1, steps_per_epoch) != 0:
            _save(model, out_path.with_name("last.pt"), backbone_id, step, len(train_pairs),
                  history[-1] if history else {}, history, W, H, metadata,
                  training_state={"optimizer": opt.state_dict(), "scheduler": sched.state_dict()})
        if progress:
            progress({"phase": "training", "step": step, "total_steps": total, "loss": float(loss.item()),
                      "train_images": len(train_pairs), "val_images": len(val_pairs), **metadata})
        if step % 50 == 0 or step == total:
            log.info("step %d/%d loss %.4f (ce %.4f dice %.4f)", step, total, loss.item(), ce.item(), dice.item())
        if step % max(1, steps_per_epoch) == 0 or step == total:
            if progress:
                progress({"phase": "validation", "step": step, "total_steps": total})
            metrics = evaluate_pairs(model, val_pairs, W, H, device, sources=val_sources) if val_pairs else {}
            score = metrics["by_source"][selection_source]["lane_iou"] if selection_source else metrics.get("lane_iou", 0.0)
            history.append({"step": step, "loss": float(loss.item()), "selection_lane_iou": score, **metrics})
            from ..bootstrap import atomic_json
            atomic_json(out_path.parent / "training_history.json", history)
            if score > best:
                best = score
                _save(model, out_path, backbone_id, step, len(train_pairs), metrics, history, W, H, metadata)
            _save(model, out_path.with_name("last.pt"), backbone_id, step, len(train_pairs), metrics, history, W, H, metadata,
                  training_state={"optimizer": opt.state_dict(), "scheduler": sched.state_dict()})
            if progress:
                progress({"phase": "training", "step": step, "total_steps": total, "val_metrics": metrics,
                          "best_lane_iou": best})
            model.train()
            model.backbone.eval()
    if not out_path.exists():
        _save(model, out_path, backbone_id, step, len(train_pairs), {}, history, W, H, metadata)
    peak_vram_gib = round(torch.cuda.max_memory_allocated() / 2**30, 3) if device == "cuda" else None
    del opt, model
    if device == "cuda":
        torch.cuda.empty_cache()
    return {"checkpoint": str(out_path), "steps": step, "train_images": len(train_pairs), "val_images": len(val_pairs),
            "best": best, "seconds": round(time.time() - t0, 1), "history": history[-5:],
            "peak_vram_gib": peak_vram_gib, **metadata}


def _save(model, out_path: Path, backbone_id, step, n_train, metrics, history, W, H, metadata=None, training_state=None):
    torch, _, _ = _torch()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    from ..bootstrap import atomic_json
    import os
    import uuid
    temporary = out_path.with_name(out_path.name + "." + uuid.uuid4().hex + ".tmp")
    torch.save({"decoder": model.decoder.state_dict(), "backbone": backbone_id, "trained_steps": step,
                "train_images": n_train, "val_metrics": metrics, "history": history, "input": [W, H],
                "classes": ["unknown", "road", "lane", "undrivable", "movable", "my_car"], **(metadata or {}), **(training_state or {})}, temporary)
    os.replace(temporary, out_path)
    atomic_json(out_path.with_suffix(".json"), {"backbone": backbone_id, "trained_steps": step,
                                              "train_images": n_train, "val_metrics": metrics, **(metadata or {})})


def evaluate_pairs(model, pairs, W, H, device, sources=None) -> dict:
    torch, _, _ = _torch()
    model.eval()
    lane, road, grouped = [], [], {}
    if sources is not None and len(sources) != len(pairs):
        raise ValueError("Validation source labels must match pairs")
    with torch.inference_mode():
        for index, (path, get_cls) in enumerate(pairs):
            rgb, cls = read_rgb(path), get_cls()
            if rgb is None:
                continue
            with amp_context(torch, device, device == "cuda"):
                logits = model(preprocess(rgb, W, H)[None].to(device))
            pred = logits.float().argmax(1)[0].cpu().numpy().astype(np.uint8)
            gt = cv2.resize(cls, (W, H), interpolation=cv2.INTER_NEAREST)
            m = iou_per_class(pred, gt)
            if sources is not None:
                group = grouped.setdefault(sources[index], {"lane": [], "road": [], "n": 0})
                group["n"] += 1
                if not math.isnan(m[2]):
                    group["lane"].append(m[2])
                if not math.isnan(m[1]):
                    group["road"].append(m[1])
            if not math.isnan(m[2]):
                lane.append(m[2])
            if not math.isnan(m[1]):
                road.append(m[1])
    result = {"lane_iou": float(np.mean(lane)) if lane else 0.0, "road_iou": float(np.mean(road)) if road else 0.0,
              "n": len(pairs)}
    if sources is not None:
        result["by_source"] = {key: {"lane_iou": float(np.mean(g["lane"])) if g["lane"] else 0.0,
                                     "road_iou": float(np.mean(g["road"])) if g["road"] else 0.0, "n": g["n"]}
                               for key, g in sorted(grouped.items())}
    return result


# ---- teacher -----------------------------------------------------------------------------------

class Dinov3SegTeacher(Teacher):
    name = "dinov3_seg"
    license_ids = ["dinov3", "dinov3_seg"]
    needs_gpu = True

    def __init__(self, settings, registry=None):
        super().__init__(settings)
        self.registry = registry
        self.model = None
        ckpt = Path(self.cfg.get("checkpoint", "models/dinov3_seg/current.pt"))
        self.ckpt = ckpt if ckpt.is_absolute() else settings.root / ckpt

    def checkpoint_sha(self) -> str:
        return file_sha256(self.ckpt) if self.ckpt.exists() else "missing"

    def load(self) -> None:
        torch, _, _ = _torch()
        if not self.ckpt.exists():
            raise RuntimeError(f"no trained decoder at {self.ckpt}; run `bcdata train-teacher` first")
        state = torch.load(self.ckpt, map_location="cpu")
        if int(state.get("trained_steps", 0)) <= 0 or not state.get("val_metrics"):
            raise RuntimeError("decoder checkpoint has no training steps or no validation metrics; refusing to pseudo-label")
        dev = self.settings.get("teachers.device", "auto")
        self.device = ("cuda" if torch.cuda.is_available() else "cpu") if dev == "auto" else dev
        self.model = build_model(state["backbone"], revision=state.get("backbone_revision"),
                                 precision=state.get("precision"), device=self.device,
                                 cache_dir=self.cfg.get("cache_dir")).to(self.device).eval()
        self.model.decoder.load_state_dict(state["decoder"])
        self.W, self.H = state.get("input", [self.cfg.get("input_width", 896), self.cfg.get("input_height", 448)])
        self.torch = torch

    def unload(self) -> None:
        if self.model is not None:
            del self.model
            self.model = None
            if self.device == "cuda":
                self.torch.cuda.empty_cache()

    def predict(self, rgb: np.ndarray, row) -> TeacherOutput:
        torch = self.torch
        h, w = rgb.shape[:2]
        with torch.inference_mode(), amp_context(torch, self.device, self.settings.get("teachers.amp", True)):
            logits = self.model(preprocess(rgb, self.W, self.H)[None].to(self.device))
        prob = torch.softmax(logits.float(), 1)[0].cpu().numpy()
        prob = np.stack([cv2.resize(p, (w, h), interpolation=cv2.INTER_LINEAR) for p in prob])
        return TeacherOutput(lane=prob[2], road=np.clip(prob[1] + prob[2], 0, 1),
                             extra={"movable": prob[4], "my_car": prob[5]}, meta={"input": [self.W, self.H]})
