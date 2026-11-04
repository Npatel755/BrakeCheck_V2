from copy import deepcopy
import json

import numpy as np

from bcdata.geometry import lane_xs
from bcdata.source_fragment_continuation import repair_geometry
from bcdata.recheck_flagged_conversions import load_paint, unchanged_spans


def line(slot, x, top, bottom, identifier=0):
    return {'id': identifier, 'slot': slot, 'x_ref': x,
            'y_top': top, 'y_bottom': bottom, 'kind': 'dashed',
            'fit': {'basis': 'interp', 'ys': [top, bottom], 'xs': [x, x]},
            'points': [[x, y, True] for y in range(bottom, top - 1, -5)],
            'flags': [] if slot else ['fragment']}


def fixture():
    geometry = {'width': 400, 'height': 400, 'horizon_y': 160, 'valid_bottom': 399,
                'lanes': [line(1, 90, 300, 390), line(None, 90, 230, 275, 1)]}
    paint = np.zeros((400, 400), np.float32)
    paint[300:391, 88:93] = 1
    paint[230:276, 88:93] = 1
    return geometry, paint


def test_unique_outer_dash_extension_preserves_existing_curve_and_input():
    geometry, paint = fixture()
    before = deepcopy(geometry)
    candidate, evidence = repair_geometry(geometry, paint)
    assert evidence and geometry == before
    assert len(candidate['lanes']) == 1
    assert candidate['lanes'][0]['slot'] == 1
    assert candidate['lanes'][0]['y_top'] == 230
    assert unchanged_spans(geometry, candidate)
    assert np.array_equal(lane_xs(geometry['lanes'][0], np.arange(300, 391)),
                          lane_xs(candidate['lanes'][0], np.arange(300, 391)))


def test_competing_fragments_do_not_get_arbitrary_partners():
    geometry, paint = fixture()
    geometry['lanes'].append(line(None, 93, 230, 275, 2))
    paint[230:276, 91:96] = 1
    candidate, evidence = repair_geometry(geometry, paint)
    assert not evidence and candidate == geometry


def test_different_source_paint_types_are_not_joined():
    geometry, paint = fixture()
    solid, dashed = np.zeros_like(paint), np.zeros_like(paint)
    solid[300:391] = paint[300:391]
    dashed[230:276] = paint[230:276]
    assert not repair_geometry(geometry, paint, solid=solid, dashed=dashed)[1]


def test_unpainted_or_beyond_four_or_large_gap_fragment_stays_manual():
    geometry, paint = fixture()
    assert not repair_geometry(geometry, np.zeros_like(paint))[1]
    geometry['lanes'][1]['flags'].append('beyond_four_lanes')
    assert not repair_geometry(geometry, paint)[1]
    geometry['lanes'][1]['flags'].remove('beyond_four_lanes')
    geometry['lanes'][1] = line(None, 90, 180, 220, 1)
    paint[180:221, 88:93] = 1
    assert not repair_geometry(geometry, paint)[1]


def test_only_resolves_the_missing_fragment_warning():
    geometry, paint = fixture()
    geometry['source_converter'] = {'warnings': ['unassigned_source_fragment_requires_review',
                                                'ambiguous_source_slot_identity']}
    candidate, proof = repair_geometry(geometry, paint)
    assert proof
    assert candidate['source_converter']['warnings'] == ['ambiguous_source_slot_identity']


def test_rounding_a_visible_border_point_does_not_index_past_image():
    geometry, paint = fixture()
    for lane in geometry['lanes']:
        lane['fit']['xs'] = [399.8, 399.8]
        lane['points'] = [[399.8, point[1], True] for point in lane['points']]
    paint[:] = 0
    paint[230:276, 399] = 1
    paint[300:391, 399] = 1
    assert repair_geometry(geometry, paint, solid=paint, dashed=np.zeros_like(paint))[1]


def test_fast_lane_loader_matches_full_source_loader(tmp_path):
    from PIL import Image
    from bcdata.source_boundary_maps import load_boundary_maps
    from bcdata.sources.comma10k import CLASS_COLORS, LANE
    for fmt in ('a2d2_rgb', 'comma10k_rgb', 'class_map'):
        rng = np.random.default_rng(19)
        rgb = rng.integers(0, 256, (30, 40, 3), dtype=np.uint8)
        rgb[0] = (255, 193, 37)
        rgb[1] = (128, 0, 255)
        rgb[2] = CLASS_COLORS[LANE]
        path = tmp_path / (fmt + '.png')
        Image.fromarray(rgb[:, :, 0] % 6 if fmt == 'class_map' else rgb).save(path)
        params = {'format': fmt}
        if fmt == 'a2d2_rgb':
            palette = tmp_path / 'palette.json'
            palette.write_text(json.dumps({'#ffc125': 'Solid line', '#8000ff': 'Dashed line'}))
            params['palette_path'] = str(palette)
        annotation = {'path': str(path), 'params': json.dumps(params)}
        assert np.array_equal(load_paint(annotation)[0], load_boundary_maps(annotation)['lane'])
