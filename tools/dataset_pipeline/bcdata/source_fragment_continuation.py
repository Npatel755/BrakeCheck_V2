"""Extend an existing lane into a uniquely supported source fragment.

No new slots, source pixels, or model predictions are introduced. Unlike a
global refit, this preserves the complete existing curve and only extends its
observed end. Ambiguous partners and source-type changes remain manual.
"""
from copy import deepcopy
import math

import numpy as np

from .geometry import lane_xs

VERSION = 'source-fragment-continuation-v1'


def _observed(line, paint):
    height, width = paint.shape
    rows, xs = [], []
    for y in range(max(0, int(line['y_top'])), min(height - 1, int(line['y_bottom'])) + 1):
        x = float(lane_xs(line, y))
        if not np.isfinite(x) or not 0 <= x < width:
            continue
        xi = round(x)
        start, stop = max(0, xi - 4), min(width, xi + 5)
        hits = np.flatnonzero(paint[y, start:stop])
        if len(hits):
            # Use the existing trajectory as an anchor, not an unrelated run.
            rows.append(y)
            xs.append(x)
    return np.asarray(rows, float), np.asarray(xs, float)


def _tangent(rows, xs, top):
    edge = rows.min() if top else rows.max()
    use = np.abs(rows - edge) <= max(12., min(40., .4 * np.ptp(rows)))
    if use.sum() < 8 or np.ptp(rows[use]) < 7:
        return None
    fit = np.polyfit(rows[use], xs[use], 1)
    if np.max(np.abs(np.polyval(fit, rows[use]) - xs[use])) > 3:
        return None
    return fit


def _semantic_type(rows, xs, solid, dashed):
    if solid is None or dashed is None:
        return None
    yi = rows.astype(int)
    xi = np.clip(np.round(xs).astype(int), 0, np.shape(solid)[1] - 1)
    values = [np.asarray(mask)[yi, xi].sum() for mask in (solid, dashed)]
    return int(np.argmax(values)) if max(values) >= .9 * max(1, sum(values)) else None


def _join(a, b, paint, solid, dashed):
    ar, ax = _observed(a, paint)
    br, bx = _observed(b, paint)
    if min(len(ar), len(br)) < max(12, .015 * paint.shape[0]):
        return None
    if max(ar.min(), br.min()) <= min(ar.max(), br.max()):
        return None
    lr, lx, ur, ux = (ar, ax, br, bx) if ar.min() > br.max() else (br, bx, ar, ax)
    gap = lr.min() - ur.max()
    # A long occlusion, a fork, or opposing marking types is not repaired.
    if not 1 < gap <= .12 * paint.shape[0]:
        return None
    lt, ut = _tangent(lr, lx, True), _tangent(ur, ux, False)
    if lt is None or ut is None:
        return None
    angle = abs(math.atan(lt[0]) - math.atan(ut[0]))
    if angle > math.radians(6) or max(abs(lt[0]), abs(ut[0])) > 4:
        return None
    endpoint_error = max(abs(np.polyval(lt, ur.max()) - ux[-1]),
                         abs(np.polyval(ut, lr.min()) - lx[0]))
    if endpoint_error > max(5., .004 * paint.shape[1]):
        return None
    secant = math.atan((lx[0] - ux[-1]) / gap)
    near = gap <= .02 * paint.shape[0] and endpoint_error <= 5
    if not near and max(abs(secant - math.atan(t[0])) for t in (lt, ut)) > math.radians(10):
        return None
    at, bt = _semantic_type(ar, ax, solid, dashed), _semantic_type(br, bx, solid, dashed)
    if at is not None and bt is not None and at != bt:
        return None
    return {'gap_rows': int(gap), 'endpoint_error_px': float(endpoint_error),
            'tangent_difference_degrees': math.degrees(angle),
            'original_observed_rows': len(ar), 'fragment_observed_rows': len(br)}


def repair_geometry(original, paint, *, solid=None, dashed=None):
    """Return a draft plus measured evidence. Inputs are never modified."""
    paint = np.asarray(paint) >= .5
    if paint.shape != (original['height'], original['width']):
        raise ValueError('Source dimensions differ from geometry')
    result = deepcopy(original)
    evidence = []
    used = set()
    # More than one possible partner at either end is left to the reviewer.
    for _ in range(12):
        pairs = []
        lanes = result.get('lanes', [])
        for parent_index, parent in enumerate(lanes):
            if parent.get('slot') not in (1, 2, 3, 4):
                continue
            for fragment_index, fragment in enumerate(lanes):
                if fragment_index in used or fragment.get('slot') or 'fragment' not in fragment.get('flags', []):
                    continue
                if set(fragment.get('flags', [])) & {'beyond_four_lanes', 'double_line', 'crossing', 'invalid_coordinates'}:
                    continue
                if (fragment['y_top'] <= parent['y_bottom'] and fragment['y_bottom'] >= parent['y_top']
                        or fragment['y_bottom'] > original['valid_bottom']):
                    continue
                proof = _join(parent, fragment, paint, solid, dashed)
                if proof:
                    pairs.append((parent_index, fragment_index, proof))
        unique = [(pi, fi, proof) for pi, fi, proof in pairs
                  if sum(other_fi == fi for _, other_fi, _ in pairs) == 1
                  and sum(other_pi == pi for other_pi, _, _ in pairs) == 1]
        if not unique:
            break
        for pi, fi, proof in unique:
            parent, fragment = lanes[pi], lanes[fi]
            fr, fx = _observed(fragment, paint)
            # Keep every original row and knot. Sample integer rows as well so
            # a quadratic curve cannot change shape during interpolation.
            rows = np.unique(np.r_[np.arange(parent['y_top'], parent['y_bottom'] + 1),
                                   [p[1] for p in parent['points']], fr])
            values = np.array([float(lane_xs(parent, y)) if parent['y_top'] <= y <= parent['y_bottom']
                               else float(np.interp(y, fr, fx)) for y in rows])
            points = deepcopy(parent['points']) + [[float(x), int(y), True] for y, x in zip(fr, fx) if int(y) % 5 == 0 or y in (fr[0], fr[-1])]
            parent.update(fit={'basis': 'interp', 'ys': rows.tolist(), 'xs': values.tolist()},
                          points=sorted(points, key=lambda p: -p[1]),
                          y_top=int(rows.min()), y_bottom=int(rows.max()))
            used.add(fi)
            evidence.append({'slot': parent['slot'], 'fragment_id': fragment.get('id'), **proof})
    if evidence:
        result['lanes'] = [line for index, line in enumerate(result['lanes']) if index not in used]
        previous = deepcopy(original.get('source_converter', {}))
        warnings = list(previous.get('warnings', []))
        # Only the exact missing-fragment mechanism can be resolved here.
        # Uncovered source paint and all other topology warnings remain QC gates.
        unresolved = [line for line in result['lanes'] if not line.get('slot')
                      and 'fragment' in line.get('flags', [])
                      and line['y_bottom'] - line['y_top'] >= .025 * original['height']]
        resolved = []
        if not unresolved and 'unassigned_source_fragment_requires_review' in warnings:
            warnings.remove('unassigned_source_fragment_requires_review')
            resolved.append('unassigned_source_fragment_requires_review')
        result['source_converter'] = {'version': VERSION, 'risk_family': 'unique_observed_fragment_extension',
                                      'warnings': warnings, 'resolved_warnings': resolved,
                                      'previous_converter': previous, 'continuations': evidence,
                                      'source_only': True, 'requires_fresh_human_audit': True}
    return result, evidence
