"""Source-only association repairs preserve visible paint and audit uncertainty."""
from copy import deepcopy

import cv2
import numpy as np
import pytest

from bcdata.geometry import GeoParams, extract_lanes, lane_xs
from bcdata.source_converter_v4 import continuation_score, repair_geometry_v4
from tests.synth import lane_prob_from_classes, scene


def chain(top, bottom, slope=.5, offset=0., slot=None):
    ys = list(range(top, bottom+1, 5))
    if ys[-1] != bottom:
        ys.append(bottom)
    xs = [400+slope*(y-320)+offset for y in ys]
    return {"fit": {"basis": "interp", "ys": ys, "xs": xs},
            "points": [[x, y, True] for x,y in zip(xs, ys)],
            "y_top": top, "y_bottom": bottom, "physical": True,
            "slot": slot, "flags": [] if slot else ["fragment"],
            "kind": "solid", "gaps": [], "segments": 1,
            "x_ref": 400+slope*(700-320)+offset, "mean_confidence": 1.}


def geometry(lanes):
    return {"width": 800, "height": 800, "horizon_y": 320,
            "valid_bottom": 700, "tags": {}, "stats": {}, "lanes": lanes}


def paint_for(lanes):
    paint = np.zeros((800,800), np.float32)
    for lane in lanes:
        cv2.polylines(paint, [np.round(np.asarray(lane["points"])[:,:2]).astype(np.int32)], False, 1., 3)
    return paint


def test_supported_inward_dash_gets_ego_slot_without_imagined_extension():
    outer = chain(420, 550, slope=-1.5, slot=2)
    central = chain(450, 485, slope=-.2)
    right = chain(400, 650, slope=.5, slot=3)
    original = geometry([outer, right, central])
    before = deepcopy(original)
    fixed = repair_geometry_v4(original, paint_for(original["lanes"]))
    assert original == before
    slots = {lane["slot"]:lane for lane in fixed["lanes"] if lane["slot"]}
    assert set(slots) == {1,2,3}
    assert slots[1]["points"] == outer["points"]
    assert slots[2]["points"] == central["points"]
    assert (slots[2]["y_top"], slots[2]["y_bottom"]) == (450,485)
    assert fixed["source_converter"]["risk_family"] == "fragment_recovery"
    assert fixed["source_converter"]["requires_fresh_human_audit"]


def test_near_mask_break_joins_same_boundary_despite_quantized_secant():
    upper = chain(400, 500, slope=-1., offset=20., slot=1)
    lower = chain(507, 650, slope=-1., offset=24., slot=2)
    original = geometry([upper,lower])
    fixed = repair_geometry_v4(original, paint_for(original["lanes"]))
    assigned = [lane for lane in fixed["lanes"] if lane["slot"]]
    assert len(assigned) == 1
    assert assigned[0]["slot"] == 2
    assert (assigned[0]["y_top"], assigned[0]["y_bottom"]) == (400,650)
    assert assigned[0]["gaps"] == [[500,507]]
    assert fixed["source_converter"]["joined_chains"] == 1


def test_two_slotted_dash_fragments_become_one_physical_boundary():
    a,b = chain(400,460,slot=3), chain(520,650,slot=4)
    fixed = repair_geometry_v4(geometry([a,b]), paint_for([a,b]))
    lane, = [lane for lane in fixed["lanes"] if lane["slot"]]
    assert lane["slot"] == 3
    assert lane["y_top"] == 400 and lane["y_bottom"] == 650
    for piece in (a,b):
        ys = [point[1] for point in piece["points"]]
        np.testing.assert_allclose(lane_xs(lane,ys), lane_xs(piece,ys))


def test_parallel_neighbor_cannot_become_dash_continuation():
    a,b = chain(400,450,slot=3), chain(520,650,offset=45,slot=4)
    original = geometry([a,b])
    paint = paint_for([a,b])
    assert continuation_score(a,b,paint,original) is None
    fixed = repair_geometry_v4(original,paint)
    assert fixed["lanes"] == original["lanes"]
    assert not fixed["source_converter"]["joined_chains"]


def test_no_source_paint_cannot_recover_or_merge_anything():
    original = geometry([chain(400,440), chain(470,510)])
    fixed = repair_geometry_v4(original,np.zeros((800,800),np.float32))
    assert fixed["lanes"] == original["lanes"]
    assert not fixed["source_converter"]["joined_chains"]
    assert not fixed["source_converter"]["recovered_short_chains"]


def test_isolated_paint_on_empty_side_is_not_automatically_a_new_lane():
    old = chain(400,650,slope=-.6,slot=2)
    isolated = chain(450,485,slope=.8)
    original = geometry([old,isolated])
    fixed = repair_geometry_v4(original,paint_for(original["lanes"]))
    assert fixed["lanes"] == original["lanes"]
    assert not fixed["source_converter"]["recovered_short_chains"]


