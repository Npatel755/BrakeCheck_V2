"""Multi-teacher fusion: consolidated provisional masks plus a record of where teachers disagree.

* An image with a source label (human/simulator) uses that label; teachers are scored against it.
* Otherwise teachers are combined by weighted mean. The majority is *not* treated as truth: every
  teacher's map is kept, and disagreement (pairwise IoU, per-pixel spread, boundary distance)
  raises the image's review priority.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import cv2
import numpy as np

from .config import Settings
from .db import Registry
from .quality import write_png
from .teachers.base import TeacherOutput, load_output, to_u8

log = logging.getLogger(__name__)


def binary_iou(a: np.ndarray, b: np.ndarray) -> float:
    union = np.logical_or(a, b).sum()
    return float(np.logical_and(a, b).sum() / union) if union else 1.0


def boundary_distance(a: np.ndarray, b: np.ndarray, cap: float = 50.0) -> float:
    """Mean symmetric distance (px) between the edges of two binary masks, capped."""
    ea = cv2.morphologyEx(a.astype(np.uint8), cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)) > 0
    eb = cv2.morphologyEx(b.astype(np.uint8), cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)) > 0
    if not ea.any() and not eb.any():
        return 0.0
    if not ea.any() or not eb.any():
        return cap
    da = cv2.distanceTransform((~eb).astype(np.uint8), cv2.DIST_L2, 3)
    db = cv2.distanceTransform((~ea).astype(np.uint8), cv2.DIST_L2, 3)
    return float(min(cap, 0.5 * (np.minimum(da[ea], cap).mean() + np.minimum(db[eb], cap).mean())))


def fuse_maps(maps: dict[str, np.ndarray], weights: dict[str, float], thr: float, low: float, high: float) -> dict:
    names = sorted(maps)
    w = np.array([float(weights.get(n, 1.0)) for n in names], np.float32)
    stack = np.stack([maps[n] for n in names]).astype(np.float32)
    fused = np.tensordot(w / w.sum(), stack, axes=1)
    spread = np.sqrt(np.tensordot(w / w.sum(), (stack - fused) ** 2, axes=1))
    binar = {n: maps[n] >= thr for n in names}
    pair_iou, pair_bd = {}, {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            pair_iou[f"{a}|{b}"] = binary_iou(binar[a], binar[b])
            pair_bd[f"{a}|{b}"] = boundary_distance(binar[a], binar[b])
    positive = (fused >= low).sum()
    uncertain = ((fused > low) & (fused < high)).sum()
    return {
        "fused": fused, "spread": spread,
        "metrics": {
            "teachers": names,
            "pairwise_iou": pair_iou,
            "pairwise_boundary_px": pair_bd,
            "mean_pairwise_iou": float(np.mean(list(pair_iou.values()))) if pair_iou else None,
            "uncertain_fraction": float(uncertain / positive) if positive else 0.0,
            "positive_pixels": int((fused >= thr).sum()),
            "mean_spread_on_positive": float(spread[fused >= low].mean()) if positive else 0.0,
        },
    }


def fusion_score(metrics: dict, n_teachers: int) -> float:
    """Heuristic agreement score in [0,1]. Not a probability of correctness (see qc.calibration)."""
    unc = metrics.get("uncertain_fraction", 0.0)
    if n_teachers < 2 or metrics.get("mean_pairwise_iou") is None:
        return float(max(0.0, 0.7 * (1.0 - unc)))          # single teacher: never "high" on its own
    return float(np.clip(0.6 * metrics["mean_pairwise_iou"] + 0.4 * (1.0 - unc), 0.0, 1.0))


def latest_teacher_outputs(registry: Registry, image_id: str, teachers: list[str]) -> dict[str, tuple[str, TeacherOutput]]:
    out = {}
    for name in teachers:
        if name == "source_labels":
            continue
        ann = registry.annotation(image_id, "teacher", name)
        if ann is not None and Path(ann["path"]).exists():
            out[name] = (ann["model_version"], load_output(ann["path"]))
    return out


def fuse_image(settings: Settings, registry: Registry, row, teachers: list[str], force: bool = False) -> str | None:
    cfg = settings.get("fusion", {})
    thr, low, high = float(cfg.get("threshold", 0.5)), float(cfg.get("uncertain_low", 0.3)), float(cfg.get("uncertain_high", 0.7))
    image_id = row["image_id"]
    outs = latest_teacher_outputs(registry, image_id, teachers)
    src_ann = registry.annotation(image_id, "teacher", "source_labels")
    src = load_output(src_ann["path"]) if src_ann is not None and Path(src_ann["path"]).exists() else None
    if not outs and src is None:
        return None
    versions = {n: v for n, (v, _) in sorted(outs.items())}
    if src is not None:
        versions["source_labels"] = src_ann["model_version"]
    version = "fusion:" + hashlib.sha1(json.dumps([versions, cfg], sort_keys=True).encode()).hexdigest()[:12]
    if not force and registry.has_annotation(image_id, "fused", "fusion", version):
        return version
    d = settings.path("annotations", "fused_predictions")
    record: dict = {"image_id": image_id, "version": version, "teacher_versions": versions, "maps": {}}
    weights = cfg.get("weights", {}) or {}
    # weight 0 = evaluated but not trusted for labels yet (e.g. sam2_refine before it has proven itself)
    lane_maps = {n: o.lane for n, (_, o) in outs.items() if o.lane is not None and float(weights.get(n, 1.0)) > 0}
    road_maps = {n: o.road for n, (_, o) in outs.items() if o.road is not None and float(weights.get(n, 1.0)) > 0}
    all_lane_maps = {n: o.lane for n, (_, o) in outs.items() if o.lane is not None}

    if src is not None and src.lane is not None:
        lane, road = src.lane, src.road
        record["mode"] = "source_label"
        gt = lane >= 0.5
        record["teacher_vs_source"] = {n: {"lane_iou": binary_iou(m >= thr, gt), "lane_boundary_px": boundary_distance(m >= thr, gt)}
                                       for n, m in all_lane_maps.items()}
        record["metrics"] = {"teachers": sorted(all_lane_maps), "score": 1.0}
        spread = np.zeros_like(lane)
        extras = src.extra
    elif not lane_maps:
        return None
    else:
        lf = fuse_maps(lane_maps, cfg.get("weights", {}), thr, low, high)
        lane, spread = lf["fused"], lf["spread"]
        road = fuse_maps(road_maps, cfg.get("weights", {}), thr, low, high)["fused"] if road_maps else None
        record["mode"] = "teachers"
        record["metrics"] = {**lf["metrics"], "score": fusion_score(lf["metrics"], len(lane_maps))}
        extras = {}
        for name in ("comma_segnet", "dinov3_seg"):            # hood / vehicles from a segmenter that has them
            if name in outs:
                extras = {k: v for k, v in outs[name][1].extra.items() if k in ("movable", "my_car")}
                break
    for key, arr in (("lane", lane), ("road", road), ("spread", spread), *extras.items()):
        if arr is None:
            continue
        p = d / f"{image_id}_{key}.png"
        write_png(p, to_u8(arr))
        record["maps"][key] = str(p)
    meta = d / f"{image_id}.json"
    meta.write_text(json.dumps(record, indent=1, default=str))
    lic = [row["license_id"]] + [l for n in versions for l in _teacher_licenses(n)]
    registry.add_annotation(image_id, "fused", "fusion", version, str(meta), license_ids=sorted(set(lic)),
                            params={"mode": record["mode"]})
    return version


def _teacher_licenses(name: str) -> list[str]:
    return {"comma_segnet": ["comma_segnet"], "sam3": ["sam3"], "dinov3_seg": ["dinov3", "dinov3_seg"],
            "sam2": ["sam2"]}.get(name, [])


def load_fused(registry: Registry, image_id: str) -> tuple[dict, dict[str, np.ndarray]] | None:
    from .quality import read_gray_u8
    ann = registry.annotation(image_id, "fused", "fusion")
    if ann is None or not Path(ann["path"]).exists():
        return None
    record = json.loads(Path(ann["path"]).read_text())
    maps = {k: read_gray_u8(p).astype(np.float32) / 255.0 for k, p in record["maps"].items()}
    return record, maps


def run(settings: Settings, registry: Registry, rows, force: bool = False) -> dict:
    teachers = settings.get("teachers.order", [])
    counts = {"images": 0, "fused": 0, "source_label": 0, "no_teacher_output": 0}
    for r in rows:
        counts["images"] += 1
        if r["locked"]:
            continue
        v = fuse_image(settings, registry, r, teachers, force)
        if v is None:
            counts["no_teacher_output"] += 1
            continue
        counts["fused"] += 1
        if registry.image(r["image_id"])["has_source_label"]:
            counts["source_label"] += 1
    return counts
