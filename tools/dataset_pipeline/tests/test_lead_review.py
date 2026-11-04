import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from bcdata.lead_review import (LeadClaim, LeadDecision, LeadReview, candidate_targets,
                                create_lead_router, digest, encoded, file_sha)


def fixture(tmp_path,count=3):
    store=LeadReview(tmp_path)
    db=store.db()
    for index in range(count):
        path=store.root/f'images/test-{index}.png'
        path.parent.mkdir(exist_ok=True)
        Image.new('RGB',(320,180),(40+index,70,90)).save(path)
        record={'image_id':f'test-{index}','image':path.relative_to(store.root).as_posix(),
                'source':'comma10k','source_sha256':file_sha(path),'session_id':f'session-{index}',
                'source_split':'train','split':'val' if index==2 else 'train',
                'license_id':'comma10k','width':320,'height':180,'curation_bucket':'central_vehicle',
                'detections':[{'box':[130,60,190,120],'label':'car','confidence':.9}]}
        db.execute('INSERT INTO images(image_id,record_json,fingerprint) VALUES(?,?,?)',
                   (record['image_id'],encoded(record),digest(record)))
    db.commit();db.close()
    return store


def decision(item,action='box',event='label-event-0001',box=(130,60,190,120)):
    return LeadDecision(image_id=item['image_id'],revision=item['revision'],fingerprint=item['fingerprint'],
                        action=action,event_id=event,box=list(box) if action=='box' else None)


def test_queue_claims_separate_reviewers_and_labeled_does_not_create_negatives(tmp_path):
    store=fixture(tmp_path)
    alice=store.queue(owner='alice');bob=store.queue(owner='bob')
    assert alice['image_id']!=bob['image_id']
    assert alice['box'] is None and alice['decision'] is None
    assert store.status()['counts']=={'total':3,'pending':3,'box':0,'no_lead':0,'skip':0}
    with pytest.raises(HTTPException) as e:store.detail(alice['image_id'],'bob')
    assert e.value.status_code==409
    store.save(decision(alice),'Alice','alice')
    assert store.queue('labeled','alice')['image_id']==alice['image_id']


def test_save_is_idempotent_and_stale_edits_cannot_overwrite(tmp_path):
    store=fixture(tmp_path)
    item=store.queue(owner='alice');request=decision(item)
    result=store.save(request,'Alice','alice')
    assert store.save(request,'Alice','alice')==result
    with pytest.raises(HTTPException) as e:store.save(decision(item,event='new-stale-save'),'Alice','alice')
    assert e.value.status_code==409
    changed=request.model_copy(update={'box':[10,10,40,40]})
    with pytest.raises(HTTPException):store.save(changed,'Alice','alice')
    db=store.db();assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==1;db.close()


@pytest.mark.parametrize('box',[(0,0,0,30),(-1,3,40,50),(10,10,400,60),(30,20,10,50),
                                (10,10,11,30),(0,0,float('nan'),50)])
def test_invalid_boxes_never_commit(tmp_path,box):
    store=fixture(tmp_path);item=store.queue(owner='a')
    with pytest.raises((HTTPException,ValueError)):
        store.save(decision(item,box=box),'A','a')
    assert store.status()['counts']['pending']==3


def test_source_changed_blocks_labels(tmp_path):
    store=fixture(tmp_path);item=store.queue(owner='a')
    (store.root/item['image']).write_bytes(b'changed')
    with pytest.raises(HTTPException) as e:store.save(decision(item),'A','a')
    assert e.value.status_code==409


