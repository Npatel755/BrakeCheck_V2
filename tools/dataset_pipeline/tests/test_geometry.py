import numpy as np
import pytest

from bcdata.geometry import HOOD_CROP_POLICY, GeoParams, _valid_bottom, extract_lanes, lane_xs, make_crop, sample_rows
from tests.synth import H, W, lane_prob_from_classes, scene


def run(cls, **kw):
    p = GeoParams(horizon_y=0.42)
    my_car = (cls == 5).astype(np.float32)
    return extract_lanes(lane_prob_from_classes(cls), p, my_car=my_car, **kw)


def slotted(geo):
    return {l["slot"]: l for l in geo["lanes"] if l["slot"]}


def max_err(lane, truth_ys, truth_xs):
    sel = (truth_ys >= lane["y_top"]) & (truth_ys <= lane["y_bottom"])
    pred = lane_xs(lane, truth_ys[sel])
    return float(np.abs(pred - truth_xs[sel]).max())


def test_four_lanes_ordered_with_dashes_grouped():
    rgb, cls, truth = scene()
    geo = run(cls)
    s = slotted(geo)
    assert set(s) == {1, 2, 3, 4}, [ (l["slot"], l["x_ref"], l["flags"]) for l in geo["lanes"]]
    assert s[2]["kind"] == "dashed" and s[3]["kind"] == "dashed"
    assert s[1]["kind"] == "solid" and s[4]["kind"] == "solid"
    for slot, (ys, xs) in truth.items():
        assert max_err(s[slot], ys, xs) < 6.0
    # order left to right
    xr = [s[k]["x_ref"] for k in (1, 2, 3, 4)]
    assert xr == sorted(xr)


def test_dashed_lane_spans_most_of_the_road():
    _, cls, truth = scene()
    s = slotted(run(cls))
    ys, _ = truth[2]
    assert s[2]["y_bottom"] - s[2]["y_top"] > 0.6 * (ys.max() - ys.min())
    assert s[2]["segments"] >= 3


def test_stop_line_and_arrow_are_not_lanes():
    _, cls, truth = scene(stop_line=True, arrow=True)
    geo = run(cls)
    s = slotted(geo)
    assert set(s) == {1, 2, 3, 4}
    assert geo["tags"].get("crosswalk_or_stop_line")
    assert s[1]["kind"] == "solid" and s[4]["kind"] == "solid"     # the stop line crossing them is not a dash gap
    for slot, (ys, xs) in truth.items():
        assert max_err(s[slot], ys, xs) < 8.0


@pytest.mark.parametrize("curve", [25.0, -25.0, 45.0])
def test_curved_road(curve):
    _, cls, truth = scene(curve=curve)
    geo = run(cls)
    s = slotted(geo)
    assert set(s) == {1, 2, 3, 4}
    for slot, (ys, xs) in truth.items():
        assert max_err(s[slot], ys, xs) < 8.0
    assert geo["tags"].get("curve")


def test_hood_is_excluded():
    _, cls, _ = scene(hood=True)
    geo = run(cls)
    assert geo["valid_bottom"] < 725
    assert all(l["y_bottom"] <= geo["valid_bottom"] for l in geo["lanes"])


def test_detached_sky_ego_island_cannot_crop_visible_highway_paint():
    _, cls, _ = scene(hood=False)
    my_car = np.zeros((H, W), np.float32)
    my_car[:180, :] = .95
    geometry = extract_lanes(lane_prob_from_classes(cls), GeoParams(), my_car=my_car)
    # The previous first-wide-row cutoff collapsed this to horizon + 10 and
    # removed all the actual road paint, despite an otherwise unchanged mask.
    assert geometry["valid_bottom"] == H - 1
    assert set(slotted(geometry)) == {1, 2, 3, 4}
    assert min(lane["y_bottom"] for lane in slotted(geometry).values()) > H * .8
    assert geometry["hood_crop_policy"] == HOOD_CROP_POLICY


def test_detached_wide_band_below_horizon_does_not_remove_lower_paint():
    _, cls, _ = scene(hood=False)
    my_car = np.zeros((H, W), np.float32)
    my_car[500:520, :] = .9
    geometry = extract_lanes(lane_prob_from_classes(cls), GeoParams(), my_car=my_car)
    assert geometry["valid_bottom"] == H - 1
    assert any(lane["y_bottom"] > H * .9 for lane in geometry["lanes"])


def test_detached_island_above_a_real_bottom_hood_does_not_move_the_cutoff():
    hood = np.zeros((200, 320), np.float32)
    hood[10:50, :] = .9
    hood[180:, :] = .9
    assert _valid_bottom(hood.shape, hood, GeoParams()) == 178


def test_narrow_bridge_does_not_join_a_floating_broad_hood_band_to_the_bottom():
    hood = np.zeros((200, 320), np.float32)
    hood[110:140, :] = .9
    hood[140:, :30] = .9
    assert _valid_bottom(hood.shape, hood, GeoParams()) == 199


@pytest.mark.parametrize("hood_top, expected", [(None, 149), (180, 149), (130, 128)])
def test_bottom_ignore_cap_still_applies_with_or_without_real_hood(hood_top, expected):
    hood = np.zeros((200, 320), np.float32)
    hood[10:30, :] = .9
    if hood_top is not None:
        hood[hood_top:, :] = .9
    assert _valid_bottom(hood.shape, hood, GeoParams(bottom_ignore=.25)) == expected


def test_thirty_percent_boundary_and_low_probability_ego_do_not_crop_globally():
    hood = np.zeros((200, 100), np.float32)
    hood[160:, :30] = .9
    assert _valid_bottom(hood.shape, hood, GeoParams()) == 199
    hood[160:, :] = .5
    assert _valid_bottom(hood.shape, hood, GeoParams()) == 199
    hood[160:, :31] = .9
    assert _valid_bottom(hood.shape, hood, GeoParams()) == 158


def test_double_line_is_one_lane():
    _, cls, _ = scene(lanes=(("solid", 230), ("solid", 560), ("solid", 940)), double_at=560)
    geo = run(cls)
    doubles = [l for l in geo["lanes"] if "double_line" in l["flags"]]
    assert len(doubles) == 1
    assert len([l for l in geo["lanes"] if l["slot"]]) == 3


def test_empty_mask_gives_no_lanes():
    geo = extract_lanes(np.zeros((H, W), np.float32), GeoParams())
    assert geo["lanes"] == [] and geo["tags"]["lane_count"] == 0


def test_fixed_row_sampling_stays_inside_observed_extent():
    _, cls, _ = scene()
    s = slotted(run(cls))
    crop = make_crop({"x0": 0, "y0": 300, "x1": 1164, "y1": 719}, W, H, 1640, 590)
    rows = list(range(580, 249, -10))
    for lane in s.values():
        pts = sample_rows(lane, crop, rows)
        assert len(pts) >= 2
        for x, y in pts:
            yo = crop.to_source_y(y)
            assert lane["y_top"] - 1 <= yo <= lane["y_bottom"] + 1
            assert 0 <= x < 1640
