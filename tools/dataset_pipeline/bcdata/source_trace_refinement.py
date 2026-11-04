"""Refine existing slot traces from uniquely associated native source strokes.

No model or human correction is an input. This does not assign missing slots,
merge lane identities, or resolve forks. A complete, single-run component can
extend an existing trace only when its overlap identifies exactly one slot.
The caller must still run topology/QC, regression checks and fresh audits.
"""
from copy import deepcopy

import cv2
import numpy as np

from .geometry import lane_xs, paint_window

VERSION = 'native-source-trace-refinement-v1'
SOFT_WARNINGS = {'unassigned_source_fragment_requires_review'}


def components(geometry, probability):
    paint = np.asarray(probability) >= .5
    paint = paint.copy()
    paint[:geometry['horizon_y']] = False
    paint[geometry['valid_bottom'] + 1:] = False
    count, labels, stats, _ = cv2.connectedComponentsWithStats(paint.astype(np.uint8), 8)
    result = []
    for index in range(1, count):
        x, y, width, height, area = map(int, stats[index])
        if height < max(8, .012 * paint.shape[0]) or height < .15 * width:
            continue
        region = labels[y:y + height, x:x + width] == index
        edges = np.diff(np.pad(region.astype(np.int8), ((0, 0), (1, 1))), axis=1)
        if np.any((edges == 1).sum(axis=1) != 1):
            continue  # A branch cannot establish a single physical stripe.
        starts = (edges == 1).argmax(axis=1)
        stops = (edges == -1).argmax(axis=1)
        rows = np.arange(y, y + height)
        centres = x + .5 * (starts + stops - 1)
        widths = stops - starts
        result.append({'label': index, 'ys': rows, 'xs': centres, 'widths': widths,
                       'bbox': [x, y, width, height], 'area': area})
    return result


def refine(geometry, probability):
    """Return a draft plus evidence; leave ambiguous and unsupported input intact."""
    original = deepcopy(geometry)
    warnings = set(geometry.get('source_converter', {}).get('warnings', []))
    if warnings - SOFT_WARNINGS:
        return original, None
    lanes = [line for line in geometry.get('lanes', []) if line.get('slot') in (1, 2, 3, 4)]
    if not lanes or len({line['slot'] for line in lanes}) != len(lanes):
        return original, None
    strokes = components(geometry, probability)
    matches = {line['slot']: [] for line in lanes}
    evidence = []
    for stroke in strokes:
        possible = []
        for lane in lanes:
            rows = stroke['ys']
            overlap = (rows >= lane['y_top']) & (rows <= lane['y_bottom'])
            if overlap.sum() < max(8, .012 * geometry['height']):
                continue
            distance = np.abs(lane_xs(lane, rows[overlap]) - stroke['xs'][overlap])
            # A centre may move up to eight native pixels, never jump to the
            # other edge of a broad polygon or to a neighbouring boundary.
            if np.quantile(distance, .98) > 8:
                continue
            # Limit endpoint extension; long new spans need separate identity
            # evidence rather than extrapolation from a tiny visible anchor.
            extra = max(0, lane['y_top'] - rows[0]) + max(0, rows[-1] - lane['y_bottom'])
            if extra > .12 * geometry['height']:
                continue
            possible.append(lane['slot'])
        if len(possible) == 1:
            matches[possible[0]].append(stroke)
        elif len(possible) > 1:
            return original, None
    proposed = deepcopy(geometry)
    by_slot = {line['slot']: line for line in proposed['lanes'] if line.get('slot')}
    for slot, strokes in matches.items():
        if not strokes:
            continue
        lane = by_slot[slot]
        rows, centres = {}, {}
        for stroke in strokes:
            for y, x, width in zip(stroke['ys'], stroke['xs'], stroke['widths']):
                if int(y) in rows:
                    return original, None  # Parallel paint cannot share a slot.
                rows[int(y)] = float(x)
                centres[int(y)] = float(width)
        old_top, old_bottom = lane['y_top'], lane['y_bottom']
        ys = np.arange(min(old_top, min(rows)), max(old_bottom, max(rows)) + 1)
        xs = lane_xs(lane, np.clip(ys, old_top, old_bottom))
        for index, y in enumerate(ys):
            if int(y) in rows:
                xs[index] = rows[int(y)]
        # Preserve all old spans, including genuine dash gaps. Do not bridge
        # a missing/unassigned component by shortening the old trace.
        observed = np.array([int(y) in rows for y in ys])
        changed = (int(ys[0]) != old_top or int(ys[-1]) != old_bottom
                   or np.max(np.abs(xs - lane_xs(lane, np.clip(ys, old_top, old_bottom)))) > .25)
        if not changed:
            continue
        fit = {'basis': 'interp', 'ys': ys.tolist(), 'xs': np.round(xs, 4).tolist()}
        # Export every observed row, not a coarse polynomial that bows away.
        sample = sorted(set(range(0, len(ys), 5)) | {0, len(ys) - 1})[::-1]
        lane.update(fit=fit, points=[[round(float(xs[index]), 4), int(ys[index]), bool(observed[index])]
                                    for index in sample],
                    y_top=int(ys[0]), y_bottom=int(ys[-1]))
        old_lane = next(old for old in lanes if old['slot'] == slot)
        shifts = []
        for stroke in strokes:
            overlap = (stroke['ys'] >= old_top) & (stroke['ys'] <= old_bottom)
            shifts.append(float(np.max(np.abs(lane_xs(old_lane, stroke['ys'][overlap]) - stroke['xs'][overlap]))))
        evidence.append({'slot': slot, 'source_components': [stroke['label'] for stroke in strokes],
                         'source_rows': len(rows), 'old_extent': [old_top, old_bottom],
                         'new_extent': [lane['y_top'], lane['y_bottom']],
                         'maximum_overlap_recentre_px': max(shifts)})
    if not evidence:
        return original, None
    proposed.setdefault('source_converter', {})['trace_refinement'] = {'version': VERSION, 'traces': evidence}
    return proposed, {'version': VERSION, 'traces': evidence}


