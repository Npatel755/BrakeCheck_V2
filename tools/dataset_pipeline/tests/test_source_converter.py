"""Source conversion repairs use paint evidence, not just convenient geometry."""
from copy import deepcopy

import cv2
import numpy as np

from bcdata.geometry import GeoParams, extract_lanes, lane_xs
from bcdata.source_converter import join_score, repair_geometry
from tests.synth import lane_prob_from_classes, scene


def chain(top, bottom, slope=1.5, offset=0, slot=None):
    ys = list(range(top, bottom + 1, 5))
    if ys[-1] != bottom:
        ys.append(bottom)
    xs = [400 + slope * (y - 320) + offset for y in ys]
    return {"fit": {"basis": "interp", "ys": ys, "xs": xs},
            "points": [[x, y, True] for x, y in zip(xs, ys)],
            "y_top": top, "y_bottom": bottom, "physical": True,
            "slot": slot, "flags": [] if slot else ["fragment"],
            "kind": "solid", "gaps": [], "segments": 1,
            "x_ref": 400+slope*(700-320)+offset, "mean_confidence": 1.}


def geometry(lanes):
    return {"width": 800, "height": 800, "horizon_y": 320,
            "valid_bottom": 700, "tags": {}, "stats": {}, "lanes": lanes}


def paint_for(lanes):
    mask = np.zeros((800, 800), np.float32)
    for lane in lanes:
        pts = np.round(np.asarray(lane["points"])[:, :2]).astype(np.int32)
        cv2.polylines(mask, [pts], False, 1., 3)
    return mask


def test_unpainted_fragments_cannot_become_a_lane_by_joining():
    original = geometry([chain(400, 420), chain(450, 470)])
    repaired = repair_geometry(original, np.zeros((800, 800), np.float32))
    assert repaired["source_converter"]["joined_chains"] == 0
    assert not any(lane["slot"] for lane in repaired["lanes"])


def test_supported_dash_join_keeps_observed_points_and_extent():
    original = geometry([chain(400, 420), chain(450, 470)])
    before = deepcopy(original)
    repaired = repair_geometry(original, paint_for(original["lanes"]))
    assert original == before
    assert repaired["source_converter"]["joined_chains"] == 1
    slotted = [lane for lane in repaired["lanes"] if lane["slot"]]
    assert len(slotted) == 1
    lane = slotted[0]
    assert (lane["y_top"], lane["y_bottom"]) == (400, 470)
    assert lane["gaps"] == [[420, 450]]
    assert {tuple(point) for point in lane["points"]} == {
        tuple(point) for piece in original["lanes"] for point in piece["points"]}
    # Interpolation in the dash gap is permitted; extending beyond observed paint is not.
    for piece in original["lanes"]:
        ys = [point[1] for point in piece["points"]]
        np.testing.assert_allclose(lane_xs(lane, ys), lane_xs(piece, ys))


def test_neighboring_parallel_boundaries_are_not_dash_continuations():
    a, b = chain(400, 420), chain(450, 470, offset=45)
    assert join_score(a, b, 800, 800) is None
    repaired = repair_geometry(geometry([a, b]), paint_for([a, b]))
    assert repaired["source_converter"]["joined_chains"] == 0


def test_crossing_or_transverse_pieces_are_not_joined():
    a = chain(400, 420, slope=1.5)
    crossing = chain(450, 470, slope=-1.5)
    transverse = chain(450, 456, slope=8.)
    assert join_score(a, crossing, 800, 800) is None
    assert join_score(a, transverse, 800, 800) is None
    repaired = repair_geometry(geometry([transverse]), paint_for([transverse]))
    assert not any(lane["slot"] for lane in repaired["lanes"])


def test_short_outer_paint_recovery_preserves_ego_boundary_and_extent():
    ego = chain(380, 650, slope=.5, slot=3)
    outer = chain(450, 465, slope=2.)
    repaired = repair_geometry(geometry([ego, outer]), paint_for([ego, outer]), recover_short=True)
    slots = {lane["slot"]: lane for lane in repaired["lanes"] if lane["slot"]}
    assert set(slots) == {3, 4}
    assert slots[3]["points"] == ego["points"]
    assert slots[4]["points"] == outer["points"]
    assert (slots[4]["y_top"], slots[4]["y_bottom"]) == (450, 465)


