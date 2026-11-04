"""Lane-marking masks -> individual, ordered lane lines (the CULane/UFLD notion of a "lane").

Pipeline (all in source-image pixels):
 1. Mask cleanup: ignore rows above the horizon and the ego car's hood; drop specks.
 2. Run tracking, bottom row to top: each row's runs of paint are linked to the track whose
    extrapolated position is nearest (perspective-scaled tolerance). Runs much wider than a painted
    line at that depth are transverse paint (stop lines, crosswalk bars) and are not tracked.
    A branching line (lane split) starts a new track; tracks end after a few rows without paint.
 3. Segment filtering: drop near-horizontal pieces (transverse) and stubby blobs (arrows, symbols).
 4. Dashed grouping: a segment joins the chain below it when the chain's extrapolation lands on it,
    the gap is plausible and the slopes agree. Gaps inside a chain are bridged (the painted dashes
    define one physical lane line); nothing is extended past the last observed paint.
 5. Fit x(y) per chain (robust polynomial, degree by length), merge double lines, order lanes
    around the ego position into CULane slots 1..4 (left-left, left, right, right-right).
 6. Flags and scene tags: crossings, merges/splits, curvature, implausible shapes, crosswalks,
    occlusion by vehicles in gaps, faded (low-confidence) paint.

Virtual / inferred boundaries are never invented here; every exported lane is `physical: true`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np


HOOD_CROP_POLICY = "bottom-connected-broad-band-v1"


@dataclass
class GeoParams:
    camera_center_x: float = 0.5
    horizon_y: float = 0.42
    bottom_ignore: float = 0.0
    min_run_px: int = 2
    max_run_frac: float = 0.08
    link_tol_px: float = 14.0
    gap_rows: int = 4
    min_segment_rows: int = 8
    dash_max_gap_frac: float = 0.22
    dash_max_dx_px: float = 18.0
    dash_max_dslope: float = 0.45
    max_lanes_export: int = 4
    threshold: float = 0.5

    @classmethod
    def from_cfg(cls, cfg: dict) -> "GeoParams":
        known = {k: v for k, v in (cfg or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Track:
    ys: list = field(default_factory=list)
    xs: list = field(default_factory=list)
    ws: list = field(default_factory=list)
    miss: int = 0

    def predict(self, y: float) -> float:
        n = len(self.ys)
        if n >= 4:
            ys, xs = np.array(self.ys[-10:], float), np.array(self.xs[-10:], float)
            if np.ptp(ys) > 0:
                a, b = np.polyfit(ys, xs, 1)
                return float(a * y + b)
        return float(self.xs[-1])


def _runs(row: np.ndarray) -> list[tuple[int, int]]:
    padded = np.concatenate([[0], row.astype(np.int8), [0]])
    d = np.diff(padded)
    starts, ends = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
    return list(zip(starts.tolist(), ends.tolist()))          # [start, end)


class _Geo:
    def __init__(self, shape: tuple[int, int], p: GeoParams, valid_bottom: int):
        self.H, self.W = shape
        self.p = p
        self.hy = int(round(p.horizon_y * self.H))
        self.by = valid_bottom

    def persp(self, y: float) -> float:
        span = max(1.0, self.by - self.hy)
        return float(np.clip((y - self.hy) / span, 0.12, 1.0))


def _valid_bottom(shape, my_car: np.ndarray | None, p: GeoParams) -> int:
    H, W = shape
    bottom = int(round(H * (1.0 - p.bottom_ignore))) - 1
    if my_car is not None and my_car.any():
        # A global hood cutoff needs a broad band anchored at the actual image
        # bottom. Detached ego-class islands (including a misclassified sky
        # band) must not erase every valid lane farther down the picture.
        # Require broad coverage continuously: a thin vertical bridge to a
        # detached wide island does not establish a real full-width hood.
        broad = (my_car > 0.5).mean(axis=1) > 0.3
        if broad[-1]:
            gaps = np.flatnonzero(~broad)
            first_hood_row = int(gaps[-1]) + 1 if len(gaps) else 0
            bottom = min(bottom, first_hood_row - 2)
    return max(bottom, int(H * p.horizon_y) + 10)


def track_segments(lane: np.ndarray, g: _Geo) -> tuple[list[Track], dict]:
    p = g.p
    active: list[Track] = []
    done: list[Track] = []
    wide_rows = 0
    for y in range(g.by, g.hy - 1, -1):
        pf = g.persp(y)
        max_w = max(3.0, p.max_run_frac * g.W * pf)
        runs = []
        wide = False
        for s, e in _runs(lane[y]):
            w = e - s
            if w < p.min_run_px and pf > 0.3:
                continue
            if w > max_w:
                wide = True
                continue
            runs.append(((s + e - 1) / 2.0, w))
        wide_rows += int(wide)
        pairs = []
        for ti, t in enumerate(active):
            pred = t.predict(y)
            for ri, (cx, w) in enumerate(runs):
                tol = p.link_tol_px * max(0.3, pf) + w / 2.0 + (t.miss * 1.5)
                d = abs(cx - pred)
                if d <= tol:
                    pairs.append((d, ti, ri))
        pairs.sort()
        used_t, used_r = set(), set()
        for d, ti, ri in pairs:
            if ti in used_t or ri in used_r:
                continue
            used_t.add(ti)
            used_r.add(ri)
            t = active[ti]
            t.ys.append(y)
            t.xs.append(runs[ri][0])
            t.ws.append(runs[ri][1])
            t.miss = 0
        still = []
        for ti, t in enumerate(active):
            if ti not in used_t:
                t.miss += 1
            (still if t.miss <= p.gap_rows else done).append(t)
        active = still
        for ri, (cx, w) in enumerate(runs):
            if ri not in used_r:
                active.append(Track([y], [cx], [w]))
    done += active
    return done, {"wide_rows": wide_rows}


@dataclass
class Segment:
    ys: np.ndarray
    xs: np.ndarray
    ws: np.ndarray

    @property
    def y_top(self) -> int:
        return int(self.ys.min())

    @property
    def y_bottom(self) -> int:
        return int(self.ys.max())

    def slope(self) -> float | None:
        if len(self.ys) < 5 or np.ptp(self.ys) < 4:
            return None
        return float(np.polyfit(self.ys, self.xs, 1)[0])


def filter_segments(tracks: list[Track], g: _Geo) -> tuple[list[Segment], dict]:
    keep, stats = [], {"short": 0, "transverse": 0, "stubby": 0}
    for t in tracks:
        ys, xs, ws = np.array(t.ys), np.array(t.xs, float), np.array(t.ws, float)
        if len(ys) == 0:
            continue
        min_rows = max(3, int(round(g.p.min_segment_rows * g.persp(ys.max()))))
        if len(ys) < min_rows:
            stats["short"] += 1
            continue
        seg = Segment(ys, xs, ws)
        s = seg.slope()
        # wide-angle dashcams see lane lines near the image edges at 2-5 px per row; stop lines and
        # crosswalk bars are flatter than ~9 degrees (slope > 6)
        if s is not None and abs(s) > 6.0:
            stats["transverse"] += 1
            continue
        # blob test along the line's own direction: a slanted dash has wide row runs but is still long
        k = 1.0 + (s * s if s is not None else 0.0)
        length = len(ys) * math.sqrt(k)
        thickness = float(np.median(ws)) / math.sqrt(k)
        if length < 1.5 * thickness and thickness > 4:
            stats["stubby"] += 1                        # arrows, diamonds, text: wider than long
            continue
        keep.append(seg)
    return keep, stats


@dataclass
class Chain:
    segments: list[Segment]

    @property
    def ys(self) -> np.ndarray:
        return np.concatenate([s.ys for s in self.segments])

    @property
    def xs(self) -> np.ndarray:
        return np.concatenate([s.xs for s in self.segments])

    @property
    def y_top(self) -> int:
        return min(s.y_top for s in self.segments)

    @property
    def y_bottom(self) -> int:
        return max(s.y_bottom for s in self.segments)

    def top_fit(self, rows: int = 40) -> tuple[float, float] | None:
        ys, xs = self.ys, self.xs
        sel = ys <= self.y_top + rows
        ys, xs = ys[sel], xs[sel]
        if len(ys) < 4 or np.ptp(ys) < 3:
            ys, xs = self.ys, self.xs
        if len(ys) < 2 or np.ptp(ys) < 2:
            return None
        a, b = np.polyfit(ys, xs, 1)
        return float(a), float(b)


def _intervals_overlap(a: Chain, b: Chain, slack: int = 2) -> bool:
    for sa in a.segments:
        for sb in b.segments:
            if min(sa.y_bottom, sb.y_bottom) - max(sa.y_top, sb.y_top) > slack:
                return True
    return False


def _gap_between(a: Chain, b: Chain) -> float:
    """Largest vertical hole the union would have (dash gaps must stay plausible)."""
    spans = sorted([(s.y_top, s.y_bottom) for s in a.segments + b.segments])
    worst, reach = 0.0, spans[0][1]
    for top, bot in spans[1:]:
        worst = max(worst, top - reach)
        reach = max(reach, bot)
    return worst


def _joint_fit(a: Chain, b: Chain, g: _Geo) -> tuple[float, float] | None:
    """(rms, max residual) of one smooth curve through both chains, residuals scaled by depth.

    Two 3-parameter models are tried and the better kept: a quadratic, and the flat-road perspective
    model x = c + a*(y - v) + b/(y - v) (straight or constant-curvature road seen by a pinhole camera),
    with v a little above the configured horizon. Three parameters cannot bend to join two unrelated
    lines, which a cubic could.
    """
    ys = np.concatenate([a.ys, b.ys]).astype(float)
    xs = np.concatenate([a.xs, b.xs]).astype(float)
    span = np.ptp(ys)
    # plain pixel residuals: run centres are ~1 px accurate at any depth, and neighbouring lanes stay
    # tens of px apart until very near the vanishing point (checked on comma10k masks)
    scale = np.ones_like(ys)
    best = None
    if span > 0.15 * g.H and len(ys) >= 12:
        bases = [np.stack([np.ones_like(ys), ys, ys ** 2], 1)]
        for v in (g.hy - 0.02 * g.H, g.hy - 0.06 * g.H):
            d = np.maximum(ys - v, 1.0)
            bases.append(np.stack([np.ones_like(ys), d, 1.0 / d], 1))
    else:
        bases = [np.stack([np.ones_like(ys), ys], 1)]
    for A in bases:
        coef, *_ = np.linalg.lstsq(A / scale[:, None], xs / scale, rcond=None)
        res = np.abs(xs - A @ coef) / scale
        cand = (float(np.sqrt(np.mean(res ** 2))), float(res.max()))
        if best is None or cand[0] < best[0]:
            best = cand
    return best


def group_dashes(segments: list[Segment], g: _Geo) -> list[Chain]:
    """Merge segments into lane lines by agglomerating the pair of chains that a single smooth curve
    explains best, until no pair fits. Chains may interleave in depth but never overlap in rows."""
    p = g.p
    max_gap = p.dash_max_gap_frac * g.H
    rms_tol = max(2.0, p.dash_max_dx_px / 6.0)
    max_tol = p.dash_max_dx_px / 2.0
    chains = [Chain([s]) for s in sorted(segments, key=lambda s: -s.y_bottom)]

    def score(a: Chain, b: Chain) -> float | None:
        if _intervals_overlap(a, b) or _gap_between(a, b) > max_gap:
            return None
        # pieces of one line differ in slope only through curvature; very different slopes = different lines
        ka, kb = Segment(a.ys, a.xs, a.ys).slope(), Segment(b.ys, b.xs, b.ys).slope()
        if ka is not None and kb is not None and abs(ka - kb) > 2 * p.dash_max_dslope:
            return None
        fit = _joint_fit(a, b, g)
        if fit is None or fit[0] > rms_tol or fit[1] > max_tol:
            return None
        return fit[0] + 0.5 * _gap_between(a, b) / max_gap

    pairs: dict[tuple[int, int], float] = {}
    for i in range(len(chains)):
        for j in range(i + 1, len(chains)):
            s = score(chains[i], chains[j])
            if s is not None:
                pairs[(i, j)] = s
    alive = set(range(len(chains)))
    while pairs:
        (i, j), _ = min(pairs.items(), key=lambda kv: kv[1])
        chains[i] = Chain(chains[i].segments + chains[j].segments)
        alive.discard(j)
        pairs = {k: v for k, v in pairs.items() if i not in k and j not in k}
        for k in alive:
            if k == i:
                continue
            a, b = (i, k) if i < k else (k, i)
            s = score(chains[a], chains[b])
            if s is not None:
                pairs[(a, b)] = s
    return [chains[i] for i in sorted(alive)]


# ---- lane curve models ---------------------------------------------------------------------------
# A lane's shape is stored as `fit`: {"basis": "poly", "coef": [...]} (numpy polyval order),
# {"basis": "persp", "coef": [c, a, b], "v": v} for x = c + a*(y-v) + b/(y-v) (flat-road perspective
# model), or {"basis": "interp", "ys": [...], "xs": [...]} for human-drawn polylines used as drawn.

def eval_fit(fit: dict, ys) -> np.ndarray:
    ys = np.asarray(ys, float)
    if fit["basis"] == "poly":
        return np.polyval(fit["coef"], (ys - fit.get("y0", 0.0)) / fit.get("s", 1.0))
    if fit["basis"] == "persp":
        d = np.maximum(ys - fit["v"], 1.0)
        c, a, b = fit["coef"]
        return c + a * d + b / d
    if fit["basis"] == "interp":
        return np.interp(ys, fit["ys"], fit["xs"])
    raise ValueError(fit["basis"])


def lane_xs(lane: dict, ys) -> np.ndarray:
    return eval_fit(lane["fit"], ys)


def _design(basis: str, ys: np.ndarray, deg: int = 2, v: float = 0.0) -> np.ndarray:
    if basis == "poly":
        return np.stack([ys ** k for k in range(deg, -1, -1)], 1)
    d = np.maximum(ys - v, 1.0)
    return np.stack([np.ones_like(ys), d, 1.0 / d], 1)


def _robust_lstsq(A: np.ndarray, xs: np.ndarray) -> tuple[np.ndarray, float]:
    coef, *_ = np.linalg.lstsq(A, xs, rcond=None)
    for _ in range(2):
        res = xs - A @ coef
        mad = np.median(np.abs(res - np.median(res))) + 1e-6
        inl = np.abs(res) <= max(3.0, 3.5 * 1.4826 * mad)
        if inl.sum() < A.shape[1] + 3:
            break
        coef, *_ = np.linalg.lstsq(A[inl], xs[inl], rcond=None)
    rms = float(np.sqrt(np.mean((xs - A @ coef) ** 2)))
    return coef, rms


def fit_lane(ys: np.ndarray, xs: np.ndarray, span_frac: float, horizon: float | None, H: int) -> dict:
    """Robust fit; picks the perspective model when it explains the paint better than a polynomial."""
    ys, xs = np.asarray(ys, float), np.asarray(xs, float)
    if len(ys) < 2 or np.ptp(ys) < 1:
        return {"basis": "poly", "coef": [0.0, float(xs.mean())]}
    y0, s = float(ys.mean()), float(max(1.0, np.ptp(ys) / 2))        # normalised for conditioning
    u = (ys - y0) / s
    poly = lambda c: {"basis": "poly", "coef": [float(x) for x in c], "y0": y0, "s": s}
    if span_frac < 0.15 or len(ys) < 12:
        coef, _ = _robust_lstsq(_design("poly", u, 1), xs)
        return poly(coef)
    coef2, rms2 = _robust_lstsq(_design("poly", u, 2), xs)
    best = (poly(coef2), rms2)
    if horizon is not None:
        for frac in (0.02, 0.06, 0.10):
            v = horizon - frac * H
            if ys.min() - v < 3:
                continue
            c, rms = _robust_lstsq(_design("persp", ys, v=v), xs)
            if rms < best[1] - 0.05:
                best = ({"basis": "persp", "coef": [float(x) for x in c], "v": float(v)}, rms)
    if span_frac > 0.4 and len(ys) > 40 and best[1] > 2.0:
        coef3, rms3 = _robust_lstsq(_design("poly", u, 3), xs)
        if rms3 < 0.7 * best[1]:
            best = (poly(coef3), rms3)
    return best[0]


def _lane_from_chain(ch: Chain, g: _Geo, sample_step: int = 5) -> dict:
    ys, xs = ch.ys.astype(float), ch.xs.astype(float)
    order = np.argsort(ys)
    ys, xs = ys[order], xs[order]
    span = (ys.max() - ys.min()) / max(1.0, g.by - g.hy)
    fit = fit_lane(ys, xs, span, g.hy, g.H)
    observed = set(ch.ys.tolist())
    y_top, y_bot = int(ys.min()), int(ys.max())
    pts = []
    for y in range(y_bot, y_top - 1, -sample_step):
        seen = any((y + d) in observed for d in range(-2, 3))
        pts.append([round(float(eval_fit(fit, y)), 2), int(y), bool(seen)])
    if pts[-1][1] != y_top:
        pts.append([round(float(eval_fit(fit, y_top)), 2), y_top, True])
    gaps = []
    segs = sorted(ch.segments, key=lambda s: -s.y_bottom)
    for lower, upper in zip(segs, segs[1:]):
        if lower.y_top - upper.y_bottom > 2:
            gaps.append([upper.y_bottom, lower.y_top])
    resid = float(np.sqrt(np.mean((xs - eval_fit(fit, ys)) ** 2)))
    return {"fit": fit, "y_top": y_top, "y_bottom": y_bot, "points": pts,
            "segments": len(ch.segments), "kind": "dashed" if gaps else "solid", "gaps": gaps,
            "fit_rms_px": round(resid, 3), "physical": True, "flags": []}


def _x_at(lane: dict, y: float, allow_extrapolate: bool = False) -> float | None:
    if lane["y_top"] - 1 <= y <= lane["y_bottom"] + 1:
        return float(lane_xs(lane, y))
    if not allow_extrapolate:
        return None
    # linear extrapolation from the lowest third of the observed lane (ordering only, never exported)
    pts = np.array([[p[0], p[1]] for p in lane["points"]], float)
    low = pts[pts[:, 1] >= np.percentile(pts[:, 1], 66)]
    if len(low) < 2 or np.ptp(low[:, 1]) < 1:
        low = pts
    if len(low) < 2 or np.ptp(low[:, 1]) < 1:
        return float(pts[0, 0])
    a, b = np.polyfit(low[:, 1], low[:, 0], 1)
    return float(a * y + b)


def _overlap(a: dict, b: dict) -> tuple[int, int] | None:
    lo, hi = max(a["y_top"], b["y_top"]), min(a["y_bottom"], b["y_bottom"])
    return (lo, hi) if hi - lo >= 6 else None


def merge_double_lines(lanes: list[dict], g: _Geo) -> list[dict]:
    """Double yellow / double white lines are one lane line in CULane. Merge parallel close pairs."""
    merged = True
    while merged:
        merged = False
        for i in range(len(lanes)):
            for j in range(i + 1, len(lanes)):
                ov = _overlap(lanes[i], lanes[j])
                if ov is None:
                    continue
                ys = np.arange(ov[0], ov[1] + 1, 3)
                dx = np.abs(lane_xs(lanes[i], ys) - lane_xs(lanes[j], ys))
                # a double line is ~0.3 m centre-to-centre vs ~3.5 m lane width: well under 6% of the image
                # width at the bottom row, while separate lane lines are ~30-60% apart
                lim = np.array([0.06 * g.W * g.persp(y) + 2 for y in ys])
                if (dx <= lim).mean() > 0.8 and dx.std() < 0.5 * lim.mean() + 1:
                    a, b = lanes[i], lanes[j]
                    seen = {}
                    for q in a["points"] + b["points"]:
                        seen[q[1]] = seen.get(q[1], False) or q[2]
                    ys_all = np.array(sorted(seen, reverse=True), float)
                    xs_all = []
                    for y in ys_all:
                        xa, xb = _x_at(a, y), _x_at(b, y)
                        vals = [v for v in (xa, xb) if v is not None]
                        xs_all.append(sum(vals) / len(vals))      # centre of the pair where both exist
                    xs_all = np.array(xs_all, float)
                    fit = fit_lane(ys_all, xs_all, np.ptp(ys_all) / max(1, g.by - g.hy), g.hy, g.H)
                    new = dict(a)
                    new.update(fit=fit, y_top=int(ys_all.min()), y_bottom=int(ys_all.max()))
                    new["points"] = [[round(float(eval_fit(fit, y)), 2), int(y), bool(seen[y])] for y in ys_all]
                    new["gaps"] = a["gaps"] + b["gaps"]
                    new["segments"] = a["segments"] + b["segments"]
                    new["kind"] = "double"
                    # half the stripe spacing, normalised to the bottom row: paint checks look this far
                    # either side of the merged centre line
                    pf = np.array([g.persp(y) for y in ys])
                    new["double_half_sep"] = round(float(np.mean(dx / 2.0 / pf)), 2)
                    new["flags"] = sorted(set(a["flags"]) | set(b["flags"]) | {"double_line"})
                    lanes = [l for k, l in enumerate(lanes) if k not in (i, j)] + [new]
                    merged = True
                    break
            if merged:
                break
    return lanes


def paint_window(lane: dict, y: float, g: "_Geo | None" = None, hy: float = 0, by: float = 1, base: int = 2) -> int:
    """Half-width (px) of the window to look for this lane's paint at row y; wider for double lines."""
    half = float(lane.get("double_half_sep", 0.0))
    if not half:
        return base
    pf = g.persp(y) if g is not None else float(np.clip((y - hy) / max(1.0, by - hy), 0.12, 1.0))
    return int(round(half * pf)) + base + 1


