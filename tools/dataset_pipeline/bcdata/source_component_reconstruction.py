"""Reconstruct continuous native paint before trying to associate dash tracks.

Row tracking can split one broad/diagonal semantic polygon into many pieces.
Use connected source strokes directly when each row has one run. Branched
polygons, ambiguous joins and unassigned significant strokes remain held.
"""
from copy import deepcopy

import numpy as np

from .geometry import Segment
from .source_trace_refinement import components
from .source_converter_a2d2_v2 import (_group, _from_chain, _slots, _outside_four,
                                     _preserve_supported_boundaries, _shape_checks)
from .geometry import GeoParams, _Geo

VERSION = 'native-source-components-v1'


def reconstruct(original, paint, *, solid=None, dashed=None):
    result = deepcopy(original)
    strokes = components(original, paint)
    if not strokes or len(strokes) > 100:
        return result, None
    segments = [Segment(stroke['ys'], stroke['xs'], stroke['widths']) for stroke in strokes]
    chains, joins, warnings = _group(segments, original['width'], original['height'], solid, dashed)
    candidates = [_from_chain(chain) for chain in chains]
    okay, slot_warnings, matches = _slots(candidates, original)
    if not okay or slot_warnings:
        return result, None
    retained, preserved = _preserve_supported_boundaries(candidates, matches, original, paint >= .5)
    if not retained:
        return result, None
    for candidate in candidates:
        if not candidate.get('slot'):
            if not _outside_four(candidate, candidates, original['width']):
                candidate['flags'].append('fragment')
                warnings.append('unassigned_source_fragment_requires_review')
    params = GeoParams(horizon_y=original['horizon_y'] / original['height'],
                       max_run_frac=original.get('max_run_frac', .08))
    g = _Geo(paint.shape, params, original['valid_bottom'])
    tags = _shape_checks(candidates, g)
    if any(set(line.get('flags', [])) & {'crossing', 'invalid_coordinates', 'implausible_curve'}
           for line in candidates if line.get('slot')):
        return result, None
    candidates.sort(key=lambda line: (line.get('slot') is None, line.get('slot') or 0, line['x_ref']))
    for index, line in enumerate(candidates):
        line['id'] = index
    result.update(lanes=candidates, tags={**original.get('tags', {}), **tags})
    evidence = {'version': VERSION, 'native_components': len(strokes), 'chains': len(chains),
                'joins': joins, 'preserved_slots': preserved, 'source_only': True,
                'old_warnings': original.get('source_converter', {}).get('warnings', [])}
    result['source_converter'] = {'version': VERSION, 'warnings': sorted(set(warnings)),
                                  'component_reconstruction': evidence,
                                  'requires_fresh_human_audit': True}
    return result, evidence