def test_accepted_outer_extent_is_not_changed_by_an_unassigned_fragment():
    outer = chain(500,570,slope=-1.,slot=1)
    fragment = chain(430,470,slope=-1.)
    original = geometry([outer,fragment])
    fixed = repair_geometry_v4(original,paint_for(original["lanes"]))
    assert fixed["lanes"] == original["lanes"]
    assert not fixed["source_converter"]["joined_chains"]


def test_curve_fragment_without_observed_overlap_cannot_create_second_boundary():
    # A short distant centre-line piece can change direction around a bend.
    # Its extrapolated x_ref falls inward, but the two pieces never coexist.
    existing = chain(500,650,slope=-.8,slot=2)
    distant = chain(450,475,slope=.2,offset=-100.)
    original = geometry([existing,distant])
    fixed = repair_geometry_v4(original,paint_for(original["lanes"]))
    assert fixed["lanes"] == original["lanes"]
    assert not fixed["source_converter"]["recovered_short_chains"]
    assert "ambiguous_inward_source_continuation" in fixed["source_converter"]["warnings"]


def test_nonoverlapping_intersection_fragments_keep_identity_uncertainty():
    existing = chain(550,650,slope=-.6,slot=2)
    across_junction = chain(400,440,slope=-.2)
    original = geometry([existing,across_junction])
    fixed = repair_geometry_v4(original,paint_for(original["lanes"]))
    assert fixed["lanes"] == original["lanes"]
    assert "ambiguous_inward_source_continuation" in fixed["source_converter"]["warnings"]


def test_tiny_speck_and_transverse_paint_cannot_steal_ego_slot():
    old = chain(400,650,slope=-.6,slot=2)
    speck = chain(450,453,slope=-.2)
    transverse = chain(450,480,slope=8,offset=-1100)
    original = geometry([old,speck,transverse])
    fixed = repair_geometry_v4(original,paint_for(original["lanes"]))
    assert [lane["slot"] for lane in fixed["lanes"] if lane["slot"]] == [2]
    assert not fixed["source_converter"]["recovered_short_chains"]


def test_crossing_identity_is_flagged_without_inventing_a_repair():
    a = chain(400,650,slope=.8,offset=-50,slot=2)
    b = chain(400,650,slope=-.8,offset=50,slot=3)
    original = geometry([a,b])
    fixed = repair_geometry_v4(original,paint_for([a,b]))
    assert fixed["lanes"] == original["lanes"]
    assert "crossing_source_boundaries" in fixed["source_converter"]["warnings"]
    assert fixed["source_converter"]["risk_family"] == "crossing_uncertain"


def test_hood_pixels_cannot_support_new_inward_fragment():
    old = chain(400,650,slope=-.8,slot=2)
    fragment = chain(480,520,slope=-.2)
    original = geometry([old,fragment])
    paint = paint_for(original["lanes"])
    hood = np.zeros_like(paint)
    hood[470:530,340:390] = 1.
    fixed = repair_geometry_v4(original,paint,my_car=hood)
    assert not fixed["source_converter"]["recovered_short_chains"]
    assert fixed["lanes"] == original["lanes"]


def test_unresolved_prior_warning_is_preserved():
    original = geometry([chain(400,650,slot=3)])
    original["source_converter"] = {"warnings":["source_taxonomy_uncertain"]}
    fixed = repair_geometry_v4(original,paint_for(original["lanes"]))
    assert fixed["lanes"] == original["lanes"]
    assert "source_taxonomy_uncertain" in fixed["source_converter"]["warnings"]


def test_one_recovery_does_not_erase_unbound_prior_fragment_warning():
    original = geometry([chain(420,550,slope=-1.5,slot=2),chain(450,485,slope=-.2)])
    original["source_converter"] = {"warnings":["unassigned_source_fragment_requires_review"]}
    fixed = repair_geometry_v4(original,paint_for(original["lanes"]))
    assert fixed["source_converter"]["recovered_short_chains"] == 1
    assert "unassigned_source_fragment_requires_review" in fixed["source_converter"]["warnings"]


def test_source_dimensions_and_finite_values_are_required():
    original = geometry([])
    with pytest.raises(ValueError,match="dimensions"):
        repair_geometry_v4(original,np.zeros((8,8),np.float32))
    with pytest.raises(ValueError,match="dimensions"):
        repair_geometry_v4(original,np.zeros((800,800),np.float32),movable=np.zeros((8,8)))
    with pytest.raises(ValueError,match="non-finite"):
        repair_geometry_v4(original,np.full((800,800),np.nan,np.float32))


def test_stop_line_and_arrow_do_not_gain_lane_slots():
    _,classes,_ = scene(stop_line=True,arrow=True)
    paint = lane_prob_from_classes(classes)
    hood = (classes == 5).astype(np.float32)
    original = extract_lanes(paint,GeoParams(horizon_y=.42),my_car=hood)
    fixed = repair_geometry_v4(original,paint,my_car=hood)
    assert {lane["slot"] for lane in fixed["lanes"] if lane["slot"]} == {1,2,3,4}
    assert fixed["tags"]["crosswalk_or_stop_line"]
