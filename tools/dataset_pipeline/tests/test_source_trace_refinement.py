from copy import deepcopy

import numpy as np

from bcdata.geometry import lane_xs
from bcdata.source_trace_refinement import refine, retains_slot_traces, retains_physical_traces
from bcdata.source_target_evidence import measure
from tests.test_source_run_evidence import fixture


def test_native_trace_extends_a_unique_source_stroke_and_retains_identity():
    geometry, paint = fixture()
    geometry['lanes'][0]['y_top'] = 130
    geometry['lanes'][0]['points'] = [p for p in geometry['lanes'][0]['points'] if p[1] >= 130]
    before = deepcopy(geometry)
    result, proof = refine(geometry, paint)
    assert proof and result['lanes'][0]['y_top'] == 100
    assert retains_slot_traces(geometry, result)
    assert geometry == before
    assert measure(result, paint)['eligible_for_fresh_audit']


def test_refinement_cannot_make_a_missing_parallel_boundary_disappear():
    geometry, paint = fixture()
    paint[150:350, 195:206] = 1
    result, _ = refine(geometry, paint)
    assert not measure(result, paint)['eligible_for_fresh_audit']
    assert [line['slot'] for line in result['lanes']] == [2]


def test_slot_changes_and_truncations_fail_retention():
    original, _ = fixture()
    for field, value in [('slot', 1), ('y_top', 110), ('y_bottom', 380)]:
        changed = deepcopy(original)
        changed['lanes'][0][field] = value
        assert not retains_slot_traces(original, changed)


def test_double_marking_requires_two_real_symmetric_stripes():
    geometry, paint = fixture()
    geometry['lanes'][0].update(kind='double', double_half_sep=6., flags=['double_line'])
    paint[:] = 0
    paint[100:391, 95:99] = 1
    paint[100:391, 102:106] = 1
    proof = measure(geometry, paint)
    assert proof['eligible_for_fresh_audit']
    assert proof['double_stripe_runs_represented'] > 0
    paint[100:391, 102:106] = 0
    assert not measure(geometry, paint)['eligible_for_fresh_audit']


def test_a_remote_parallel_lane_cannot_be_explained_as_a_double_stripe():
    geometry, paint = fixture()
    geometry['lanes'][0].update(kind='double', double_half_sep=40., flags=['double_line'])
    paint[:] = 0
    paint[100:391, 60:65] = 1
    paint[100:391, 136:141] = 1
    assert not measure(geometry, paint)['eligible_for_fresh_audit']


def test_beyond_four_needs_two_actual_neighbours_at_every_source_row():
    geometry, paint = fixture()
    line = deepcopy(geometry['lanes'][0])
    line.update(id=1, slot=1, x_ref=50,
                fit={'basis': 'interp', 'ys': [100, 390], 'xs': [50, 50]},
                points=[[50, y, True] for y in range(390, 99, -5)])
    geometry['lanes'].append(line)
    paint[100:391, 47:54] = 1
    paint[200:350, 17:24] = 1
    assert measure(geometry, paint)['verified_beyond_four_components']
    assert measure(geometry, paint)['eligible_for_fresh_audit']
    # Extrapolating an outer trace beyond its visible extent cannot excuse it.
    geometry['lanes'][1]['y_top'] = 250
    geometry['lanes'][1]['points'] = [p for p in geometry['lanes'][1]['points'] if p[1] >= 250]
    assert not measure(geometry, paint)['eligible_for_fresh_audit']


def test_wrong_side_source_trace_does_not_recentre_to_a_remote_stripe():
    geometry, paint = fixture()
    paint[:] = 0
    paint[100:391, 130:140] = 1
    result, proof = refine(geometry, paint)
    assert proof is None
    assert np.all(lane_xs(result['lanes'][0], np.arange(100, 391)) == 100)


def test_physical_retention_allows_a_duplicate_slot_merge_but_no_lost_paint():
    geometry, paint = fixture()
    upper = deepcopy(geometry['lanes'][0])
    upper.update(slot=1, y_top=100, y_bottom=180)
    geometry['lanes'][0]['y_top'] = 220
    geometry['lanes'].append(upper)
    merged, _ = fixture()
    assert not retains_slot_traces(geometry, merged)
    assert retains_physical_traces(geometry, merged, paint)
    merged['lanes'][0]['y_top'] = 140
    assert not retains_physical_traces(geometry, merged, paint)


def test_physical_retention_matches_native_scalar_reference():
    geometry, paint = fixture()
    candidate = deepcopy(geometry)
    candidate['lanes'][0]['fit']['xs'] = [106, 106]
    assert retains_physical_traces(geometry, candidate, paint)
    candidate['lanes'][0]['fit']['xs'] = [109, 109]
    assert not retains_physical_traces(geometry, candidate, paint)
    # An unsupported excursion does not oblige a repair to invent paint there.
    paint[:] = 0
    assert retains_physical_traces(geometry, candidate, paint)
