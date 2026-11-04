import copy
import numpy as np
import pytest

from bcdata.recall_search import SHORT_RULE, STOCK_RULE, choose_with_budget, score_matrix, search_rules, select_indices


def candidates():
    return [{'axis': axis, 'slot': slot, 'positive_anchors': 4, 'mean_confidence': .8,
             'points': [[300,300],[302,310],[305,320],[309,330]]}
            for axis in ('row','col') for slot in range(4)]


def test_selection_respects_assigned_heads_confidence_and_anchor_thresholds():
    values=candidates()
    assert select_indices(values,SHORT_RULE)==[1,2,4,7]
    assert select_indices(values,STOCK_RULE)==[]
    values[1]['positive_anchors']=3
    values[2]['mean_confidence']=.74
    assert select_indices(values,SHORT_RULE)==[4,7]
    # Relaxing the gate can recover a short trace, not the excluded row/column heads.
    assert select_indices(values,{**SHORT_RULE,'row_minimum':2,'col_minimum':2,'confidence':.7})==[1,2,4,7]


def test_union_preserves_stock_lanes_without_duplicate_predictions():
    values=candidates();values[1].update(positive_anchors=40,mean_confidence=.6)
    selected=select_indices(values,{**SHORT_RULE,'preserve_stock':True})
    assert selected==[1,2,4,7]
    assert len(set(selected))==len(selected)
    assert select_indices(values,SHORT_RULE)==[2,4,7]


def test_budget_selection_maximizes_recall_rather_than_f1_or_unlimited_recall():
    rows=[dict(name='high_f1',recall=.7,fp=150,lane_f1=.85),
          dict(name='recall_within_limit',recall=.76,fp=243,lane_f1=.82),
          dict(name='over_limit',recall=.9,fp=244,lane_f1=.84)]
    assert choose_with_budget(rows,243)['name']=='recall_within_limit'
    assert choose_with_budget(rows,0) is None
    tied=rows+[dict(name='same_recall_fewer_false',recall=.76,fp=240,lane_f1=.82)]
    assert choose_with_budget(tied,243)['name']=='same_recall_fewer_false'


def test_matching_counts_empty_images_and_one_to_one_lanes():
    assert score_matrix(np.empty((8,0)),[1,2])==dict(tp=0,fp=2,fn=0)
    matrix=np.array([[.9],[.8],[.1]])
    assert score_matrix(matrix,[0,1])==dict(tp=1,fp=1,fn=0)
    assert score_matrix(matrix,[])==dict(tp=0,fp=0,fn=1)


def test_search_space_is_bounded_and_contains_baseline():
    rules=search_rules()
    assert len(rules)==80 and SHORT_RULE in rules
    assert len({str(sorted(rule.items())) for rule in rules})==80


def test_cached_geometry_matches_both_existing_decoders():
    torch=pytest.importorskip('torch')
    from tests.test_ufld_short_span import predictions,activate
    from bcdata.recall_search import candidate_heads
    from bcdata.ufld_baseline import decode
    outputs=predictions()
    activate(outputs,'row',1,list(range(10,55)),.9)
    activate(outputs,'col',3,[20,21,22,23],.8)
    heads=candidate_heads(outputs,0)
    for name,rule in [('stock',STOCK_RULE),('short_span_v1',SHORT_RULE)]:
        assert [heads[i]['points'] for i in select_indices(heads,rule)]==decode(outputs,0,name)


def test_policy_validation_rejects_invalid_budget_and_unsafe_values(tmp_path):
    import json
    from bcdata.recall_search import load_policy
    path=tmp_path/'policy.json'
    config={'rule':SHORT_RULE,'false_positive_budget':243,'test_images_used':0,'manifest_sha256':'f'*64,'validation_images':1586}
    path.write_text(json.dumps(config));assert load_policy(path)['false_positive_budget']==243
    for changed in ({'false_positive_budget':-1},{'test_images_used':1},{'rule':{**SHORT_RULE,'confidence':float('nan')}},
                    {'rule':{**SHORT_RULE,'row_minimum':True}},{'rule':{**SHORT_RULE,'geometry':'yes'}}):
        path.write_text(json.dumps({**config,**changed}))
        with pytest.raises(ValueError):load_policy(path)


def test_recall_checkpoint_does_not_replace_a_budget_compliant_best_with_more_false_alarms(tmp_path):
    torch=pytest.importorskip('torch')
    from bcdata.ufld_baseline import save_training_checkpoint
    model=torch.nn.Linear(1,1);optimizer=torch.optim.SGD(model.parameters(),lr=.001)
    def metrics(recall,fp):
        return {'lane_f1':.72,'decoder_comparisons':{'short_span_v1':{'lane_f1':.79},
                'recall_budget_v4':{'recall':recall,'fp':fp}}}
    save_training_checkpoint(tmp_path,model,optimizer,epoch=0,step=1,history=[],config={},metrics=metrics(.74,240),
                             best=-1,best_short_span=-1,recall_fp_budget=243)
    saved=(tmp_path/'best_recall.pt').read_bytes()
    save_training_checkpoint(tmp_path,model,optimizer,epoch=1,step=2,history=[],config={},metrics=metrics(.9,244),
                             best=.72,best_short_span=.79,best_recall=.74,recall_fp_budget=243)
    assert (tmp_path/'best_recall.pt').read_bytes()==saved
    checkpoint=torch.load(tmp_path/'last.pt',weights_only=False)
    assert checkpoint['best_recall']==.74


def test_runtime_recall_decoder_uses_selected_rule_without_changing_original(monkeypatch):
    pytest.importorskip('torch')
    from tests.test_ufld_short_span import predictions,activate
    from bcdata import ufld_baseline as baseline
    from bcdata.recall_search import candidate_heads
    outputs=predictions();activate(outputs,'row',1,list(range(35,42)),.72)
    rule={**SHORT_RULE,'row_minimum':2,'col_minimum':2,'confidence':.7,'geometry':True}
    original=baseline.decode(outputs,0,'stock')
    monkeypatch.setitem(baseline.DECODER_POLICIES,'recall_budget_v4',rule)
    heads=candidate_heads(outputs,0)
    assert baseline.decode(outputs,0,'recall_budget_v4')==[heads[i]['points'] for i in select_indices(heads,rule)]
    assert len(baseline.decode(outputs,0,'recall_budget_v4'))==1
    assert baseline.decode(outputs,0,'stock')==original==[]
