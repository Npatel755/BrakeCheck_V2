from copy import deepcopy

import numpy as np

from bcdata.source_order_reconstruction import reconstruct, retains_source_slots, normalize_reference
from bcdata.source_target_evidence import measure


def fixture():
    paint = np.zeros((400, 400), np.float32)
    lines = []
    for slot, x in enumerate((50, 140, 260, 350), 1):
        paint[100:391, x-3:x+4] = 1
        lines.append({'slot': slot, 'x_ref': x, 'y_top': 100, 'y_bottom': 390,
                      'fit': {'basis': 'interp', 'ys': [100, 390], 'xs': [x, x]},
                      'points': [[x, y, True] for y in range(390, 99, -5)],
                      'flags': [], 'kind': 'solid'})
    return {'width': 400, 'height': 400, 'horizon_y': 80, 'valid_bottom': 399,
            'max_run_frac': .08, 'lanes': lines, 'source_converter': {'warnings': []}}, paint


def test_old_divider_split_across_slots_recovers_four_actual_boundaries():
    geometry, paint = fixture()
    geometry['lanes'] = [deepcopy(geometry['lanes'][1]), deepcopy(geometry['lanes'][1]),
                         deepcopy(geometry['lanes'][2])]
    geometry['lanes'][0].update(slot=1, y_top=300)
    geometry['lanes'][1].update(slot=2, y_bottom=240)
    original = deepcopy(geometry)
    candidate, proof = reconstruct(geometry, paint)
    assert proof and not proof['old_slots_used_for_assignment']
    assert [(line['slot'], line['x_ref']) for line in candidate['lanes']] == [
        (1, 50), (2, 140), (3, 260), (4, 350)]
    assert measure(candidate, paint)['eligible_for_fresh_audit']
    assert geometry == original


def test_one_sided_paint_cannot_establish_ego_lane_identity():
    geometry, paint = fixture()
    paint[:, 200:] = 0
    candidate, proof = reconstruct(geometry, paint)
    assert proof is None and candidate == geometry


def test_ordinary_refinement_cannot_move_a_supported_lane_to_another_slot():
    geometry, paint = fixture()
    candidate = deepcopy(geometry)
    candidate['lanes'][0]['slot'] = None
    assert not retains_source_slots(geometry, candidate, paint)
    assert retains_source_slots(geometry, geometry, paint)


def test_partial_foreground_order_cannot_invent_missing_boundary_identity():
    geometry, paint = fixture()
    paint[:200] = 0
    paint[100:391, 47:54] = 0
    paint[90:150, 70:77] = 1
    candidate, proof = reconstruct(geometry, paint)
    assert proof is None and candidate == geometry


def test_rounded_source_cap_is_measured_without_extending_the_trace():
    geometry, paint = fixture()
    for x in (50, 140, 260, 350):
        paint[98:100, x-1:x+2] = 1
    original = deepcopy(geometry)
    evidence = measure(geometry, paint)
    assert evidence['bounded_endpoint_cap_rows'] == 8
    assert evidence['eligible_for_fresh_audit']
    assert geometry == original


def test_missing_stroke_extent_is_not_misclassified_as_a_rounded_cap():
    geometry, paint = fixture()
    paint[90:100, 137:144] = 1
    evidence = measure(geometry, paint)
    assert not evidence['eligible_for_fresh_audit']
    assert evidence['bounded_endpoint_cap_rows'] == 0


def test_missing_parallel_dash_remains_unrepresented():
    geometry, paint = fixture()
    paint[220:280, 190:197] = 1
    assert not measure(geometry, paint)['eligible_for_fresh_audit']


def test_mixed_endpoint_coordinates_are_compared_at_one_observed_depth():
    geometry, paint = fixture()
    paint[:, 200:] = 0
    lane, outer = geometry['lanes'][2:]
    ys = np.arange(100, 391)
    xs = np.interp(ys, [100, 390], [260, 330])
    for y, x in zip(ys, xs):
        paint[y, round(x)-3:round(x)+4] = 1
    paint[100:321, 322:329] = 1
    lane.update(x_ref=330, fit={'basis': 'interp', 'ys': [100, 390], 'xs': [260, 330]},
                points=[[float(x), int(y), True] for x, y in zip(xs[::-5], ys[::-5])])
    outer.update(x_ref=325, y_bottom=320,
                 fit={'basis': 'interp', 'ys': [100, 320], 'xs': [325, 325]},
                 points=[[325, y, True] for y in range(320, 99, -5)])
    before = deepcopy(geometry)
    candidate, proof = normalize_reference(geometry, paint)
    assert proof and proof['reference_y'] == 320
    assert candidate['lanes'][2]['x_ref'] < candidate['lanes'][3]['x_ref']
    for old, new in zip(geometry['lanes'], candidate['lanes']):
        assert old['fit'] == new['fit'] and old['points'] == new['points']
    assert geometry == before
