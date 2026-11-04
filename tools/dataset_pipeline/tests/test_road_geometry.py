import numpy as np
import pytest

from bcdata.road_geometry import road_edges_from_probability


def road():
    mask = np.zeros((200, 300), np.float32)
    for y in range(80, 192):
        half = int(20 + (y - 80) * .55)
        mask[y, 150-half:150+half] = .9
    return mask


def test_empty_road_is_unknown_not_absent():
    edges = road_edges_from_probability(np.zeros((200, 300)))
    assert all(e['status'] == 'unknown' and not e['points'] for e in edges.values())


def test_independent_road_contours_follow_widening_surface():
    edges = road_edges_from_probability(road())
    assert edges['left']['status'] == edges['right']['status'] == 'visible'
    assert edges['left']['points'][0][0] < edges['left']['points'][-1][0]
    assert edges['right']['points'][0][0] > edges['right']['points'][-1][0]


def test_frame_border_is_not_a_road_edge():
    mask = road()
    mask[80:192, :151] = .9
    edges = road_edges_from_probability(mask)
    assert edges['left']['status'] == 'unknown'
    assert edges['right']['status'] == 'visible'


def test_vehicle_occlusion_does_not_become_a_continuous_edge():
    mask = road()
    blocked = np.zeros_like(mask)
    blocked[120:160, :150] = 1
    edges = road_edges_from_probability(mask, occlusion_mask=blocked)
    ys = [p[1] for p in edges['left']['points']]
    assert edges['right']['status'] == 'visible'
    assert not ys or min(ys) > 160 or max(ys) < 120


def test_curves_are_kept_and_small_disconnected_regions_not_chosen():
    mask = road()
    for y in range(80, 192):
        mask[y] = np.roll(mask[y], int(20 * ((y-140)/60)**2))
    mask[110:114, 10:16] = 1
    edges = road_edges_from_probability(mask)
    assert len(edges['left']['points']) > 2
    assert min(x for x, _ in edges['left']['points']) > 30


def test_exclusions_and_invalid_probability_are_checked():
    with pytest.raises(ValueError):
        road_edges_from_probability(np.full((10,10), np.nan))
    with pytest.raises(ValueError):
        road_edges_from_probability(road(), hood_mask=np.zeros((3,3)))
