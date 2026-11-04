"""Diagnostic-only bounded cap trimming; never installs or clears a draft.

All grouping, identity, source retention, native coverage and QC checks remain
the existing implementations. This is an explicit counterfactual for a chosen
connected component, not a general cap classifier or a production converter.
"""
from copy import deepcopy

import numpy as np

from .geometry import GeoParams, Segment, _Geo, lane_xs
from .source_converter_a2d2_v2 import (_from_chain, _group, _outside_four, _paint_type,
                                     _preserve_supported_boundaries, _shape_checks, _slots)
from .source_trace_refinement import components

VERSION = 'diagnostic-native-cap-probe-v1'


def prototype(original, paint, component_bbox, trim_rows, *, solid=None, dashed=None,
              append_preserved_extension=False):
    """Return a source-component counterfactual and construction diagnostics.

Only the chosen component's FIRST 0..4 measured rows can be omitted from its
new segment. Source pixels and any originally supported lane stay intact.
Omitted rows receive no new evidence exemption. Callers must run ordinary
coverage/retention/QC afterward and retain every failure.
"""
    if type(trim_rows) is not int or not 0 <= trim_rows <= 4:
        raise ValueError('Diagnostic trim must be an integer from zero to four')
    strokes = components(original, paint)
    matches = [index for index, stroke in enumerate(strokes) if list(stroke['bbox']) == list(component_bbox)]
    if len(matches) != 1:
        raise ValueError('Chosen native component is absent or ambiguous')
    target = matches[0]
    stroke = strokes[target]
    segment = Segment(stroke['ys'], stroke['xs'], stroke['widths'])
    if _paint_type(segment, solid, dashed) != 1:
        raise ValueError('Cap counterfactual requires confident native dashed paint')
    if len(stroke['ys']) - trim_rows < 5:
        raise ValueError('Insufficient measured interior after bounded trim')
    removed_y = stroke['ys'][:trim_rows]
    removed_x = stroke['xs'][:trim_rows]
    removed_width = stroke['widths'][:trim_rows]
    for lane in original.get('lanes', []):
        if lane.get('slot') not in (1, 2, 3, 4):
            continue
        overlap = (removed_y >= lane['y_top']) & (removed_y <= lane['y_bottom'])
        if np.any(overlap):
            xs = lane_xs(lane, removed_y[overlap])
            if np.any(np.abs(xs - removed_x[overlap]) <= .5 * removed_width[overlap]):
                raise ValueError('Cannot trim cap rows already supporting an original lane')
    segments = [Segment(item['ys'][trim_rows:] if index == target else item['ys'],
                        item['xs'][trim_rows:] if index == target else item['xs'],
                        item['widths'][trim_rows:] if index == target else item['widths'])
                for index, item in enumerate(strokes)]
    chains, joins, warnings = _group(segments, original['width'], original['height'], solid, dashed)
    candidates = [_from_chain(chain) for chain in chains]
    okay, slot_warnings, associations = _slots(candidates, original)
    measured = deepcopy(candidates)
    result = deepcopy(original)
    receipt = {'version': VERSION, 'diagnostic_only': True, 'component_bbox': list(component_bbox),
               'trim_rows': trim_rows, 'omitted_native_rows': removed_y.tolist(),
               'measured_new_anchor': [float(segments[target].xs[0]), int(segments[target].ys[0])],
               'native_components': len(strokes), 'chains': len(chains), 'joins': joins,
               'slot_assignment_ok': okay, 'slot_warnings': slot_warnings,
               'source_pixels_changed': False, 'new_evidence_exemptions': 0,
               'append_preserved_extension': append_preserved_extension}
    if not okay or slot_warnings:
        receipt['construction_failed'] = 'existing_slot_assignment_gate'
        return result, receipt
    retained, preserved = _preserve_supported_boundaries(candidates, associations, original, np.asarray(paint) >= .5)
    receipt.update(preserved_slots=preserved, supported_boundary_preservation=retained)
    if not retained:
        receipt['construction_failed'] = 'existing_supported_boundary_gate'
        return result, receipt
    extensions = []
    if append_preserved_extension:
        for index, old_lanes in associations.items():
            if len(old_lanes) != 1 or not candidates[index].get('preserved_source_boundary'):
                continue
            old, observed = old_lanes[0], measured[index]
            if old.get('slot') != observed.get('slot'):
                continue
            measured_rows = np.asarray(observed['source_observed_rows'], int)
            target_rows = segments[target].ys
            # Append only the chosen component after its ordinary joined chain
            # received this existing slot. Keep all old rows and knots exactly.
            extra = np.intersect1d(measured_rows[measured_rows > old['y_bottom']], target_rows)
            if not len(extra):
                continue
            old_rows = np.unique(np.r_[np.arange(old['y_top'], old['y_bottom'] + 1),
                                       [point[1] for point in old['points']],
                                       old.get('fit', {}).get('ys', [])])
            old_rows = old_rows[(old_rows >= old['y_top']) & (old_rows <= old['y_bottom'])]
            rows = np.r_[old_rows, extra]
            xs = np.r_[lane_xs(old, old_rows), lane_xs(observed, extra)]
            line = candidates[index]
            widths = dict(zip(measured_rows, observed['source_observed_widths']))
            retained_widths = dict(zip(line['source_observed_rows'], line['source_observed_widths']))
            retained_widths.update({int(y): widths[int(y)] for y in extra})
            points = deepcopy(old['points']) + [[float(lane_xs(observed, y)), int(y), True]
                                               for y in extra if y % 5 == 0 or y in (extra[0], extra[-1])]
            line.update(fit={'basis': 'interp', 'ys': rows.tolist(), 'xs': xs.tolist()},
                        y_bottom=int(extra[-1]), points=sorted(points, key=lambda point: -point[1]),
                        source_observed_rows=sorted(retained_widths),
                        source_observed_widths=[retained_widths[y] for y in sorted(retained_widths)],
                        segments=old.get('segments', 1) + 1,
                        gaps=deepcopy(old.get('gaps', [])) + [[old['y_bottom'], int(extra[0])]],
                        diagnostic_preserved_span_extension=True)
            extensions.append({'slot': old['slot'], 'old_extent': [old['y_top'], old['y_bottom']],
                               'new_extent': [line['y_top'], line['y_bottom']],
                               'new_observed_rows': len(extra), 'every_old_row_and_knot_preserved': True})
    receipt['preserved_span_extensions'] = extensions
    for candidate in candidates:
        if not candidate.get('slot') and not _outside_four(candidate, candidates, original['width']):
            candidate['flags'].append('fragment')
            warnings.append('unassigned_source_fragment_requires_review')
    params = GeoParams(horizon_y=original['horizon_y'] / original['height'],
                       max_run_frac=original.get('max_run_frac', .08))
    tags = _shape_checks(candidates, _Geo(np.shape(paint), params, original['valid_bottom']))
    candidates.sort(key=lambda line: (line.get('slot') is None, line.get('slot') or 0, line['x_ref']))
    for index, line in enumerate(candidates):
        line['id'] = index
    result.update(lanes=candidates, tags={**original.get('tags', {}), **tags})
    result['source_converter'] = {'version': VERSION, 'warnings': sorted(set(warnings)),
                                  'diagnostic_only': True, 'cap_probe': receipt,
                                  'requires_fresh_human_audit': True}
    receipt['shape_gate_failures'] = sorted({flag for line in candidates if line.get('slot')
                                             for flag in line.get('flags', [])
                                             if flag in ('crossing', 'invalid_coordinates', 'implausible_curve')})
    return result, receipt
