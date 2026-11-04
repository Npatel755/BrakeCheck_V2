"""Automatic quality control and review prioritisation for every generated label.

The score is an agreement/plausibility heuristic, **not** the probability that a label is correct.
Once reviewers have judged enough images, `calibration()` reports the measured acceptance rate per
score bin; use that table (not the raw score) when deciding what can skip review.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import numpy as np

from .config import Settings
from .db import Registry
from .fusion import load_fused
from .geometry import lane_xs, paint_window
from .lanes import current_geometry

HARD = {"invalid_coordinates", "no_lanes_with_paint", "crossing_severe"}
PENALTY = {
    "no_lanes": 0.5, "invalid_coordinates": 1.0, "crossing": 0.35, "implausible_curve": 0.35,
    "slot_conflict": 0.4, "short_lane": 0.1, "low_mask_coverage": 0.25, "polyline_off_mask": 0.3,
    "teacher_disagreement": 0.35, "faded_or_uncertain": 0.1, "temporal_jump": 0.25, "one_teacher_only": 0.15,
}


def lane_paint(geo: dict, lane_prob: np.ndarray, thr: float = 0.5) -> np.ndarray:
    """Paint that could belong to a lane line: inside the tracked band, minus transverse runs."""
    H, W = lane_prob.shape
    paint = lane_prob >= thr
    region = np.zeros_like(paint)
    region[geo["horizon_y"]:geo["valid_bottom"] + 1] = True
    paint &= region
    # stop lines and crosswalk bars are paint but not lanes: drop row runs wider than a painted line
    # at that depth (the same rule the lane tracker uses)
    hy, by = geo["horizon_y"], geo["valid_bottom"]
    max_frac = float(geo.get("max_run_frac", 0.06))
    for y in range(hy, min(by + 1, H)):
        row = paint[y]
        if not row.any():
            continue
        pf = float(np.clip((y - hy) / max(1.0, by - hy), 0.12, 1.0))
        max_w = max(3.0, max_frac * W * pf)
        padded = np.concatenate([[0], row.astype(np.int8), [0]])
        d = np.diff(padded)
        for s, e in zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]):
            if e - s > max_w:
                row[s:e] = False
    return paint


def mask_polyline_consistency(geo: dict, lane_prob: np.ndarray, thr: float = 0.5) -> tuple[float, float]:
    """(share of lane paint within 6 px of an exported lane, share of lane points that sit on paint)."""
    import cv2
    H, W = lane_prob.shape
    canvas = np.zeros((H, W), np.uint8)
    on_paint, total = 0, 0
    for l in geo["lanes"]:
        if not l.get("slot"):
            continue
        pts = np.array([[p[0], p[1]] for p in l["points"]], np.float32)
        thick = 13 + 2 * int(round(float(l.get("double_half_sep", 0.0))))
        cv2.polylines(canvas, [np.round(pts).astype(np.int32)], False, 1, thickness=thick)
        for x, y, seen in l["points"]:
            if not seen:
                continue
            xi, yi = int(round(x)), int(y)
            if 0 <= xi < W and 0 <= yi < H:
                total += 1
                r = paint_window(l, yi, hy=geo["horizon_y"], by=geo["valid_bottom"], base=4)
                on_paint += int(lane_prob[yi, max(0, xi - r):xi + r + 1].max() >= thr)
    paint = lane_paint(geo, lane_prob, thr)
    covered = float((paint & (canvas > 0)).sum() / paint.sum()) if paint.sum() else 1.0
    return covered, (on_paint / total if total else 1.0)


def a2d2_source_coverage(geo: dict, lane_prob: np.ndarray, thr: float = 0.5) -> dict:
    """Measure source run centres, without weighting wide Audi strokes more.

    This is a conservative supplementary check, not a substitute for observed
    points touching paint or for lane-slot identity. Source components are
    independent of the proposed geometry. Near-horizontal components and the
    same over-wide row runs excluded by the lane tracker are not lane centres.
    A missing substantial component remains explicit even when other long
    boundaries make the aggregate coverage look good.
    """
    import cv2
    paint = lane_paint(geo, lane_prob, thr)
    H, W = paint.shape
    canvas = np.zeros((H, W), np.uint8)
    profiles = {}
    for line in geo["lanes"]:
        if line.get("slot"):
            points = np.round(np.asarray(line["points"], float)[:, :2]).astype(np.int32)
            cv2.polylines(canvas, [points], False, 1, thickness=13)
            points = points[np.argsort(points[:, 1])]
            xs = np.full(H, np.nan)
            observed = np.zeros(H, bool)
            lo, hi = max(0, points[0, 1]), min(H-1, points[-1, 1])
            for yi in range(lo, hi+1):
                x = float(np.interp(yi, points[:, 1], points[:, 0]))
                xi = int(round(x))
                if 0 <= xi < W:
                    xs[yi] = x
                    radius = paint_window(line, yi, hy=geo["horizon_y"], by=geo["valid_bottom"], base=4)
                    observed[yi] = bool(paint[yi, max(0, xi-radius):min(W, xi+radius+1)].any())
            profiles[line["slot"]] = xs, observed
    count, labels, stats, _ = cv2.connectedComponentsWithStats(paint.astype(np.uint8), 8)
    centres, covered, uncovered_components, transverse = 0, 0, [], 0
    beyond_four = []
    for component in range(1, count):
        x, y, width, height, _ = map(int, stats[component])
        # Row-centre measurements are unsuitable for transverse road paint.
        # Keep the aspect limit permissive for diagonal foreground boundaries.
        if height < 8 or height < .15 * width:
            transverse += 1
            continue
        component_total = component_covered = 0
        measured = []
        for yi in range(y, y + height):
            row = labels[yi, x:x+width] == component
            padded = np.concatenate([[0], row.astype(np.int8), [0]])
            delta = np.diff(padded)
            for start, stop in zip(np.nonzero(delta == 1)[0], np.nonzero(delta == -1)[0]):
                xi = x + int(round((int(start) + int(stop) - 1) / 2))
                component_total += 1
                component_covered += int(canvas[yi, xi] > 0)
                measured.append((xi, yi))
        # Four-slot targets contain two boundaries on either side. Extra
        # source paint is outside that contract only when BOTH retained
        # boundaries coexist on real paint at EVERY measured component row.
        # A bounding box, extrapolated line or fragment label is insufficient.
        outside = False
        for outer, inner, side in ((1, 2, -1), (4, 3, 1)):
            if outer not in profiles or inner not in profiles or not measured:
                continue
            ox, op = profiles[outer]
            ix, ip = profiles[inner]
            margin = max(12., .02 * W)
            if all(op[y] and ip[y] and (ox[y]-ix[y])*side > margin
                   and (x-ox[y])*side > margin for x,y in measured):
                outside = True
                break
        if outside:
            beyond_four.append({"bbox":[x,y,width,height], "rows":component_total})
            continue
        centres += component_total
        covered += component_covered
        fraction = component_covered / max(1, component_total)
        if height >= max(8, .025 * H) and fraction < .75:
            uncovered_components.append({"bbox": [x, y, width, height],
                                         "coverage": round(fraction, 3), "rows": component_total})
    return {"coverage": covered / centres if centres else None,
            "source_centres": centres, "uncovered_components": uncovered_components,
            "transverse_components": transverse, "beyond_four_components": beyond_four}


def _a2d2_disjoint_slots(geo: dict) -> list[list[int]]:
    """Flag same-side slots observed in disjoint depth bands for inspection.

    Two pieces of one dashed boundary can touch all source pixels yet acquire
    different slots. Occlusion can also cause this pattern, so it is uncertainty
    for review, not proof that the boundaries should be merged.
    """
    by_slot = {line["slot"]: line for line in geo["lanes"] if line.get("slot")}
    result = []
    for left, right in ((1, 2), (3, 4)):
        if left in by_slot and right in by_slot:
            a, b = by_slot[left], by_slot[right]
            gap = max(a["y_top"], b["y_top"]) - min(a["y_bottom"], b["y_bottom"])
            if gap > 5:
                result.append([left, right])
    return result


def check(geo: dict, fused_record: dict | None, lane_prob: np.ndarray | None, neighbour: dict | None,
          settings: Settings, *, source: str | None = None) -> tuple[float, list[dict]]:
    issues: list[dict] = []
    slotted = [l for l in geo["lanes"] if l.get("slot")]
    paint_px = int(lane_paint(geo, lane_prob).sum()) if lane_prob is not None else 0
    if not slotted:
        big = paint_px >= 0.0008 * geo["width"] * geo["height"]       # ~800 px on a 1 MP frame
        issues.append({"code": "no_lanes_with_paint" if big else "no_lanes", "detail": f"{paint_px} lane-like paint px"})
    slots = [l["slot"] for l in slotted]
    if len(slots) != len(set(slots)):
        issues.append({"code": "slot_conflict"})
    xs_ref = {l["slot"]: l.get("x_ref") for l in slotted}
    ordered = [xs_ref[s] for s in sorted(xs_ref) if xs_ref[s] is not None]
    if ordered != sorted(ordered):
        issues.append({"code": "slot_conflict", "detail": "slot order disagrees with x order"})
    for l in slotted:
        for f in l.get("flags", []):
            if f in ("invalid_coordinates", "crossing", "implausible_curve", "faded_or_uncertain"):
                issues.append({"code": f, "lane": l["id"]})
        pts = np.array([[p[0], p[1]] for p in l["points"]], float)
        if not np.isfinite(pts).all() or (pts[:, 0] < -0.5 * geo["width"]).any() or (pts[:, 0] > 1.5 * geo["width"]).any():
            issues.append({"code": "invalid_coordinates", "lane": l["id"]})
        if l["y_bottom"] - l["y_top"] < 0.12 * (geo["valid_bottom"] - geo["horizon_y"]) and l["slot"] in (2, 3):
            issues.append({"code": "short_lane", "lane": l["id"]})
    crossings = sum(1 for i in issues if i["code"] == "crossing")
    if crossings >= 2:
        issues.append({"code": "crossing_severe"})
    if lane_prob is not None and slotted:
        cov, on = mask_polyline_consistency(geo, lane_prob)
        if source == "a2d2" and (fused_record or {}).get("mode") == "source_label":
            agreement = a2d2_source_coverage(geo, lane_prob)
            disjoint = _a2d2_disjoint_slots(geo)
            warnings = geo.get("source_converter", {}).get("warnings", [])
            if agreement["uncovered_components"]:
                issues.append({"code": "source_boundary_uncovered",
                               "detail": agreement["uncovered_components"]})
            if disjoint:
                issues.append({"code": "source_slot_disjoint", "detail": disjoint})
            if warnings:
                issues.append({"code": "source_conversion_uncertain", "detail": warnings})
            # Remove the broad-stroke area bias only when an independent
            # centre measurement is strong and no completeness/identity risk
            # remains. Otherwise preserve the existing conservative flag.
            if (agreement["coverage"] is not None and agreement["coverage"] >= .85
                    and on >= .9 and not agreement["uncovered_components"]
                    and not disjoint and not warnings):
                cov = agreement["coverage"]
                # A short source-labeled span can be the complete visible
                # boundary beside an occluding car or at the image border.
                # Its length belongs in sampled review; strong independent
                # coverage prevents a missing continuation from using this.
                issues = [issue for issue in issues if issue["code"] != "short_lane"]
        if cov < 0.6:
            issues.append({"code": "low_mask_coverage", "detail": round(cov, 3)})
        if on < 0.7:
            issues.append({"code": "polyline_off_mask", "detail": round(on, 3)})
    fm = (fused_record or {}).get("metrics", {})
    if (fused_record or {}).get("mode") == "teachers":
        n = len(fm.get("teachers", []))
        if n < 2:
            issues.append({"code": "one_teacher_only"})
        elif fm.get("mean_pairwise_iou") is not None and fm["mean_pairwise_iou"] < 0.5:
            issues.append({"code": "teacher_disagreement", "detail": round(fm["mean_pairwise_iou"], 3)})
    if neighbour is not None:
        jump = temporal_jump(geo, neighbour)
        if jump is not None and jump > float(settings.get("qc.temporal_max_dx_px", 40)):
            issues.append({"code": "temporal_jump", "detail": round(jump, 1)})
    score = 1.0
    for i in issues:
        score -= PENALTY.get(i["code"], 0.2)
    if (fused_record or {}).get("mode") == "teachers":
        score = min(score, float(fm.get("score", score)) + 0.2)
    return float(np.clip(score, 0.0, 1.0)), issues


def temporal_jump(a: dict, b: dict) -> float | None:
    """Mean |dx| between same-slot lanes of two adjacent frames over their shared rows."""
    by_slot = {l["slot"]: l for l in b["lanes"] if l.get("slot")}
    diffs = []
    for l in a["lanes"]:
        m = by_slot.get(l.get("slot"))
        if not m:
            continue
        lo, hi = max(l["y_top"], m["y_top"]), min(l["y_bottom"], m["y_bottom"])
        if hi - lo < 10:
            continue
        ys = np.arange(lo, hi, 5)
        diffs.append(float(np.abs(lane_xs(l, ys) - lane_xs(m, ys)).mean()))
    return float(np.mean(diffs)) if diffs else None


def tier_for(score: float, issues: list[dict], mode: str | None, settings: Settings) -> str:
    codes = {i["code"] for i in issues}
    if codes & HARD:
        return "invalid"
    # a human/simulator mask with no lane paint is a valid "no lanes here" example
    if mode == "source_label" and not codes - {"short_lane", "faded_or_uncertain", "no_lanes"}:
        return "source_label"
    t = settings.get("qc.tiers", {"high": 0.8, "medium": 0.5})
    if score >= t["high"]:
        return "high"
    if score >= t["medium"]:
        return "medium"
    return "low"


def run(settings: Settings, registry: Registry, rows) -> dict:
    rows = list(rows)
    rng = random.Random(int(settings.get("seed", 0)) + 17)
    rate = float(settings.get("qc.random_sample_rate", 0.05))
    by_session: dict[str, list] = {}
    for r in rows:
        by_session.setdefault(r["session_id"], []).append(r)
    tag_freq: dict[str, int] = {}
    for r in rows:
        for k, v in registry.tags(r).items():
            tag_freq[f"{k}={v['value']}"] = tag_freq.get(f"{k}={v['value']}", 0) + 1
    counts: dict = {"images": len(rows), "no_geometry": 0}
    for sess in by_session.values():
        sess.sort(key=lambda r: (r["frame_index"] is None, r["frame_index"] or 0))
        prev_geo = None
        prev_frame = None
        for r in sess:
            cur = current_geometry(registry, r["image_id"])
            if cur is None:
                counts["no_geometry"] += 1
                prev_geo = None
                continue
            origin, geo = cur
            if origin == "human":
                registry.set_qc(r["image_id"], 1.0, "human", 0.0, [], False, "human")
                counts["human"] = counts.get("human", 0) + 1
                prev_geo, prev_frame = geo, r["frame_index"]
                continue
            fused = load_fused(registry, r["image_id"])
            record, maps = fused if fused else (None, {})
            neighbour = prev_geo if (prev_frame is not None and r["frame_index"] is not None
                                     and r["source"] in ("comma2k19", "carla")
                                     and abs(r["frame_index"] - prev_frame) <= 5) else None
            score, issues = check(geo, record, maps.get("lane"), neighbour, settings, source=r["source"])
            tier = tier_for(score, issues, (record or {}).get("mode"), settings)
            rarity = sum(1.0 / tag_freq.get(f"{k}={v['value']}", 1) for k, v in registry.tags(r).items())
            disagreement = 1.0 - float((record or {}).get("metrics", {}).get("mean_pairwise_iou") or 1.0)
            priority = (1.0 - score) + 0.5 * disagreement + min(0.5, rarity)
            sample = tier in ("high", "source_label") and rng.random() < rate
            registry.set_qc(r["image_id"], score, tier, priority, issues, sample, geo.get("version", ""))
            counts[tier] = counts.get(tier, 0) + 1
            counts["qc_sample"] = counts.get("qc_sample", 0) + int(sample)
            prev_geo, prev_frame = geo, r["frame_index"]
    return counts


def calibration(registry: Registry, bins: int = 5) -> list[dict]:
    """Measured acceptance rate of reviewed auto labels per QC score bin."""
    rows = registry.conn.execute("""SELECT q.score, r.decision FROM qc q JOIN reviews r ON r.image_id=q.image_id
                                    WHERE q.tier NOT IN ('human')""").fetchall()
    out = []
    edges = np.linspace(0, 1, bins + 1)
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = [d for s, d in rows if lo <= s < hi or (hi == 1.0 and s == 1.0)]
        if sel:
            out.append({"score_bin": f"{lo:.1f}-{hi:.1f}", "n": len(sel),
                        "accepted_unchanged": round(sum(d == "approved" for d in sel) / len(sel), 3),
                        "corrected": round(sum(d == "corrected" for d in sel) / len(sel), 3),
                        "rejected": round(sum(d == "rejected" for d in sel) / len(sel), 3)})
    return out


def review_queue(settings: Settings, registry: Registry, batch_size: int | None = None, name: str | None = None) -> Path:
    """Write the next review batch (highest priority first, plus the random QC sample) as CSV + JSON."""
    import csv
    import time
    batch_size = int(batch_size or settings.get("review.batch_size", 100))
    rows = registry.conn.execute("""
        SELECT i.image_id, i.source, i.split, q.score, q.tier, q.priority, q.issues, q.qc_sample
        FROM images i JOIN qc q ON q.image_id=i.image_id
        WHERE i.locked=0 AND i.status='curated' AND q.tier != 'human'
          AND (q.tier IN ('low','medium','invalid') OR q.qc_sample=1 OR i.split='test')
          AND i.review_status IN ('none','queued','difficult')
        ORDER BY (i.split='test') DESC, q.qc_sample DESC, q.priority DESC LIMIT ?""", (batch_size,)).fetchall()
    name = name or time.strftime("batch_%Y%m%d_%H%M%S")
    d = settings.path("review_queue", name)
    d.mkdir(parents=True, exist_ok=True)
    reviewers = settings.get("review.reviewers", ["reviewer_1", "reviewer_2"])
    with open(d / "queue.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "image_id", "assigned_to", "source", "split", "tier", "score", "priority", "qc_sample", "issues"])
        for k, r in enumerate(rows):
            issues = ",".join(i["code"] for i in json.loads(r["issues"] or "[]"))
            w.writerow([k + 1, r["image_id"], reviewers[k % len(reviewers)], r["source"], r["split"], r["tier"],
                        round(r["score"], 3), round(r["priority"], 3), r["qc_sample"], issues])
    with registry.tx() as c:
        for r in rows:
            c.execute("UPDATE images SET review_status='queued' WHERE image_id=? AND review_status='none'", (r["image_id"],))
    (d / "queue.json").write_text(json.dumps([r["image_id"] for r in rows]))
    return d
