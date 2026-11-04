"""Native paint evidence respecting the existing four-boundary label policy.

A verified double marking has one centre between its two stripes. Paint
beyond the outermost of four targets is outside the target only where two
retained, source-supported neighbours prove that ordering at every run row.
Neither exception uses extrapolated curves or a model prediction.
"""
import cv2
import numpy as np

from .geometry import lane_xs
from .source_run_evidence import measure as strict_measure

VERSION = 'native-four-target-evidence-v2'


def endpoint_caps(lanes, profiles, runs):
    """Account only for measured rounded paint caps just beyond a trace end.

    The endpoint must lie in native paint. At most four rows of the same
    unbranched run may continue, bounded by half its measured stroke width.
    No interior gap or separate dash is explained by this allowance.
    """
    caps = {}
    for slot, line in lanes.items():
        for endpoint, direction in ((line['y_top'], -1), (line['y_bottom'], 1)):
            y = int(endpoint)
            if y < 0 or y >= len(profiles[slot]):
                continue
            x = profiles[slot][y]
            matches = [(a, b) for a, b in runs.get(y, []) if a <= x <= b]
            if len(matches) != 1:
                continue
            a, b = matches[0]
            radius = min(4, int((b - a + 1) / 2))
            previous = (a, b)
            proposed = []
            for distance in range(1, radius + 1):
                row = y + direction * distance
                touching = [(lo, hi) for lo, hi in runs.get(row, [])
                            if lo <= previous[1] + 1 and hi >= previous[0] - 1]
                if len(touching) != 1:
                    break
                lo, hi = touching[0]
                # A cap narrows around the endpoint, rather than drifting
                # along a missing longitudinal span or widening into a fork.
                if not lo <= x <= hi or hi - lo > b - a:
                    break
                proposed.append((row, lo, hi))
                previous = (lo, hi)
            if proposed:
                next_row = proposed[-1][0] + direction
                continues = any(lo <= previous[1] + 1 and hi >= previous[0] - 1
                                for lo, hi in runs.get(next_row, []))
                if not continues and previous[1] - previous[0] < b - a:
                    caps.update({run: slot for run in proposed})
    return caps


def measure(geometry, probability):
    result = strict_measure(geometry, probability)
    paint = np.asarray(probability) >= .5
    paint = paint.copy()
    paint[:geometry['horizon_y']] = False
    paint[geometry['valid_bottom'] + 1:] = False
    height, width = paint.shape
    lanes = {line['slot']: line for line in geometry.get('lanes', []) if line.get('slot')}
    profiles = {}
    for slot, line in lanes.items():
        values = np.full(height, np.nan)
        rows = np.arange(max(0, line['y_top']), min(height - 1, line['y_bottom']) + 1)
        values[rows] = lane_xs(line, rows)
        profiles[slot] = values
    runs = {}
    for y in np.flatnonzero(paint.any(axis=1)):
        delta = np.diff(np.r_[0, paint[y].astype(np.int8), 0])
        runs[int(y)] = list(zip(np.flatnonzero(delta == 1).tolist(),
                                (np.flatnonzero(delta == -1) - 1).tolist()))
    caps = endpoint_caps(lanes, profiles, runs)
    double_rows = {slot: {} for slot, line in lanes.items()
                   if line.get('kind') == 'double' or 'double_line' in line.get('flags', [])}
    for slot, observations in double_rows.items():
        for y, row in runs.items():
            x = profiles[slot][y]
            if not np.isfinite(x):
                continue
            perspective = np.clip((y - geometry['horizon_y']) /
                                  max(1, geometry['valid_bottom'] - geometry['horizon_y']), .12, 1.)
            for a, b in zip(row, row[1:]):
                if not a[1] < x < b[0]:
                    continue
                centres = [.5 * sum(a), .5 * sum(b)]
                separation = centres[1] - centres[0]
                if (separation > max(8., .03 * width * perspective)
                        or abs(.5 * sum(centres) - x) > 3.
                        or max(a[1] - a[0] + 1, b[1] - b[0] + 1) >
                           3 * min(a[1] - a[0] + 1, b[1] - b[0] + 1)):
                    continue
                if any(np.isfinite(values[y]) and a[0] - 4 <= values[y] <= b[1] + 4
                       for other, values in profiles.items() if other != slot):
                    continue
                observations[y] = (a, b)
    double_rows = {slot: rows for slot, rows in double_rows.items()
                   if len(rows) >= max(20, .025 * height)}

    def supported(slot, y):
        value = profiles[slot][y]
        return (np.isfinite(value) and
                (any(start <= value <= stop for start, stop in runs.get(y, []))
                 or y in double_rows.get(slot, {})))

    count, labels, stats, _ = cv2.connectedComponentsWithStats(paint.astype(np.uint8), 8)
    total = hits = 0
    unsupported, outside, double_hits = [], [], 0
    for index in range(1, count):
        x, y, w, h, _ = map(int, stats[index])
        if h < 8 or h < .15 * w:
            continue
        measurements = []
        for row in range(y, y + h):
            mask = labels[row, x:x + w] == index
            delta = np.diff(np.r_[0, mask.astype(np.int8), 0])
            for start, stop in zip(np.flatnonzero(delta == 1), np.flatnonzero(delta == -1)):
                measurements.append((row, x + int(start), x + int(stop) - 1))
        outboard = False
        for outer, inner, side in ((1, 2, -1), (4, 3, 1)):
            if outer not in profiles or inner not in profiles:
                continue
            margin = max(12., .02 * width)
            if measurements and all(supported(outer, row) and supported(inner, row)
                    and (profiles[outer][row] - profiles[inner][row]) * side > margin
                    and min((edge - profiles[outer][row]) * side for edge in (start, stop)) > margin
                    for row, start, stop in measurements):
                outboard = True
                outside.append({'bbox': [x, y, w, h], 'source_run_rows': len(measurements),
                                'outer_slot': outer, 'inner_slot': inner,
                                'all_rows_source_supported': True})
                break
        if outboard:
            continue
        matched = 0
        for row, start, stop in measurements:
            direct = any(np.isfinite(values[row]) and start - 1 <= values[row] <= stop + 1
                         for values in profiles.values())
            paired = any(any(a <= start and stop <= b for a, b in observed[row])
                         for observed in double_rows.values() if row in observed)
            matched += bool(direct or paired or (row, start, stop) in caps)
            double_hits += bool(paired and not direct)
        total += len(measurements)
        hits += matched
        if h >= max(8, .025 * height) and matched / max(1, len(measurements)) < .98:
            unsupported.append({'bbox': [x, y, w, h], 'source_run_rows': len(measurements),
                                'represented_fraction': matched / max(1, len(measurements))})
    fraction = hits / total if total else None
    eligible = bool(lanes and total >= 20 and fraction >= .98
                    and result['observed_points_on_paint'] >= .98 and not unsupported
                    and not result['unhandled_converter_warnings'] and not result['crossings']
                    and not result['near_duplicate_slot_pairs']
                    and len(lanes) == sum(bool(line.get('slot')) for line in geometry['lanes']))
    return {**result, 'version': VERSION, 'source_run_rows': total, 'represented_rows': hits,
            'represented_fraction': fraction, 'unrepresented_components': unsupported,
            'verified_beyond_four_components': outside,
            'verified_double_marking_rows': {str(slot): len(rows) for slot, rows in double_rows.items()},
            'double_stripe_runs_represented': double_hits,
            'bounded_endpoint_cap_rows': len(caps),
            'eligible_for_fresh_audit': eligible}
