"""Observed pairwise ordering must be unique, local and non-crossing."""
from copy import deepcopy
import numpy as np
from bcdata.geometry import Segment
from bcdata.source_converter_a2d2_v2 import _from_chain, _slots, _pairwise_foreground_order


def candidate(x, intervals):
    segments=[]
    for top,bottom in intervals:
        ys=np.arange(top,bottom+1)
        segments.append(Segment(ys,np.full(len(ys),float(x)),np.full(len(ys),5.)))
    return _from_chain(segments)


def geometry():
    return {'height':800,'width':800,'horizon_y':320,'valid_bottom':780,'lanes':[]}


def staggered():
    return [candidate(150,[(600,640),(680,720)]),candidate(350,[(600,640),(740,780)]),
            candidate(600,[(680,720),(740,780)])]


def test_staggered_paint_has_unique_pairwise_order_without_common_rows():
    anchors=staggered(); before=deepcopy(anchors)
    assert not set(anchors[0]['source_observed_rows']) & set(anchors[1]['source_observed_rows']) & set(anchors[2]['source_observed_rows'])
    assert _pairwise_foreground_order(anchors,geometry())==anchors
    assert anchors==before


def test_staggered_order_recovers_slots_from_measured_pairs():
    anchors=staggered(); original=geometry()
    original['lanes']=deepcopy(anchors)
    for line,slot in zip(original['lanes'],(1,2,3)):
        line['slot']=slot
    before=deepcopy(original)
    okay,warnings,matches=_slots(anchors,original)
    assert okay and not warnings
    assert [a['slot'] for a in anchors]==[1,2,3]
    assert all(a['pairwise_order_evidence']['unique_observed_order'] for a in anchors)
    assert original==before


def test_missing_pairwise_order_is_still_ambiguous():
    anchors=[candidate(150,[(600,640)]),candidate(350,[(680,720)])]
    assert _pairwise_foreground_order(anchors,geometry()) is None


def test_only_far_overlap_cannot_authorize_foreground_slots():
    anchors=[candidate(150,[(400,440),(600,640)]),candidate(350,[(400,440),(680,720)])]
    assert _pairwise_foreground_order(anchors,geometry()) is None


def test_crossing_or_touching_pair_is_rejected():
    anchors=staggered(); anchors[1]['fit']['xs']=[150.]*len(anchors[1]['fit']['xs'])
    assert _pairwise_foreground_order(anchors,geometry()) is None


def test_nonoverlapping_phases_can_use_strict_observed_foreground_envelopes():
    anchors=[candidate(150,[(600,640),(700,720)]),candidate(350,[(650,690),(740,780)])]
    assert not set(anchors[0]['source_observed_rows']) & set(anchors[1]['source_observed_rows'])
    assert _pairwise_foreground_order(anchors,geometry())==anchors