def assign_slots(lanes: list[dict], g: _Geo) -> None:
    ego = g.p.camera_center_x * g.W
    y_ref = g.by
    for l in lanes:
        l["x_ref"] = _x_at(l, y_ref, allow_extrapolate=True)
    min_rows = max(8, int(0.05 * g.H))
    eligible = [l for l in lanes if (l["y_bottom"] - l["y_top"]) >= min_rows]
    for l in lanes:
        if l not in eligible:
            l["flags"].append("fragment")
        l["slot"] = None
    left = sorted([l for l in eligible if l["x_ref"] < ego], key=lambda l: -l["x_ref"])
    right = sorted([l for l in eligible if l["x_ref"] >= ego], key=lambda l: l["x_ref"])
    for slot, l in zip((2, 1), left):
        l["slot"] = slot
    for slot, l in zip((3, 4), right):
        l["slot"] = slot
    for l in left[2:] + right[2:]:
        l["flags"].append("beyond_four_lanes")


def lane_checks(lanes: list[dict], g: _Geo) -> dict:
    tags: dict = {}
    for l in lanes:
        ys = np.arange(l["y_top"], l["y_bottom"] + 1, 4, dtype=float)
        if len(ys) >= 3:
            xs = lane_xs(l, ys)
            line = np.polyval(np.polyfit(ys, xs, 1), ys)
            dev = float(np.abs(xs - line).max())
            span = max(1.0, ys.max() - ys.min())
            l["curvature"] = round(dev / span, 4)
            # wide-angle lenses bend a real curve sideways near the horizon, so only a lane that swings
            # sideways by more than its whole height, or wiggles back and forth, is implausible
            slope_sign = np.sign(np.diff(xs))
            reversals = int((np.abs(np.diff(slope_sign[slope_sign != 0])) > 0).sum())
            if dev / span > 1.0 or reversals > 1:
                l["flags"].append("implausible_curve")
            elif dev / span > 0.04 and dev > 6:
                l["flags"].append("curved")
            if np.any(~np.isfinite(xs)):
                l["flags"].append("invalid_coordinates")
    pairs_cross, pairs_merge = 0, 0
    for i in range(len(lanes)):
        for j in range(i + 1, len(lanes)):
            ov = _overlap(lanes[i], lanes[j])
            if ov is None:
                continue
            # lanes legitimately meet at the vanishing point; ignore the band just below the horizon
            lo = max(ov[0], int(g.hy + 0.12 * (g.by - g.hy)))
            if ov[1] - lo < 6:
                continue
            ys = np.arange(lo, ov[1] + 1, 2)
            d = lane_xs(lanes[i], ys) - lane_xs(lanes[j], ys)
            close = np.abs(d) < np.array([0.012 * g.W * g.persp(y) + 2 for y in ys])
            sep = lambda y: max(6.0, 0.03 * g.W * g.persp(y))   # both ends clearly apart, not just fit noise
            changes = np.nonzero(np.diff(np.sign(d)) != 0)[0]
            # lanes converge toward the vanishing point at the top of their overlap; only a sign change in
            # the lower 70% of the overlap is a real crossing
            real_cross = len(changes) and (ys[changes.max()] - ys[0]) > 0.3 * (ys[-1] - ys[0])
            if real_cross and np.sign(d[0]) != np.sign(d[-1]) and abs(d[0]) > sep(ys[0]) and abs(d[-1]) > sep(ys[-1]):
                pairs_cross += 1
                lanes[i]["flags"].append("crossing")
                lanes[j]["flags"].append("crossing")
            elif close.any() and not close.all():
                pairs_merge += 1
                lanes[i]["flags"].append("merge_or_split")
                lanes[j]["flags"].append("merge_or_split")
    if any("curved" in l["flags"] for l in lanes if l.get("slot") in (2, 3)):
        tags["curve"] = True
    if pairs_merge:
        tags["merge_or_split"] = True
    if pairs_cross:
        tags["lane_crossing"] = True
    return tags


