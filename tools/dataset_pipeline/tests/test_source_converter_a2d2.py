"""Source-only Audi reconstruction retains paint extent and rejects topology guesses."""
from copy import deepcopy

import cv2
import numpy as np
import pytest

from bcdata.geometry import GeoParams, Segment, lane_xs
from bcdata.source_converter_a2d2 import VERSION, _outside_four, continuation_score, meaningful_reversals, repair_geometry


def line(top, bottom, slot=2, offset=0., slope=-.3, curve=0.):
    ys = np.arange(top, bottom + 1)
    xs = 300 + offset + slope * (ys - 400) + curve * np.sin((ys - top) / max(1, bottom - top) * np.pi)
    rows = sorted(set(ys[::5].tolist() + [bottom]))
    return {"fit": {"basis": "interp", "ys": ys.tolist(), "xs": xs.tolist()},
            "points": [[float(np.interp(y, ys, xs)), y, True] for y in rows],
            "y_top": top, "y_bottom": bottom, "slot": slot, "physical": True,
            "kind": "solid", "flags": [], "gaps": [], "segments": 1,
            "mean_confidence": 1., "x_ref": float(xs[-1])}


def geo(lanes):
    return {"width": 800, "height": 800, "horizon_y": 320, "valid_bottom": 750,
            "max_run_frac": .08, "tags": {}, "stats": {}, "lanes": lanes}


def paint(lanes, thickness=7):
    mask = np.zeros((800, 800), np.float32)
    for item in lanes:
        cv2.polylines(mask, [np.round(np.asarray(item["points"])[:, :2]).astype(np.int32)], False, 1., thickness)
    return mask


def test_consistent_source_geometry_and_input_objects_stay_unchanged():
    original = geo([line(380, 730, 2), line(380, 730, 3, offset=250, slope=.3)])
    mask = paint(original["lanes"])
    before, pixels = deepcopy(original), mask.copy()
    fixed = repair_geometry(original, mask)
    assert original == before
    np.testing.assert_array_equal(mask, pixels)
    assert fixed["lanes"] == original["lanes"]
    assert fixed["source_converter"]["version"] == VERSION
    assert not fixed["source_converter"]["changed"]


def test_curved_source_uses_measured_centres_instead_of_off_paint_polynomial():
    source = line(430, 730, 2, curve=55)
    wrong = line(430, 730, 2, curve=20)
    original = geo([wrong])
    mask = paint([source], thickness=13)
    fixed = repair_geometry(original, mask)
    assigned = [item for item in fixed["lanes"] if item.get("slot")]
    assert len(assigned) == 1
    assert fixed["source_converter"]["changed"]
    assert assigned[0]["fit"]["basis"] == "interp"
    for x, y, seen in assigned[0]["points"]:
        if seen:
            assert mask[y, max(0, round(x) - 1):round(x) + 2].any()
    assert assigned[0]["y_top"] >= source["y_top"] - 8
    assert assigned[0]["y_bottom"] <= source["y_bottom"] + 8


def test_parallel_neighbour_is_not_a_dash_continuation():
    ys1, ys2 = np.arange(400, 450), np.arange(510, 650)
    upper = Segment(ys1, 300 - .3 * (ys1 - 400), np.ones_like(ys1) * 3)
    lower = Segment(ys2, 350 - .3 * (ys2 - 400), np.ones_like(ys2) * 3)
    assert continuation_score(lower, upper, 800, 800) is None


def test_solid_and_dashed_semantics_cannot_be_joined_across_a_gap():
    ys1, ys2 = np.arange(400, 450), np.arange(510, 650)
    upper = Segment(ys1, 300 - .3 * (ys1 - 400), np.ones_like(ys1) * 3)
    lower = Segment(ys2, 300 - .3 * (ys2 - 400), np.ones_like(ys2) * 3)
    assert continuation_score(lower, upper, 800, 800, 0, 1) is None


def test_transverse_paint_cannot_create_a_missing_ego_boundary():
    original = geo([])
    mask = np.zeros((800, 800), np.float32)
    cv2.line(mask, (100, 550), (700, 560), 1., 12)
    fixed = repair_geometry(original, mask)
    assert not [item for item in fixed["lanes"] if item.get("slot")]


def test_empty_source_does_not_complete_an_existing_model_trace():
    original = geo([line(400, 700)])
    original["source_converter"] = {"warnings": ["source_taxonomy_uncertain"]}
    fixed = repair_geometry(original, np.zeros((800, 800), np.float32))
    assert fixed["lanes"] == original["lanes"]
    assert "source_taxonomy_uncertain" in fixed["source_converter"]["warnings"]


def test_hood_covered_paint_cannot_supply_a_new_boundary():
    original = geo([line(380, 730)])
    mask = paint(original["lanes"])
    hood = mask.copy()
    fixed = repair_geometry(original, mask, my_car=hood)
    assert fixed["lanes"] == original["lanes"]
    assert not fixed["source_converter"]["changed"]


@pytest.mark.parametrize("name", ["my_car", "movable", "solid_mask", "dashed_mask"])
def test_all_auxiliary_masks_require_matching_dimensions(name):
    with pytest.raises(ValueError, match="dimensions"):
        repair_geometry(geo([]), np.zeros((800, 800), np.float32), **{name: np.zeros((8, 8))})


def test_source_requires_finite_values_and_matching_dimensions():
    with pytest.raises(ValueError, match="dimensions"):
        repair_geometry(geo([]), np.zeros((8, 8), np.float32))
    with pytest.raises(ValueError, match="non-finite"):
        repair_geometry(geo([]), np.full((800, 800), np.nan, np.float32))


