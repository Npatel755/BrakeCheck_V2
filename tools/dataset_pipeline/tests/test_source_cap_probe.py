"""A cap counterfactual cannot silently gain new source-evidence exemptions."""
from copy import deepcopy

import numpy as np
import pytest

from bcdata.source_cap_probe import prototype
from bcdata.source_target_evidence import measure
from bcdata.source_trace_refinement import components, retains_physical_traces
from tests.test_source_tip_evidence import fixture


@pytest.mark.parametrize('trim', [-1, 5, True, 1.5])
def test_cap_probe_is_bounded_to_four_explicit_native_rows(trim):
    with pytest.raises(ValueError, match='zero to four'):
        prototype({}, np.zeros((1, 1)), [0, 0, 1, 1], trim)


def test_probe_does_not_mutate_native_pixels_or_original_geometry():
    geometry, paint, _ = fixture()
    original, native = deepcopy(geometry), paint.copy()
    bbox = components(geometry, paint)[0]['bbox']
    _, receipt = prototype(geometry, paint, bbox, 0, solid=np.zeros_like(paint), dashed=paint)
    assert geometry == original
    np.testing.assert_array_equal(paint, native)
    assert receipt['new_evidence_exemptions'] == 0 and receipt['source_pixels_changed'] is False


def test_probe_cannot_remove_cap_rows_already_supporting_an_original_lane():
    geometry, paint, _ = fixture()
    bbox = components(geometry, paint)[0]['bbox']
    with pytest.raises(ValueError, match='already supporting an original lane'):
        prototype(geometry, paint, bbox, 1, solid=np.zeros_like(paint), dashed=paint)


def test_probe_requires_native_dashed_paint_instead_of_relaxing_type_gate():
    geometry, paint, _ = fixture()
    bbox = components(geometry, paint)[0]['bbox']
    with pytest.raises(ValueError, match='confident native dashed'):
        prototype(geometry, paint, bbox, 0, solid=paint, dashed=np.zeros_like(paint))


def test_internal_oblique_cap_is_not_covered_by_existing_global_endpoint_allowance():
    height, width = 1208, 1920
    paint = np.zeros((height, width), np.float32)
    upper_ys = np.arange(849, 931)
    upper_xs = np.round(1146.5 + (upper_ys - 849) * (1189 - 1146.5) / 81, 1)
    for y, x in zip(upper_ys, upper_xs):
        paint[y, round(x) - 20:round(x) + 21] = 1
    lower_ys = np.arange(1139, 1208)
    lower_xs = np.r_[1383., 1370., 1358., 1347.5, 1348. + .7 * np.arange(65)]
    widths = np.r_[7, 33, 59, 82, np.linspace(83, 95, 65).astype(int)]
    for y, x, span in zip(lower_ys, lower_xs, widths):
        start = round(x - .5 * (span - 1))
        paint[y, start:start + span] = 1
    def geometry(ys, xs):
        lane = {'slot': 3, 'physical': True, 'fit': {'basis': 'interp', 'ys': ys.tolist(), 'xs': xs.tolist()},
                'y_top': int(ys.min()), 'y_bottom': int(ys.max()),
                'points': [[float(x), int(y), True] for x, y in zip(xs, ys)][::-1]}
        return {'width': width, 'height': height, 'horizon_y': 0, 'valid_bottom': height - 1,
                'lanes': [lane], 'source_converter': {'warnings': []}}
    original = geometry(upper_ys, upper_xs)
    candidate = geometry(np.r_[upper_ys, lower_ys[4:]], np.r_[upper_xs, lower_xs[4:]])
    evidence = measure(candidate, paint)
    assert retains_physical_traces(original, candidate, paint)
    assert evidence['bounded_endpoint_cap_rows'] == 0
    assert evidence['eligible_for_fresh_audit'] is False
    missing = next(item for item in evidence['unrepresented_components'] if item['bbox'][1] == 1139)
    assert missing['source_run_rows'] == 69
    assert missing['represented_fraction'] < .98
