"""Measure whether native paint runs are represented, independent of stroke width.

Wide semantic strokes need a centreline, not a filled polygon. A run must
contain a real exported curve at its measured row; interpolation outside a
curve's extent cannot explain a missing source fragment. This evidence only
routes eligible conversions to fresh human sample audits.
"""
import cv2
import numpy as np

from .geometry import lane_xs
from .qc import mask_polyline_consistency

VERSION = 'native-source-run-evidence-v1'
SOFT = {'low_mask_coverage', 'source_boundary_uncovered', 'source_conversion_uncertain',
        'repair_unassigned_source_fragment_requires_review', 'short_lane', 'faded_or_uncertain'}


def measure(geometry, probability):
    # Inspect native source pixels, including wide longitudinal strokes. Reuse
    # neither the proposed lane's raster nor a per-row width filter that could
    # hide the very missing source extent this evidence is meant to catch.
    paint = np.asarray(probability) >= .5
    paint = paint.copy()
    paint[:geometry['horizon_y']] = False
    paint[geometry['valid_bottom'] + 1:] = False
    height, width = paint.shape
    lanes = [line for line in geometry.get('lanes', []) if line.get('slot') in (1, 2, 3, 4)]
    # One native row/run is one measurement, regardless of its area.
    profiles = {}
    for line in lanes:
        values = np.full(height, np.nan)
        lo, hi = max(0, int(np.ceil(line['y_top']))), min(height - 1, int(np.floor(line['y_bottom'])))
        rows = np.arange(lo, hi + 1)
        values[rows] = lane_xs(line, rows)
        profiles[line['slot']] = values
    count, labels, stats, _ = cv2.connectedComponentsWithStats(paint.astype(np.uint8), 8)
    measurements = hits = 0
    unsupported = []
    transverse = 0
    for index in range(1, count):
        x, y, w, h, area = map(int, stats[index])
        if h < 8 or h < .15 * w:
            transverse += 1
            continue
        total = matched = 0
        for row in range(y, y + h):
            mask = labels[row, x:x + w] == index
            delta = np.diff(np.r_[0, mask.astype(np.int8), 0])
            for start, stop in zip(np.flatnonzero(delta == 1), np.flatnonzero(delta == -1)):
                start, stop = x + int(start), x + int(stop) - 1
                # No broad tolerance window, nearest-curve search or projection.
                # A line must actually pass through this source stroke.
                represented = any(np.isfinite(values[row]) and start - 1 <= values[row] <= stop + 1
                                  for values in profiles.values())
                total += 1
                matched += int(represented)
        measurements += total
        hits += matched
        if h >= max(8, .025 * height) and matched / max(1, total) < .98:
            unsupported.append({'bbox': [x, y, w, h], 'source_run_rows': total,
                                'represented_fraction': matched / max(1, total)})
    _, on_paint = mask_polyline_consistency(geometry, probability)
    coverage = hits / measurements if measurements else None
    warnings = geometry.get('source_converter', {}).get('warnings', [])
    unhandled_warnings = sorted(set(warnings) - {'unassigned_source_fragment_requires_review'})
    from tools.evaluate_source_v4 import crossing_checks
    # Check ordering over shared visible extents; no extrapolated intersections.
    crossings = crossing_checks(geometry)
    ambiguous = []
    for first in lanes:
        for second in lanes:
            if first['slot'] >= second['slot']:
                continue
            a, b = profiles[first['slot']], profiles[second['slot']]
            shared = np.isfinite(a) & np.isfinite(b)
            if np.count_nonzero(shared & (np.abs(a - b) < 8)) >= 5:
                ambiguous.append([first['slot'], second['slot']])
    eligible = bool(lanes and measurements >= 20 and coverage >= .98 and on_paint >= .98
                    and not unsupported and not unhandled_warnings and not crossings
                    and len(profiles) == len(lanes) and not ambiguous)
    return {'version': VERSION, 'source_run_rows': measurements, 'represented_rows': hits,
            'represented_fraction': coverage, 'observed_points_on_paint': on_paint,
            'unrepresented_components': unsupported, 'transverse_components': transverse,
            'unhandled_converter_warnings': unhandled_warnings,
            'crossings': crossings, 'near_duplicate_slot_pairs': ambiguous,
            'eligible_for_fresh_audit': eligible}


def audit_candidate(geometry, probability, reasons):
    if not reasons or not set(reasons) <= SOFT:
        return None
    result = measure(geometry, probability)
    return result if result['eligible_for_fresh_audit'] else None
