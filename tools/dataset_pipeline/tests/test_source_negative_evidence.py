from copy import deepcopy

from bcdata.source_negative_evidence import measure
from tests.test_source_run_evidence import fixture


def test_only_genuinely_empty_or_horizontal_source_paint_supports_a_negative():
    geometry, paint = fixture()
    geometry['lanes'] = []
    paint[:] = 0
    assert measure(geometry, paint)['family'] == 'empty-source'
    paint[250:256, 20:380] = 1
    assert measure(geometry, paint)['family'] == 'transverse-only'
    paint[300:310, 100:103] = 1
    assert measure(geometry, paint) is None  # Even a tiny upright dash matters.


def test_negative_evidence_cannot_delete_a_current_boundary():
    geometry, paint = fixture()
    before = deepcopy(geometry)
    paint[:] = 0
    assert measure(geometry, paint) is None
    assert geometry == before
