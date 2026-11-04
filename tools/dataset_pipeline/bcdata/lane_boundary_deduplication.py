"""Consolidate supported paired paint without collapsing distinct road lanes."""
from copy import deepcopy

import numpy as np

from .lane_boundary_completion import complete_lane_paths

POLICY = 'outer-main-physical-boundary-consolidation:v1'


def coincident_outer_slots(lanes, width=1280):
    """Reject effectively identical manual labels, including sparse polylines."""
    by_slot = {lane['slot']: lane for lane in lanes}
    duplicates = []
    for outer, main in ((1, 2), (4, 3)):
        if outer not in by_slot or main not in by_slot:
            continue
        a, b = _points(by_slot[outer]), _points(by_slot[main])
        lo, hi = max(a[0, 1], b[0, 1]), min(a[-1, 1], b[-1, 1])
        if hi - lo < 24:
            continue
        ys = np.linspace(lo, hi, 32)
        distance = np.abs(np.interp(ys, a[:, 1], a[:, 0]) - np.interp(ys, b[:, 1], b[:, 0]))
        if np.quantile(distance, .9) <= max(2, .002 * width):
            duplicates.append((outer, main))
    return duplicates


def _points(lane):
    points = np.asarray(lane['points'], float)[:, :2]
    return points[np.argsort(points[:, 1])]


def _observed(lane):
    result = {}
    for section in lane.get('observed_paint_segments', lane.get('segments', [lane['points']])):
        for x, y, *_ in section if isinstance(section, list) else section['points']:
            result.setdefault(float(y), []).append(float(x))
    return {y: float(np.mean(xs)) for y, xs in result.items()}


def _pair_evidence(outer, main, by_slot, width, height):
    a, b = _points(outer), _points(main)
    lo, hi = max(a[0, 1], b[0, 1]), min(a[-1, 1], b[-1, 1])
    if hi - lo < 24:
        return None
    # Far-away convergence alone cannot establish a duplicate boundary.
    ys = np.arange(lo + .35 * (hi - lo), hi + .01)
    xa, xb = np.interp(ys, a[:, 1], a[:, 0]), np.interp(ys, b[:, 1], b[:, 0])
    signed = xa - xb
    if (signed < -2).any() and (signed > 2).any():
        return None
    separation = np.abs(signed)
    slope_difference = abs(float(np.polyfit(ys, xa, 1)[0] - np.polyfit(ys, xb, 1)[0]))
    observed_a, observed_b = _observed(outer), _observed(main)
    common = sorted(observed_a.keys() & observed_b.keys())
    supported = len(common) >= 12
    ratio = None
    if 2 in by_slot and 3 in by_slot:
        left, right = _points(by_slot[2]), _points(by_slot[3])
        selected = (ys >= max(left[0, 1], right[0, 1])) & (ys <= min(left[-1, 1], right[-1, 1]))
        lane_width = np.interp(ys[selected], right[:, 1], right[:, 0]) - np.interp(ys[selected], left[:, 1], left[:, 0])
        usable = lane_width >= .025 * width
        if usable.sum() >= 12:
            ratio = float(np.quantile(separation[selected][usable] / lane_width[usable], .9))
    q90 = float(np.quantile(separation, .9))
    if ratio is not None:
        merge = supported and ratio <= .12 and slope_difference <= .45
        suspicious = ratio <= .25 and q90 <= .06 * width
    else:
        merge = supported and q90 <= .0125 * width and slope_difference <= .18 and hi >= .57 * height
        suspicious = q90 <= .025 * width and slope_difference <= .5
    if not merge and not suspicious:
        return None
    return {'outer_slot': outer['slot'], 'main_slot': main['slot'], 'merge': bool(merge),
            'joint_observed_rows': len(common), 'overlap_rows': round(float(hi - lo), 3),
            'separation_q90_px': round(q90, 4), 'separation_to_ego_width_q90': ratio,
            'slope_difference': round(slope_difference, 4)}


def consolidate_boundaries(geometry):
    if geometry.get('boundary_deduplication', {}).get('policy') == POLICY:
        return geometry
    by_slot = {lane['slot']: lane for lane in geometry['lanes']}
    evidence = []
    for outer, main in ((1, 2), (4, 3)):
        if outer in by_slot and main in by_slot:
            pair = _pair_evidence(by_slot[outer], by_slot[main], by_slot, geometry['width'], geometry['height'])
            if pair:
                evidence.append(pair)
    if not evidence:
        return geometry
    result = deepcopy(geometry)
    by_slot = {lane['slot']: lane for lane in result['lanes']}
    for pair in evidence:
        if not pair['merge']:
            continue
        outer, main = by_slot.pop(pair['outer_slot']), by_slot[pair['main_slot']]
        sections = [deepcopy(section) for lane in (main, outer)
                    for section in lane.get('observed_paint_segments', lane.get('segments', [lane['points']]))]
        main.update(observed_paint_segments=sections,
                    kind='double' if pair['separation_q90_px'] > 2 else main.get('kind', 'lane_marking'),
                    boundary_consolidation={'policy': POLICY, 'same_physical_boundary': True,
                        'merged_from_slots': [pair['main_slot'], pair['outer_slot']],
                        'original_lane_instances': [main['points'], outer['points']], 'evidence': pair})
    result['lanes'] = sorted(by_slot.values(), key=lambda lane: lane['slot'])
    if any(pair['merge'] for pair in evidence):
        result.pop('boundary_completion', None)
        result = complete_lane_paths(result)
    result['boundary_deduplication'] = {'policy': POLICY, 'merged_pairs': [p for p in evidence if p['merge']],
        'suspect_pairs': [p for p in evidence if not p['merge']], 'original_evidence_preserved': True,
        'human_review_created': False}
    return result
