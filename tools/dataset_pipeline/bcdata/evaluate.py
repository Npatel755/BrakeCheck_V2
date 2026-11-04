"""Teacher evaluation on the fixed evaluation set (split='test'), against human or source labels.

Mask metrics: IoU, precision, recall, boundary F-score (3 px tolerance).
Lane metrics: lanes matched by mean horizontal distance (Hungarian), centerline error, completeness
(share of reference rows covered by the matched lane), false-positive and missed lanes, and a
CULane-style F1 where a predicted lane counts when its 30 px-wide (at 1640 px) line overlaps the
reference with IoU >= 0.5. A bigger model replaces a smaller one only if it wins here.
"""
from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
from scipy.optimize import linear_sum_assignment

from .config import Settings
from .db import Registry
from .fusion import load_fused
from .geometry import extract_lanes, lane_xs
from .lanes import current_geometry, params_for
from .teachers.base import load_output


def mask_metrics(pred: np.ndarray, ref: np.ndarray, tol: int = 3) -> dict:
    p, r = pred.astype(bool), ref.astype(bool)
    tp, fp, fn = (p & r).sum(), (p & ~r).sum(), (~p & r).sum()
    iou = tp / (tp + fp + fn) if (tp + fp + fn) else 1.0
    prec = tp / (tp + fp) if (tp + fp) else 1.0
    rec = tp / (tp + fn) if (tp + fn) else 1.0
    k = np.ones((3, 3), np.uint8)
    ep = cv2.morphologyEx(p.astype(np.uint8), cv2.MORPH_GRADIENT, k) > 0
    er = cv2.morphologyEx(r.astype(np.uint8), cv2.MORPH_GRADIENT, k) > 0
    if ep.any() and er.any():
        dr = cv2.distanceTransform((~er).astype(np.uint8), cv2.DIST_L2, 3)
        dp = cv2.distanceTransform((~ep).astype(np.uint8), cv2.DIST_L2, 3)
        bp, br = (dr[ep] <= tol).mean(), (dp[er] <= tol).mean()
        bf = 2 * bp * br / (bp + br) if (bp + br) else 0.0
    else:
        bf = 1.0 if not ep.any() and not er.any() else 0.0
    return {"iou": float(iou), "precision": float(prec), "recall": float(rec), "boundary_f": float(bf)}


def _lane_mask(lane: dict, shape: tuple[int, int], width: int) -> np.ndarray:
    m = np.zeros(shape, np.uint8)
    ys = np.arange(lane["y_top"], lane["y_bottom"] + 1, 2, dtype=float)
    pts = np.stack([lane_xs(lane, ys), ys], 1)
    cv2.polylines(m, [np.round(pts).astype(np.int32)], False, 1, thickness=max(1, width))
    return m > 0


def lane_metrics(pred: dict, ref: dict, match_px: float | None = None) -> dict:
    """pred/ref: geometry dicts in the same source-pixel frame. Only slotted (exportable) lanes count."""
    P = [l for l in pred["lanes"] if l.get("slot")]
    R = [l for l in ref["lanes"] if l.get("slot")]
    W, H = ref["width"], ref["height"]
    match_px = match_px or 0.03 * W
    line_w = max(2, int(round(30 * W / 1640)))
    if not P or not R:
        return {"ref_lanes": len(R), "pred_lanes": len(P), "matched": 0, "fp": len(P), "fn": len(R),
                "centerline_err_px": None, "completeness": 0.0 if R else 1.0, "f1_culane": 1.0 if not P and not R else 0.0}
    cost = np.full((len(P), len(R)), 1e6)
    for i, p in enumerate(P):
        for j, r in enumerate(R):
            lo, hi = max(p["y_top"], r["y_top"]), min(p["y_bottom"], r["y_bottom"])
            if hi - lo < 5:
                continue
            ys = np.arange(lo, hi + 1, 3)
            cost[i, j] = float(np.abs(lane_xs(p, ys) - lane_xs(r, ys)).mean())
    rows, cols = linear_sum_assignment(cost)
    matched, errs, comp = [], [], []
    for i, j in zip(rows, cols):
        if cost[i, j] <= match_px:
            matched.append((i, j))
            errs.append(cost[i, j])
            r = R[j]
            span = max(1, r["y_bottom"] - r["y_top"])
            lo, hi = max(P[i]["y_top"], r["y_top"]), min(P[i]["y_bottom"], r["y_bottom"])
            comp.append(max(0, hi - lo) / span)
    # CULane-style line IoU F1
    tp = 0
    for i, j in zip(rows, cols):
        if cost[i, j] >= 1e6:
            continue
        a, b = _lane_mask(P[i], (H, W), line_w), _lane_mask(R[j], (H, W), line_w)
        if (a & b).sum() / max(1, (a | b).sum()) >= 0.5:
            tp += 1
    prec, rec = tp / len(P), tp / len(R)
    return {"ref_lanes": len(R), "pred_lanes": len(P), "matched": len(matched), "fp": len(P) - len(matched),
            "fn": len(R) - len(matched), "centerline_err_px": float(np.mean(errs)) if errs else None,
            "completeness": float(np.mean(comp)) if comp else 0.0,
            "f1_culane": float(2 * prec * rec / (prec + rec)) if (prec + rec) else 0.0}


def reference_for(registry: Registry, row) -> tuple[str, dict | None, np.ndarray | None]:
    """Human review wins; otherwise a source label (comma10k/ZOD/CARLA). Returns (origin, geometry, lane mask)."""
    cur = current_geometry(registry, row["image_id"])
    fused = load_fused(registry, row["image_id"])
    src_mask = fused[1]["lane"] >= 0.5 if fused and fused[0].get("mode") == "source_label" else None
    if cur is not None and cur[0] == "human":
        return "human", cur[1], src_mask
    if src_mask is not None and cur is not None:
        return "source_label", cur[1], src_mask
    return "none", None, None