def test_explicit_no_lead_is_exported_skip_and_pending_are_excluded(tmp_path):
    store=fixture(tmp_path,count=4)
    for index,action in enumerate(('box','no_lead','skip')):
        item=store.detail(f'test-{index}','a')
        store.save(decision(item,action,f'event-save-{index}'),'A','a')
    result=store.export();folder=Path(result['path'])
    assert result['images']==2 and result['boxes']==result['no_lead']==1
    assert result['manifest_sha256']==file_sha(folder/'manifest.json')
    records=json.loads((folder/'manifest.json').read_text())
    assert {r['decision'] for r in records}=={'box','no_lead'}
    coco=json.loads((folder/'train.coco.json').read_text())
    assert len(coco['images'])==2 and len(coco['annotations'])==1
    assert coco['annotations'][0]['bbox']==[130,60,60,60]
    assert store.export()==result


def test_undo_restores_full_previous_label_and_rejects_newer_saves(tmp_path):
    store=fixture(tmp_path)
    item=store.queue(owner='a');first=store.save(decision(item),'A','a')
    item=store.detail(item['image_id'],'a')
    second=store.save(decision(item,'no_lead','no-lead-event-001'),'A','a')
    with pytest.raises(HTTPException):store.undo(first['event_id'],'A','a')
    with pytest.raises(HTTPException):store.undo(second['event_id'],'Other','a')
    result=store.undo(second['event_id'],'A','a')
    assert store.undo(second['event_id'],'A','a')==result
    current=store.detail(item['image_id'],'a')
    assert current['box']==[130,60,190,120] and current['decision']=='box'


def test_detector_miss_never_turns_all_candidates_into_false_negatives():
    r={'detections':[{'box':[10,10,30,30]}]}
    targets=candidate_targets(r,[100,100,120,120],'box')
    assert targets['detector_missed_lead'] and not targets['usable_for_ranking']
    assert targets['candidate_labels'] is None
    assert candidate_targets(r,None,'no_lead')['candidate_labels']==[0]
    assert candidate_targets(r,[10,10,30,30],'box')['candidate_labels']==[1]


def test_training_status_reports_actual_worker_progress_and_ignores_invalid_files(tmp_path):
    store=fixture(tmp_path)
    assert not store.status()['training_started']
    run=store.root/'runs'/'pilot';run.mkdir(parents=True)
    (run/'status.json').write_text(json.dumps({'status':'training','epoch':3,'epochs':80,'private_path':'not exposed'}))
    status=store.status()
    assert status['training_started'] and status['training']['epoch']==3
    assert 'private_path' not in status['training']
    (run/'status.json').write_text(json.dumps({'status':'complete','best_epoch':8}))
    assert store.status()['training']['status']=='complete'
    (run/'status.json').write_text('{broken')
    assert store.status()['training'] is None


def test_expired_or_changed_claim_cannot_overwrite_new_labels(tmp_path):
    store=fixture(tmp_path);item=store.queue(owner='a')
    store.save(decision(item),'A','a')
    with pytest.raises(HTTPException):
        store.claim(item['image_id'],LeadClaim(revision=item['revision'],fingerprint=item['fingerprint']),'a')


def test_api_requires_existing_session_and_does_not_accept_extra_box_on_no_lead(tmp_path):
    store=fixture(tmp_path)
    def denied():raise HTTPException(401,'Pair first')
    app=FastAPI();app.include_router(create_lead_router(tmp_path,denied))
    with TestClient(app) as c:
        for url in ('/api/studio/lead-cars','/api/studio/lead-cars/next','/api/studio/lead-cars/images/test-0/photo'):
            assert c.get(url).status_code==401
        assert c.post('/api/studio/lead-cars/export',json={}).status_code==401
    app=FastAPI();app.include_router(create_lead_router(tmp_path,lambda:{'owner':'a','reviewer':'A'}))
    with TestClient(app) as c:
        item=c.get('/api/studio/lead-cars/next').json()
        body=decision(item,'no_lead').model_dump();body['box']=[1,2,30,40]
        assert c.post('/api/studio/lead-cars/labels',json=body).status_code==422
        body['box']=None
        assert c.post('/api/studio/lead-cars/labels',json=body).json()['saved']
        assert store.status()['counts']['no_lead']==1