def extract_lanes(lane_prob: np.ndarray, params: GeoParams, my_car: np.ndarray | None = None,
                  movable: np.ndarray | None = None) -> dict:
    H, W = lane_prob.shape
    lane = lane_prob >= params.threshold
    if my_car is not None:
        lane &= my_car < 0.5
    n, labels, stats, _ = cv2.connectedComponentsWithStats(lane.astype(np.uint8), connectivity=8)
    small = np.isin(labels, np.nonzero(stats[:, cv2.CC_STAT_AREA] < 6)[0])
    lane &= ~small
    g = _Geo((H, W), params, _valid_bottom((H, W), my_car, params))
    tracks, tstats = track_segments(lane, g)
    segments, fstats = filter_segments(tracks, g)
    chains = group_dashes(segments, g)
    lanes = [_lane_from_chain(c, g) for c in chains]
    lanes = merge_double_lines(lanes, g)
    assign_slots(lanes, g)
    tags = lane_checks(lanes, g)
    # a "gap" that is really paint swallowed by a crossing stop line / crosswalk bar is not a dash gap
    for l in lanes:
        real = []
        for y0, y1 in l["gaps"]:
            ys = np.arange(y0, y1 + 1)
            xs = np.round(lane_xs(l, ys)).astype(int)
            ok = (xs >= 0) & (xs < W)
            painted = np.array([lane[y, max(0, x - 2):x + 3].any() for y, x in zip(ys[ok], xs[ok])], bool)
            if not len(painted) or painted.mean() < 0.7:
                real.append([y0, y1])
        l["gaps"] = real
        if l["kind"] == "dashed" and not real:
            l["kind"] = "solid"
    # paint confidence along each lane and occlusion inside dash gaps
    for l in lanes:
        vals = []
        for x, y, seen in l["points"]:
            xi, yi = int(round(x)), int(y)
            if 0 <= xi < W and 0 <= yi < H and seen:
                r = paint_window(l, yi, g)
                vals.append(float(lane_prob[yi, max(0, xi - r):xi + r + 1].max()))
        l["mean_confidence"] = round(float(np.mean(vals)), 3) if vals else 0.0
        if vals and l["mean_confidence"] < 0.65:
            l["flags"].append("faded_or_uncertain")
        if movable is not None and l["gaps"]:
            hits = 0
            total = 0
            for y0, y1 in l["gaps"]:
                for y in range(y0, y1 + 1, 3):
                    xi = int(round(float(lane_xs(l, y))))
                    total += 1
                    if 0 <= xi < W and movable[y, max(0, xi - 3):xi + 4].max() >= 0.5:
                        hits += 1
            if total and hits / total > 0.3:
                l["flags"].append("occluded")
                tags["occlusion"] = True
        l["flags"] = sorted(set(l["flags"]))
    if tstats["wide_rows"] > 0.02 * (g.by - g.hy):
        tags["crosswalk_or_stop_line"] = True
    if any("faded_or_uncertain" in l["flags"] for l in lanes):
        tags["faded_markings"] = True
    lanes.sort(key=lambda l: (l["slot"] is None, l["slot"] or 0, l["x_ref"]))
    for k, l in enumerate(lanes):
        l["id"] = k
    tags["lane_count"] = sum(1 for l in lanes if l["slot"])
    return {"width": W, "height": H, "horizon_y": g.hy, "valid_bottom": g.by, "max_run_frac": params.max_run_frac,
            "hood_crop_policy": HOOD_CROP_POLICY,
            "lanes": lanes, "tags": tags,
            "stats": {**tstats, **fstats, "tracks": len(tracks), "segments": len(segments), "chains": len(chains)}}


