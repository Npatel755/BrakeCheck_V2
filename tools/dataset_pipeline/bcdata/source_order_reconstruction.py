"""Recover lane identity from native foreground paint, independently of old slots.

Only a unique measured order, with foreground paint on both sides of the
camera centre, may replace old identities. No model or human trace is input.
Unresolved fragments and old source-supported traces still gate publication.
"""
from copy import deepcopy

import numpy as np

from .geometry import Segment, GeoParams, _Geo, lane_xs
from .source_trace_refinement import components, retains_physical_traces
from .source_converter_a2d2_v2 import (
    _group, _from_chain, _pairwise_foreground_order, _relative,
    _outside_four, _shape_checks,
)

VERSION = 'native-foreground-order-v1'


def retains_source_slots(original, candidate, paint):
    """An ordinary refinement cannot move supported paint to another slot."""
    for lane in original.get('lanes', []):
        if not lane.get('slot'):
            continue
        before = {**original, 'lanes': [lane]}
        after = {**candidate, 'lanes': [line for line in candidate.get('lanes', [])
                                      if line.get('slot') == lane['slot']]}
        if not retains_physical_traces(before, after, paint):
            return False
    return True


def normalize_reference(geometry, paint):
    """Compare slot coordinates at one measured foreground depth, not mixed ends."""
    lanes = [deepcopy(line) for line in geometry.get('lanes', []) if line.get('slot')]
    if len(lanes) < 2 or len({line['slot'] for line in lanes}) != len(lanes):
        return deepcopy(geometry), None
    for line in lanes:
        rows = np.arange(max(0, line['y_top']), min(paint.shape[0] - 1, line['y_bottom']) + 1)
        xs = lane_xs(line, rows)
        valid = np.isfinite(xs) & (xs >= 0) & (xs <= paint.shape[1] - 1)
        rows, xs = rows[valid], xs[valid]
        observed = np.asarray(paint)[rows, np.rint(xs).astype(int)] >= .5
        line['source_observed_rows'] = rows[observed].tolist()
    ordered = _pairwise_foreground_order(lanes, geometry)
    if ordered is None or [line['slot'] for line in ordered] != sorted(line['slot'] for line in lanes):
        return deepcopy(geometry), None
    top, bottom = max(line['y_top'] for line in lanes), min(line['y_bottom'] for line in lanes)
    if bottom - top < max(8, .015 * geometry['height']):
        return deepcopy(geometry), None
    values = {line['slot']: float(lane_xs(line, bottom)) for line in lanes}
    if any(values[b['slot']] <= values[a['slot']] for a, b in zip(ordered, ordered[1:])):
        return deepcopy(geometry), None
    if ((2 in values and values[2] >= .5 * geometry['width'])
            or (3 in values and values[3] <= .5 * geometry['width'])):
        return deepcopy(geometry), None
    result, changes = deepcopy(geometry), []
    for line in result['lanes']:
        if line.get('slot') in values and line.get('x_ref') != values[line['slot']]:
            changes.append({'slot': line['slot'], 'old_x_ref': line.get('x_ref'),
                            'new_x_ref': values[line['slot']]})
            line['x_ref'] = values[line['slot']]
    evidence = {'reference_y': bottom, 'changes': changes,
                'unique_measured_order': True, 'trace_geometry_changed': False} if changes else None
    return result, evidence


def reconstruct(original, paint, *, solid=None, dashed=None):
    unchanged = deepcopy(original)
    strokes = components(original, paint)
    if not strokes or len(strokes) > 100:
        return unchanged, None
    segments = [Segment(s['ys'], s['xs'], s['widths']) for s in strokes]
    chains, joins, warnings = _group(segments, original['width'], original['height'], solid, dashed)
    if warnings:
        return unchanged, None
    candidates = [_from_chain(chain) for chain in chains]
    near = original['horizon_y'] + .55 * (original['valid_bottom'] - original['horizon_y'])
    minimum_rows = max(12, .02 * original['height'])
    anchors = [line for line in candidates
               if np.count_nonzero(np.asarray(line['source_observed_rows']) >= near) >= minimum_rows]
    if len(anchors) < 2:
        return unchanged, None
    ordered = _pairwise_foreground_order(anchors, original)
    if ordered is None:
        return unchanged, None
    # Use an actual common curve span. Shared painted rows establish ordering;
    # only the reference position may interpolate a bounded dash gap.
    top = max(max(near, line['y_top']) for line in anchors)
    bottom = min(line['y_bottom'] for line in anchors)
    if bottom - top < max(8, .015 * original['height']):
        return unchanged, None
    rows = np.arange(int(np.ceil(top)), int(np.floor(bottom)) + 1)
    centre, margin = .5 * original['width'], max(12, .015 * original['width'])
    profiles = [lane_xs(line, rows) for line in ordered]
    # A slot cannot switch sides within the measured reference interval.
    left = [i for i, xs in enumerate(profiles) if np.all(xs < centre - margin)]
    right = [i for i, xs in enumerate(profiles) if np.all(xs > centre + margin)]
    if not left or not right or len(left) + len(right) != len(ordered):
        return unchanged, None
    if left != list(range(len(left))) or right != list(range(len(left), len(ordered))):
        return unchanged, None
    for indices, slots in ((left[::-1], (2, 1)), (right, (3, 4))):
        for index, slot in zip(indices, slots):
            ordered[index]['slot'] = slot
    occupied = {line['slot'] for line in candidates if line.get('slot')}
    anchor_ids = {id(line) for line in anchors}
    for candidate in candidates:
        candidate['x_ref'] = float(lane_xs(candidate, bottom if id(candidate) in anchor_ids else candidate['y_bottom']))
        if candidate.get('slot'):
            continue
        possible, constraints = set(range(1, 5)) - occupied, 0
        for anchor in ordered:
            if not anchor.get('slot'):
                continue
            relation = _relative(candidate, anchor, original['height'])
            if relation:
                constraints += 1
                possible &= (set(range(1, anchor['slot'])) if relation < 0
                             else set(range(anchor['slot'] + 1, 5)))
        if constraints and len(possible) == 1:
            candidate['slot'] = possible.pop()
            occupied.add(candidate['slot'])
    for candidate in candidates:
        if not candidate.get('slot') and not _outside_four(candidate, candidates, original['width']):
            return unchanged, None
    params = GeoParams(horizon_y=original['horizon_y'] / original['height'],
                       max_run_frac=original.get('max_run_frac', .08))
    tags = _shape_checks(candidates, _Geo(paint.shape, params, original['valid_bottom']))
    if any(set(line.get('flags', [])) & {'crossing', 'invalid_coordinates', 'implausible_curve'}
           for line in candidates if line.get('slot')):
        return unchanged, None
    candidate = deepcopy(original)
    candidates.sort(key=lambda line: (line.get('slot') is None, line.get('slot') or 0, line['x_ref']))
    for index, line in enumerate(candidates):
        line['id'] = index
    candidate.update(lanes=candidates, tags={**original.get('tags', {}), **tags})
    if not retains_physical_traces(original, candidate, paint):
        return unchanged, None
    evidence = {'version': VERSION, 'native_components': len(strokes), 'joins': joins,
                'foreground_order': [line['slot'] for line in ordered],
                'reference_interval': [float(top), float(bottom)],
                'source_only': True, 'old_slots_used_for_assignment': False}
    candidate['source_converter'] = {'version': VERSION, 'warnings': [],
                                     'foreground_order': evidence, 'requires_fresh_human_audit': True}
    return candidate, evidence
