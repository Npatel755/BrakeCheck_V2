"""Active learning: reviewed labels -> fine-tune the DINOv3 teacher -> evaluate on validation ->
promote only if it is better (overall and without regressing a scene tag) -> re-label uncertain images ->
build the next review queue, weighted toward rare conditions and failure categories."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import cv2
import numpy as np

from . import fusion, lanes, qc
from .config import Settings
from .db import Registry
from .evaluate import evaluate
from .geometry import lane_xs
from .licenses import LicenseRegistry
from .teachers.base import run_teacher

log = logging.getLogger(__name__)


def human_pairs(registry: Registry, settings: Settings) -> list:
    """Reviewed train/val images (never test) -> (image path, class-map loader) using reviewed lanes."""
    from .fusion import load_fused
    pairs = []
    rows = registry.images("review_status IN ('approved','corrected') AND split IN ('train','val') AND pool='commercial'")
    for r in rows:
        ann = registry.annotation(r["image_id"], "human")
        if ann is None or r["has_source_label"]:
            continue                                   # source-labelled images are already in the training set
        geo = json.loads(Path(ann["path"]).read_text())

        def loader(r=r, geo=geo):
            fused = load_fused(registry, r["image_id"])
            cls = np.zeros((r["height"], r["width"]), np.uint8)
            if fused is not None and "road" in fused[1]:
                cls[fused[1]["road"] >= 0.7] = 1        # confident road only; the rest is ignored
            for l in geo["lanes"]:
                ys = np.arange(l["y_top"], l["y_bottom"] + 1, 2, dtype=float)
                pts = np.stack([lane_xs(l, ys), ys], 1)
                w = max(2, int(r["width"] / 160))
                cv2.polylines(cls, [np.round(pts).astype(np.int32)], False, 2, thickness=w)
            return cls
        pairs.append((r["path"], loader))
    return pairs


def cycle(settings: Settings, registry: Registry, licenses: LicenseRegistry, epochs: int = 10,
          max_steps: int | None = None, force: bool = False) -> dict:
    from .teachers.dinov3_seg import Dinov3SegTeacher, train
    cfg = settings.get("active_learning", {})
    reviewed = registry.count("SELECT COUNT(*) FROM images WHERE review_status IN ('approved','corrected')")
    last = registry.conn.execute("SELECT params FROM runs WHERE stage='al-cycle' AND ok=1 ORDER BY id DESC LIMIT 1").fetchone()
    last_n = json.loads(last["params"]).get("reviewed", 0) if last else 0
    report: dict = {"reviewed": reviewed, "new_since_last": reviewed - last_n}
    if not force and reviewed - last_n < int(cfg.get("min_new_reviewed", 100)):
        report["status"] = f"waiting: {reviewed - last_n} new reviews (< {cfg.get('min_new_reviewed', 100)})"
        return report
    with registry.run("al-cycle", {"reviewed": reviewed}) as counts:
        current = registry.promoted_model("dinov3_seg")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        ckpt = settings.path("models", "dinov3_seg", f"candidate_{stamp}.pt")
        res = train(settings, registry, ckpt, epochs=epochs, max_steps=max_steps,
                    init_from=Path(current["checkpoint_path"]) if current else None,
                    extra_pairs=human_pairs(registry, settings))
        report["train"] = res
        # Select candidates on validation; reserve test for frozen final evaluation.
        cand_version = _evaluate_checkpoint(settings, registry, licenses, ckpt)
        ev = evaluate(settings, registry, "val", teachers=["dinov3_seg"], write=True)
        report["candidate_eval"] = ev["teachers"].get("dinov3_seg")
        old = json.loads(current["metrics"]) if current and current["metrics"] else None
        report["current_eval"] = old
        promote, why = decide(report["candidate_eval"], old, ev.get("by_tag", {}), json.loads(current["metrics"]).get("by_tag", {}) if old else {}, cfg)
        report["promoted"], report["reason"] = promote, why
        registry.register_model(cand_version, "dinov3_seg", str(ckpt), None, ["dinov3", "dinov3_seg"],
                                parent=current["model_version"] if current else None,
                                metrics={**(report["candidate_eval"] or {}), "by_tag": ev.get("by_tag", {})})
        if promote:
            registry.promote_model(cand_version, "dinov3_seg")
            target = settings.path(settings.get("teachers.dinov3_seg.checkpoint", "models/dinov3_seg/current.pt"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(ckpt.read_bytes())
            target.with_suffix(".json").write_text(ckpt.with_suffix(".json").read_text())
            # re-label images the old teacher was unsure about; human-locked ones are skipped by the runner
            unsure = registry.conn.execute("""SELECT i.* FROM images i JOIN qc q ON q.image_id=i.image_id
                                              WHERE i.status='curated' AND i.locked=0 AND q.tier IN ('low','medium','invalid')""").fetchall()
            t = Dinov3SegTeacher(settings, registry)
            report["relabel"] = run_teacher(settings, registry, licenses, t, unsure, force=True)
            report["fusion"] = fusion.run(settings, registry, unsure, force=True)
            report["geometry"] = lanes.run(settings, registry, unsure, force=True)
            report["qc"] = qc.run(settings, registry, registry.images("status='curated'"))
        report["next_queue"] = str(qc.review_queue(settings, registry))
        counts.update({k: v for k, v in report.items() if k in ("promoted", "reason")})
    return report


def _evaluate_checkpoint(settings, registry, licenses, ckpt: Path) -> str:
    """Run the candidate on validation only (outputs are versioned by checkpoint hash)."""
    from .teachers.dinov3_seg import Dinov3SegTeacher
    t = Dinov3SegTeacher(settings, registry)
    t.ckpt = ckpt
    run_teacher(settings, registry, licenses, t, registry.images("split='val' AND status='curated'"))
    return t.version


def decide(cand: dict | None, old: dict | None, cand_tags: dict, old_tags: dict, cfg: dict) -> tuple[bool, str]:
    if not cand or cand.get("n", 0) == 0:
        return False, "no evaluation images with references"
    if not old:
        return True, "first trained decoder"
    gain = (cand.get("lane_iou") or 0) - (old.get("lane_iou") or 0)
    if gain < float(cfg.get("min_iou_gain", 0.005)):
        return False, f"lane IoU gain {gain:+.4f} below threshold"
    worst = 0.0
    for k, m in cand_tags.items():
        if k in old_tags and m.get("lane_iou") is not None and old_tags[k].get("lane_iou") is not None and m["n"] >= 5:
            worst = min(worst, m["lane_iou"] - old_tags[k]["lane_iou"])
    if worst < -float(cfg.get("max_tag_regression", 0.03)):
        return False, f"regressed {worst:+.3f} lane IoU on a scene tag"
    return True, f"lane IoU {gain:+.4f}, worst tag change {worst:+.3f}"