# ---- canvas / export helpers --------------------------------------------------------------------

@dataclass
class Crop:
    x0: float
    y0: float
    x1: float
    y1: float
    canvas_w: int
    canvas_h: int

    @property
    def sx(self) -> float:
        return self.canvas_w / (self.x1 - self.x0)

    @property
    def sy(self) -> float:
        return self.canvas_h / (self.y1 - self.y0)

    def to_canvas(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.x0) * self.sx, (y - self.y0) * self.sy

    def to_source_y(self, yc: float) -> float:
        return yc / self.sy + self.y0


def make_crop(crop_cfg: dict, width: int, height: int, canvas_w: int, canvas_h: int) -> Crop:
    x0, y0 = float(crop_cfg.get("x0", 0)), float(crop_cfg.get("y0", 0))
    x1 = float(crop_cfg.get("x1", -1))
    y1 = float(crop_cfg.get("y1", -1))
    x1 = width if x1 <= 0 else min(x1, width)
    y1 = height if y1 <= 0 else min(y1, height)
    return Crop(x0, y0, x1, y1, canvas_w, canvas_h)


def sample_rows(lane: dict, crop: Crop, rows_canvas: list[int]) -> list[tuple[float, int]]:
    """Fixed-row sampling: (x_canvas, y_canvas) for each anchor row the observed lane covers."""
    out = []
    for yc in rows_canvas:
        yo = crop.to_source_y(yc)
        x = _x_at(lane, yo)
        if x is None:
            continue
        xc, _ = crop.to_canvas(x, yo)
        if 0 <= xc < crop.canvas_w:
            out.append((round(float(xc), 3), int(yc)))
    return out


def dense_canvas_polyline(lane: dict, crop: Crop, step: float = 2.0) -> np.ndarray:
    ys = np.arange(lane["y_top"], lane["y_bottom"] + 1e-6, step)
    xs = lane_xs(lane, ys)
    pts = np.stack([(xs - crop.x0) * crop.sx, (ys - crop.y0) * crop.sy], 1)
    keep = (pts[:, 1] >= -crop.canvas_h) & (pts[:, 1] <= 2 * crop.canvas_h)
    return pts[keep]