def retains_slot_traces(original, candidate, *, tolerance=8.):
    """Reject identity changes and loss of old visible spans before any audit."""
    old = {line['slot']: line for line in original.get('lanes', []) if line.get('slot')}
    new = {line['slot']: line for line in candidate.get('lanes', []) if line.get('slot')}
    if set(old) != set(new):
        return False
    for slot, line in old.items():
        other = new[slot]
        if other['y_top'] > line['y_top'] or other['y_bottom'] < line['y_bottom']:
            return False
        rows = np.arange(line['y_top'], line['y_bottom'] + 1)
        if np.max(np.abs(lane_xs(line, rows) - lane_xs(other, rows))) > tolerance:
            return False
    return True


def retains_physical_traces(original, candidate, paint):
    """Retain observed source-supported paint even when duplicate slots merge.

    Actual source paint identifies the old trajectory; an unsupported fitted
    excursion cannot force the repaired trace to follow it. New slot identity
    still requires independent native ordering, QC and human sample audits.
    """
    proposed = [line for line in candidate.get('lanes', []) if line.get('slot')]
    # The same native-pixel window test as the scalar reference, using row
    # prefix sums. No repeated interpolation per lane per image row.
    prefix = np.pad(np.cumsum(np.asarray(paint) >= .5, axis=1, dtype=np.int32), ((0, 0), (1, 0)))
    for line in original.get('lanes', []):
        if not line.get('slot'):
            continue
        rows = np.arange(max(0, line['y_top']), min(paint.shape[0] - 1, line['y_bottom']) + 1)
        xs = lane_xs(line, rows)
        valid = np.isfinite(xs) & (xs >= 0) & (xs < paint.shape[1])
        rows, xs = rows[valid], xs[valid]
        radii = np.array([paint_window(line, y, hy=original['horizon_y'], by=original['valid_bottom'], base=4)
                          for y in rows])
        centres = np.rint(xs).astype(int)
        lo, hi = np.maximum(0, centres - radii), np.minimum(paint.shape[1], centres + radii + 1)
        observed = prefix[rows, hi] > prefix[rows, lo]
        matched = np.zeros(len(rows), bool)
        for other in proposed:
            matched |= ((rows >= other['y_top']) & (rows <= other['y_bottom'])
                        & (np.abs(lane_xs(other, rows) - xs) <= 8))
        total = np.count_nonzero(observed)
        if total and np.count_nonzero(matched & observed) / total < .98:
            return False
    return True
