from copy import deepcopy

import numpy as np

from bcdata.qc import mask_polyline_consistency
from bcdata.source_run_evidence import audit_candidate, measure


def fixture():
    line = {'id': 0, 'slot': 2, 'x_ref': 100, 'y_top': 100, 'y_bottom': 390,
            'fit': {'basis': 'interp', 'ys': [100, 390], 'xs': [100, 100]},
            'points': [[100, y, True] for y in range(390, 99, -5)], 'flags': []}
    geometry = {'width': 400, 'height': 400, 'horizon_y': 80, 'valid_bottom': 399,
                'max_run_frac': .25, 'lanes': [line], 'source_converter': {'warnings': []}}
    paint = np.zeros((400, 400), np.float32)
    paint[100:391, 80:121] = 1
    return geometry, paint


def test_width_biased_area_flag_can_receive_a_fresh_audit():
    geometry, paint = fixture()
    before = deepcopy(geometry)
    assert mask_polyline_consistency(geometry, paint)[0] < .6
    proof = audit_candidate(geometry, paint, ['low_mask_coverage'])
    assert proof['represented_fraction'] == 1
    assert proof['eligible_for_fresh_audit']
    assert geometry == before


def test_missing_parallel_boundary_is_not_excused_by_a_long_correct_line():
    geometry, paint = fixture()
    paint[220:300, 190:198] = 1
    assert not measure(geometry, paint)['eligible_for_fresh_audit']
    assert audit_candidate(geometry, paint, ['source_boundary_uncovered']) is None


def test_missing_dash_extent_cannot_use_extrapolation():
    geometry, paint = fixture()
    geometry['lanes'][0]['y_top'] = 200
    geometry['lanes'][0]['points'] = [p for p in geometry['lanes'][0]['points'] if p[1] >= 200]
    assert not measure(geometry, paint)['eligible_for_fresh_audit']


def test_inside_old_tolerance_window_but_outside_paint_is_not_evidence():
    geometry, paint = fixture()
    paint[:] = 0
    paint[100:391, 104:107] = 1
    assert audit_candidate(geometry, paint, ['low_mask_coverage']) is None


def test_slot_identity_and_model_disagreements_remain_manual():
    geometry, paint = fixture()
    for reason in ('source_slot_disjoint', 'repair_ambiguous_source_slot_identity',
                   'transverse_source_promoted_to_boundary', 'model_source_count_disagreement'):
        assert audit_candidate(geometry, paint, [reason]) is None
    geometry['source_converter']['warnings'] = ['ambiguous_inward_source_continuation']
    assert audit_candidate(geometry, paint, ['source_conversion_uncertain']) is None


def test_two_slot_predictions_on_the_same_source_run_do_not_pass():
    geometry, paint = fixture()
    second = deepcopy(geometry['lanes'][0])
    second['slot'] = 3
    geometry['lanes'].append(second)
    # Coincident predictions are an identity ambiguity even without crossing.
    assert not measure(geometry, paint)['eligible_for_fresh_audit']
