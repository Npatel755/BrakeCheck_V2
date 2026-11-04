"""Future optional selection holds never rewrite or join observed paint."""
import copy
import math

import pytest

from bcdata.proposal_training_policy import VERSION, training_geometry_hold


def line(slot, y0, y1, *, intercept=.2, slope=.3, samples=15):
    return {"slot": slot, "physical": True, "points": [
        [(intercept + slope * (y0 + (y1 - y0) * index / (samples - 1))) * 1000,
         (y0 + (y1 - y0) * index / (samples - 1)) * 1000, True]
        for index in range(samples)]}


def check(lanes):
    return training_geometry_hold({"width": 1000, "height": 1000, "lanes": lanes}, 1000, 1000)


@pytest.mark.parametrize("slots", [(1, 2), (3, 4), (2, 1), (4, 3)])
def test_aligned_fragments_assigned_adjacent_same_side_slots_are_held(slots):
    lanes = [line(slots[0], .3, .5), line(slots[1], .6, .9)]
    original = copy.deepcopy(lanes)
    policy = check(lanes)
    assert not policy["eligible_for_training"]
    assert policy["reasons"] == ["probable_same_boundary_multiple_slots"]
    assert policy["fragment_pairs"][0]["slots"] == list(slots)
    assert policy["version"] == VERSION
    assert lanes == original


def test_tiny_observed_overlap_still_holds_but_broad_overlap_does_not():
    assert not check([line(1, .3, .6), line(2, .595, .9)])["eligible_for_training"]
    assert check([line(1, .3, .7), line(2, .6, .9)])["eligible_for_training"]


def test_parallel_distinct_lanes_remain_eligible_even_when_spans_do_not_overlap():
    assert check([line(1, .3, .5), line(2, .6, .9, intercept=.35)])["eligible_for_training"]
    assert check([line(1, .3, .9), line(2, .3, .9, intercept=.35)])["eligible_for_training"]


def test_unequal_endpoint_slopes_cannot_establish_same_boundary():
    assert check([line(1, .3, .5), line(2, .6, .9, intercept=.08, slope=.5 + 1e-3)])["eligible_for_training"]


def test_long_gaps_opposite_sides_and_short_fragments_do_not_trigger_hold():
    assert check([line(1, .1, .3), line(2, .6, .9)])["eligible_for_training"]
    assert check([line(2, .3, .5), line(3, .6, .9)])["eligible_for_training"]
    assert check([line(1, .3, .35), line(2, .4, .9)])["eligible_for_training"]


def test_reciprocal_projection_prevents_single_direction_false_match():
    # Forward error 0; reverse error .02, although tangent difference is only .1.
    assert check([line(1, .2, .4), line(2, .6, .9, intercept=.14, slope=.4)])["eligible_for_training"]


def test_only_observed_points_establish_extent_and_tangent():
    upper = line(1, .3, .5)
    lower = line(2, .6, .9)
    # Geometric fit/metadata claims are irrelevant to the observed point support.
    upper["y_top"], upper["y_bottom"] = 1, 999
    upper["fit"] = {"coefficients": [99, -99]}
    assert not check([upper, lower])["eligible_for_training"]
    for point in upper["points"]:
        point[2] = False
    assert check([upper, lower])["reasons"] == ["no_observed_painted_lines"]


def test_scale_invariance_and_unordered_points():
    lanes = [line(1, .3, .5), line(2, .6, .9)]
    baseline = check(lanes)
    resized = copy.deepcopy(lanes)
    for lane in resized:
        lane["points"].reverse()
        for point in lane["points"]:
            point[0] *= 1.92
            point[1] *= 1.208
    policy = training_geometry_hold({"width": 1920, "height": 1208, "lanes": resized}, 1920, 1208)
    assert policy == baseline


@pytest.mark.parametrize("geometry,width,height", [
    (None, 1000, 1000), ({}, 1000, 1000), ({"lanes": None}, 1000, 1000),
    ({"lanes": [None]}, 1000, 1000), ({"lanes": [line(1, .3, .5)]}, True, 1000),
    ({"lanes": [line(1, .3, .5)]}, math.nan, 1000),
    ({"lanes": [line(1, .3, .5)]}, 1000, 10 ** 500),
    ({"lanes": [line(1, .3, .5)], "width": 999}, 1000, 1000),
])
def test_malformed_geometry_fails_closed(geometry, width, height):
    result = training_geometry_hold(geometry, width, height)
    assert not result["eligible_for_training"]
    assert result["reasons"] == ["invalid_painted_geometry"]
    assert result["fragment_pairs"] == []


@pytest.mark.parametrize("change", [
    {"slot": True}, {"slot": 5}, {"slot": None}, {"physical": False},
    {"kind": "road_edge"}, {"kind": []}, {"points": []},
    {"points": [[1, 1], [2, 1]]}, {"points": [[1, 1], [-1, 2]]},
    {"points": [[1, 1], [1000, 2]]}, {"points": [[1, 1], [2, math.inf]]},
    {"points": [[1, 1], [2, math.nan]]}, {"points": [[1, 1], [True, 2]]},
    {"points": [[1, 1, "false"], [2, 2, True]]},
])
def test_invalid_lane_slots_points_and_road_edges_fail_closed(change):
    lane = line(1, .3, .5)
    lane.update(change)
    assert check([lane])["reasons"] == ["invalid_painted_geometry"]


def test_empty_and_duplicate_slots_are_not_training_labels():
    assert check([])["reasons"] == ["no_observed_painted_lines"]
    assert check([line(1, .3, .5), line(1, .6, .9)])["reasons"] == ["invalid_painted_geometry"]