def _mean(vals):
    vals = [v for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
    return float(np.mean(vals)) if vals else None


def evaluate(settings: Settings, registry: Registry, split: str = "test", teachers: list[str] | None = None,
             write: bool = True) -> dict:
    teachers = teachers or [t for t in settings.get("teachers.order", []) if t != "source_labels"]
    params = params_for(settings)
    rows = registry.images("split=? AND status='curated'", (split,))
    per_teacher: dict[str, dict[str, list]] = {t: defaultdict(list) for t in teachers}
    per_tag: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    n_ref = defaultdict(int)
    reference_guided_excluded = defaultdict(int)
    for r in rows:
        origin, ref_geo, ref_mask = reference_for(registry, r)
        if ref_geo is None:
            continue
        n_ref[origin] += 1
        if ref_mask is None:                       # human-only reference: rasterise its lanes for mask metrics
            ref_mask = np.zeros((r["height"], r["width"]), bool)
            for l in ref_geo["lanes"]:
                ref_mask |= _lane_mask(l, ref_mask.shape, max(2, r["width"] // 300))
        tags = registry.tags(r)
        for t in teachers:
            ann = registry.annotation(r["image_id"], "teacher", t)
            if ann is None or not Path(ann["path"]).exists():
                continue
            out = load_output(ann["path"])
            if out.lane is None:
                continue
            if t == "source_labels" or out.meta.get("source_guided", False):
                # A model prompted using the reference answer is useful for
                # annotation, but cannot establish independent lane accuracy.
                reference_guided_excluded[t] += 1
                continue
            mm = mask_metrics(out.lane >= params.threshold, ref_mask)
            fused = load_fused(registry, r["image_id"])
            maps = fused[1] if fused else {}
            pred_geo = extract_lanes(out.lane, params, maps.get("my_car"), maps.get("movable"))
            lm = lane_metrics(pred_geo, ref_geo)
            for k, v in {**{f"lane_{k}": v for k, v in mm.items()}, **lm}.items():
                per_teacher[t][k].append(v)
            for name, tv in tags.items():
                per_tag[f"{t}|{name}={tv['value']}"]["lane_iou"].append(mm["iou"])
                per_tag[f"{t}|{name}={tv['value']}"]["f1_culane"].append(lm["f1_culane"])
    summary = {t: {k: _mean(v) for k, v in m.items()} | {"n": len(m.get("lane_iou", []))} for t, m in per_teacher.items()}
    by_tag = {k: {m: _mean(v) for m, v in d.items()} | {"n": len(d["lane_iou"])} for k, d in sorted(per_tag.items())}
    result = {"split": split, "references": dict(n_ref), "teachers": summary, "by_tag": by_tag,
              "reference_guided_excluded": dict(reference_guided_excluded),
              "corrections": corrections_by_teacher(settings, registry), "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    if write:
        d = settings.path("reports")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        (d / f"eval_{stamp}.json").write_text(json.dumps(result, indent=1, default=str))
        (d / f"eval_{stamp}.md").write_text(to_markdown(result))
        result["report"] = str(d / f"eval_{stamp}.md")
    return result


def corrections_by_teacher(settings: Settings, registry: Registry) -> dict:
    """For human-corrected images: how far each teacher's lanes were from what the reviewer drew."""
    out: dict[str, list] = defaultdict(list)
    rows = registry.conn.execute("SELECT DISTINCT image_id FROM reviews WHERE decision='corrected'").fetchall()
    for (image_id,) in rows:
        cur = current_geometry(registry, image_id)
        if cur is None or cur[0] != "human":
            continue
        for tdir in settings.path("lane_geometry", "teachers").glob("*"):
            p = tdir / f"{image_id}.json"
            if p.exists():
                lm = lane_metrics(json.loads(p.read_text()), cur[1])
                out[tdir.name].append(lm["f1_culane"])
    return {t: {"n": len(v), "mean_f1_vs_human": _mean(v)} for t, v in out.items()}


def to_markdown(res: dict) -> str:
    lines = [f"# Teacher evaluation ({res['split']} split, {res['created']})", "",
             f"References: {res['references']}", "",
             "| teacher | n | lane IoU | precision | recall | boundary F | F1 (CULane-style) | centerline err px | completeness | FP lanes | missed lanes |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    if res.get("reference_guided_excluded"):
        lines.insert(3, "Reference-guided outputs excluded from independent scores: " + str(res["reference_guided_excluded"]))
    f = lambda v: "-" if v is None else f"{v:.3f}"
    for t, m in res["teachers"].items():
        lines.append(f"| {t} | {m.get('n', 0)} | {f(m.get('lane_iou'))} | {f(m.get('lane_precision'))} | {f(m.get('lane_recall'))} | "
                     f"{f(m.get('lane_boundary_f'))} | {f(m.get('f1_culane'))} | {f(m.get('centerline_err_px'))} | "
                     f"{f(m.get('completeness'))} | {f(m.get('fp'))} | {f(m.get('fn'))} |")
    if res.get("by_tag"):
        lines += ["", "## By scene tag", "", "| teacher / tag | n | lane IoU | F1 |", "|---|---|---|---|"]
        for k, m in res["by_tag"].items():
            lines.append(f"| {k} | {m['n']} | {f(m.get('lane_iou'))} | {f(m.get('f1_culane'))} |")
    if res.get("corrections"):
        lines += ["", "## Distance of each teacher to human corrections", ""]
        for t, m in res["corrections"].items():
            lines.append(f"- {t}: n={m['n']}, mean F1 vs human {f(m['mean_f1_vs_human'])}")
    return "\n".join(lines) + "\n"
