"""Evaluator rejects geometry regressions and converter input contamination."""
from copy import deepcopy

import numpy as np
import pytest

from tools.evaluate_a2d2_repair import (approved_regression_reasons, invoke_source_only,
                                      known_failure_check, paint_supported_identity,
                                      promotion_evidence, source_boundary_retention,
                                      source_supported_raster_score,unsupported_inward_promotions)


def lane(slot,x,top=20,bottom=80):
    return {'slot':slot,'points':[[x,top,True],[x,bottom,True]]}


def geometry(*lines):
    return {'width':100,'height':100,'lanes':list(lines)}


def test_converter_receives_source_only_and_cannot_mutate_input():
    original=geometry(lane(2,20));maps={'lane':np.zeros((100,100),np.float32)}
    def mutate(geo,paint,*args,**kwargs):
        paint[50,20]=1
        return deepcopy(geo)
    with pytest.raises(ValueError,match='mutated'):
        invoke_source_only(mutate,original,maps,None)
    def mutate_geo(geo,paint,*args,**kwargs):
        geo['lanes']=[]
        return deepcopy(geo)
    with pytest.raises(ValueError,match='mutated'):
        invoke_source_only(mutate_geo,original,maps,None)


def test_human_extensions_without_source_paint_are_not_required_targets():
    paint=np.zeros((100,100),np.float32);paint[20:41,50]=1
    human=geometry(lane(2,50,20,90));candidate=geometry(lane(2,50,20,40))
    result=paint_supported_identity(candidate,human,paint)['2']
    assert result['source_supported_row_recall']==1
    assert result['unsupported_human_rows']==50
    assert source_supported_raster_score(candidate,human,paint)['tp']==1
    record=approved_record()
    record['raster_scores']={'old':source_supported_raster_score(human,human,paint),
                             'candidate':source_supported_raster_score(candidate,human,paint)}
    record['paint_supported_identity']={'old':paint_supported_identity(human,human,paint),
                                       'candidate':paint_supported_identity(candidate,human,paint)}
    assert approved_regression_reasons(record)==[]


def test_source_supported_raster_still_rejects_wrong_slot_or_extra_lane():
    paint=np.zeros((100,100),np.float32);paint[20:81,50]=1
    human=geometry(lane(2,50))
    wrong=source_supported_raster_score(geometry(lane(1,50)),human,paint)
    assert (wrong['tp'],wrong['fp'],wrong['fn'])==(0,1,1)
    extra=source_supported_raster_score(geometry(lane(2,50),lane(3,70)),human,paint)
    assert (extra['tp'],extra['fp'],extra['fn'])==(1,1,0)


def approved_record():
    measured={'sufficient_source_rows':True,'matched_rows_within_12px':50,'source_supported_row_recall':1.}
    return {'candidate_duplicate_slots':[],
            'raster_scores':{'old':{'tp':1,'fp':0,'fn':0},'candidate':{'tp':1,'fp':0,'fn':0}},
            'paint_supported_identity':{'old':{'2':dict(measured)},'candidate':{'2':dict(measured)}},
            'crossings':{'old':[],'candidate':[]}}


def test_same_lane_count_does_not_hide_same_slot_extent_regression():
    record=approved_record();record['paint_supported_identity']['candidate']['2'].update(
        matched_rows_within_12px=35,source_supported_row_recall=.7)
    assert 'source_supported_same_slot_extent_regressed_2' in approved_regression_reasons(record)


def test_same_lane_count_does_not_hide_human_raster_displacement():
    record=approved_record();record['raster_scores']['candidate']={'tp':0,'fp':1,'fn':1}
    assert approved_regression_reasons(record)==['same_slot_human_raster_regressed']


def test_new_extra_lane_is_an_approved_regression():
    record=approved_record();record['raster_scores']['candidate']['fp']=1
    assert 'same_slot_human_raster_regressed' in approved_regression_reasons(record)


def test_nonoverlapping_source_fragment_cannot_establish_inward_slot():
    paint=np.zeros((100,100),np.float32);paint[40:81,20]=1;paint[10:31,50]=1
    original=geometry(lane(2,20,40,80))
    candidate=geometry(lane(1,20,40,80),lane(2,50,10,30))
    record={'image_id':'fixture','old_slots':[2],'v4_slots':[1,2],
            'source_boundary_retention':source_boundary_retention(original,candidate,paint),
            'source_fragment_overlap':promotion_evidence(original,candidate,paint)}
    assert unsupported_inward_promotions(record)
    # Actual parallel source paint at the same rows supplies different evidence.
    paint[40:81,50]=1;candidate=geometry(lane(1,20,40,80),lane(2,50,40,80))
    record['source_fragment_overlap']=promotion_evidence(original,candidate,paint)
    assert not unsupported_inward_promotions(record)


def known_record():
    return {'image_id':'a2d2-85cc2b7cbe649894','candidate_duplicate_slots':[],
            'candidate_slots':[1,2,3],'crossings':{'candidate':[]},
            'source_paint_support':{'candidate':{'fraction':1.}},
            'qc':{'candidate':{'issues':[]}},'unsupported_inward_promotions':[],
            'warnings':[]}


def test_matching_source_pixels_in_three_wrong_slots_is_not_a_resolved_divider():
    record=known_record();result=known_failure_check(record)
    assert result['status']=='unresolved'
    assert 'source_boundary_slot_identities_not_recovered' in result['criteria_failures']
    record['warnings']=['low_confidence']
    assert known_failure_check(record)['status']=='unresolved'
    record['warnings']=['a2d2_dash_join_ambiguous']
    assert known_failure_check(record)['status']=='flagged'
    record['candidate_slots']=[2];record['warnings']=[]
    assert known_failure_check(record)['status']=='passed'