def test_pixel_jitter_does_not_become_a_physical_reversal():
    ys = np.arange(400)
    xs = 300 + .15 * ys + np.where(ys % 2, .7, -.7)
    assert meaningful_reversals(xs, ys, 1920, 1208) == 0
    # One real S-shaped turn plus a tiny endpoint wobble is one reversal.
    xs = np.r_[np.linspace(300, 220, 180), np.linspace(220, 420, 210), np.linspace(420, 416, 10)]
    assert meaningful_reversals(xs, ys, 1920, 1208) == 1


def test_large_repeated_zigzags_still_trigger_a_shape_failure():
    ys = np.arange(400)
    xs = 300 + 40 * np.sin(ys / 399 * 6 * np.pi)
    assert meaningful_reversals(xs, ys, 1920, 1208) >= 4


def test_unrelated_prior_warning_is_retained_after_a_real_shape_repair():
    source = line(430, 730, 2, curve=55)
    original = geo([line(430, 730, 2, curve=20)])
    original['source_converter'] = {'warnings': ['source_taxonomy_uncertain']}
    fixed = repair_geometry(original, paint([source], thickness=13))
    assert fixed['source_converter']['changed']
    assert 'source_taxonomy_uncertain' in fixed['source_converter']['warnings']
    assert 'source_taxonomy_uncertain' not in fixed['source_converter']['resolved_warnings']


def test_unknown_correction_masks_cannot_manufacture_extra_source_paint():
    original = geo([line(400, 700)])
    empty = np.zeros((800, 800), np.float32)
    fixed = repair_geometry(original, empty, solid_mask=np.ones_like(empty), dashed_mask=np.ones_like(empty))
    assert fixed['lanes'] == original['lanes']


def test_unique_dash_join_keeps_one_identity_and_unpainted_gap_visibility():
    upper = line(400, 450, 3, offset=230, slope=.3)
    lower = line(520, 730, 4, offset=230, slope=.3)
    original = geo([upper, lower])
    mask = paint([upper, lower], thickness=3)
    fixed = repair_geometry(original, mask)
    assigned = [item for item in fixed['lanes'] if item.get('slot')]
    assert fixed['source_converter']['changed']
    assert len(assigned) == 1 and assigned[0]['slot'] == 3
    assert assigned[0]['y_top'] >= 398 and assigned[0]['y_bottom'] <= 732
    for point in assigned[0]['points']:
        if 455 < point[1] < 515:
            assert point[2] is False


def test_outside_four_requires_both_source_observed_neighbours():
    def candidate(x, slot=None, bottom=700):
        item = line(400, bottom, slot, offset=x-300, slope=0.)
        item['source_observed_rows'] = list(range(400, bottom+1))
        return item
    outer, inner, extra = candidate(220, 1), candidate(350, 2), candidate(100)
    assert _outside_four(extra, [outer, inner, extra], 800)
    assert extra['beyond_four_lanes_evidence']['ordering_from_actual_paint']
    unknown = candidate(100)
    assert not _outside_four(unknown, [outer, unknown], 800)
    # Unsupported vertical extent prevents declaring the complete source
    # component outside the training policy from one small overlapping band.
    early = candidate(100, bottom=750)
    short_outer, short_inner = candidate(220, 1, bottom=500), candidate(350, 2, bottom=500)
    assert not _outside_four(early, [early, short_outer, short_inner], 800)
    inside = candidate(280)
    assert not _outside_four(inside, [inside, outer, inner], 800)


def test_fixing_bowed_left_preserves_existing_fragmented_right_boundary():
    from bcdata.source_converter import merge_chains
    actual_left = line(430, 730, 2, curve=55)
    wrong_left = line(430, 730, 2, curve=20)
    upper = line(450, 495, 3, offset=230, slope=.3)
    lower = line(655, 730, 3, offset=230, slope=.3)
    old_right = merge_chains(upper, lower)
    old_right['slot'] = 3
    original = geo([wrong_left, old_right])
    fixed = repair_geometry(original, paint([actual_left, upper, lower], thickness=13))
    assert fixed['source_converter']['changed']
    assert 3 in fixed['source_converter']['preserved_existing_slots']
    right = next(item for item in fixed['lanes'] if item.get('slot') == 3)
    assert right['points'] == old_right['points']
    assert right['fit'] == old_right['fit']
    assert (right['y_top'], right['y_bottom']) == (450, 730)
    left = next(item for item in fixed['lanes'] if item.get('slot') == 2)
    assert left['fit']['basis'] == 'interp'
    assert left['points'] != wrong_left['points']


def test_recentering_broad_paint_cannot_move_a_well_supported_existing_trajectory():
    from bcdata.source_converter_a2d2 import _from_chain, _preserve_supported_boundaries
    from bcdata.geometry import Segment
    old = line(430,730,2,offset=-14,slope=0)
    geometry = geo([old])
    ys = np.arange(430,731)
    xs = np.full(len(ys), float(old['points'][0][0])+14)
    candidate = _from_chain([Segment(ys,xs,np.full(len(ys),41))])
    candidate.update(slot=2,x_ref=float(xs[0]))
    mask = paint([old],thickness=41)
    okay,preserved = _preserve_supported_boundaries([candidate],{0:[old]},geometry,mask)
    assert okay and preserved == [2]
    assert candidate['fit'] == old['fit'] and candidate['points'] == old['points']