def test_short_paint_is_a_separate_suggestion_by_default():
    ego = chain(380, 650, slope=.5, slot=3)
    outer = chain(450, 465, slope=2.)
    original = geometry([ego, outer])
    repaired = repair_geometry(original, paint_for([ego, outer]))
    assert repaired["lanes"] == original["lanes"]
    assert repaired["source_converter"]["recovered_short_chains"] == 0
    assert repaired["source_converter"]["short_recovery_candidates"] == 1
    suggestion, = repaired["source_converter"]["short_chain_suggestions"]
    assert suggestion["slot"] == 4
    assert suggestion["points"] == outer["points"]


def test_inward_fragment_does_not_displace_existing_ego_slot():
    ego = chain(380, 650, slope=-1.5, slot=2)
    inward = chain(450, 470, slope=-1.2)
    original = geometry([ego, inward])
    repaired = repair_geometry(original, paint_for([ego, inward]))
    assert repaired["lanes"] == original["lanes"]
    assert repaired["source_converter"]["recovered_short_chains"] == 0
    assert "inward_or_duplicate_fragment" in repaired["source_converter"]["warnings"]


def test_no_repair_preserves_all_original_geometry_fields():
    original = geometry([chain(380, 650, slope=.5, slot=3)])
    original["lanes"][0]["flags"] = ["curved", "merge_or_split"]
    repaired = repair_geometry(original, paint_for(original["lanes"]))
    assert {key: value for key, value in repaired.items() if key != "source_converter"} == original


def test_joining_duplicate_existing_slots_retains_inner_slot():
    a, b = chain(400, 440, slot=3), chain(470, 510, slot=4)
    repaired = repair_geometry(geometry([a, b]), paint_for([a, b]))
    assert repaired["source_converter"]["joined_chains"] == 1
    assert [lane["slot"] for lane in repaired["lanes"] if lane["slot"]] == [3]
    assert repaired["lanes"][0]["repair_original_slots"] == [3, 4]


def test_source_stop_line_and_arrow_do_not_add_boundaries():
    _, classes, _ = scene(stop_line=True, arrow=True)
    mask = lane_prob_from_classes(classes)
    original = extract_lanes(mask, GeoParams(horizon_y=.42), my_car=(classes == 5).astype(np.float32))
    repaired = repair_geometry(original, mask)
    assert [lane["slot"] for lane in repaired["lanes"] if lane["slot"]] == [1, 2, 3, 4]
    assert repaired["tags"]["crosswalk_or_stop_line"]
    for slot in (1, 2, 3, 4):
        old = next(lane for lane in original["lanes"] if lane["slot"] == slot)
        new = next(lane for lane in repaired["lanes"] if lane["slot"] == slot)
        ys = np.arange(max(old["y_top"], new["y_top"]), min(old["y_bottom"], new["y_bottom"]) + 1)
        np.testing.assert_allclose(lane_xs(old, ys), lane_xs(new, ys), atol=8)


def test_cached_suggestions_are_an_explicit_candidate_without_changing_active_labels():
    from copy import deepcopy
    from bcdata.source_converter import source_candidate
    ego = chain(380, 650, slope=.5, slot=3)
    outer = chain(450, 465, slope=2.)
    original = geometry([ego, outer])
    active = repair_geometry(original, paint_for([ego, outer]))
    before = deepcopy(active)
    assert [l['slot'] for l in active['lanes'] if l['slot']] == [3]
    candidate = source_candidate(original, None, 'a'*64, 'b'*64, repaired=active)
    assert [l['slot'] for l in candidate['lanes'] if l['slot']] == [3, 4]
    assert sum(l['slot'] is None for l in candidate['lanes']) == 0
    assert candidate['source_guided'] is True
    assert candidate['model_meta'] == {'source_sha256':'a'*64,'geometry_sha256':'b'*64}
    assert active == before
