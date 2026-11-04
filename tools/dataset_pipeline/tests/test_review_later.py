"""Deferred queue routing survives restarts without creating human decisions."""
import uuid
import pytest
from fastapi.testclient import TestClient
from bcdata.review_store import ReviewStore,ReviewError
from bcdata.review_server import create_app
from bcdata.review_export import export_reviewed
from tests.test_review import build_review_fixture
from tests.test_source_validation import setup_source,approve_samples


def test_skip_persists_and_excludes_normal_and_batch_queues(tmp_path):
    settings=build_review_fixture(tmp_path);store=ReviewStore(settings)
    image=store.next_image('desktop')['image_id']
    detail=store.detail(image)
    store.defer(image,detail['revision'],'Nick','desktop')
    store=ReviewStore(settings)
    assert store.detail(image)['review_later']
    assert store.stats()['review_later']==1
    assert store.next_image('desktop')['image_id']!=image
    assert image not in [d['image_id'] for d in store.batch('desktop')]
    assert store.next_image('desktop',status='later')['image_id']==image
    assert store.batch('desktop',status='later')[0]['image_id']==image
    with store.registry() as reg:
        assert reg.count('SELECT count(*) FROM review_events')==0
        assert reg.count("SELECT count(*) FROM annotations WHERE kind='human'")==0
        assert reg.image(image)['review_status'] not in ('approved','corrected','rejected')
    store.defer(image,detail['revision'],'Nick','desktop',False)
    assert not store.detail(image)['review_later']
    assert store.stats()['review_later']==0


def test_real_decision_resolves_deferral_and_concurrency_is_checked(tmp_path):
    settings=build_review_fixture(tmp_path);store=ReviewStore(settings)
    store.next_image('other',split='train')
    with pytest.raises(ReviewError,match='Another reviewer'):
        store.defer('sample-0',0,'Nick','desktop')
    store.defer('sample-0',0,'Nick','other')
    with pytest.raises(ReviewError,match='changed'):
        store.defer('sample-0',1,'Nick','other')
    store.decide('sample-0','approved','Nick',0,str(uuid.uuid4()),owner='desktop')
    assert not store.detail('sample-0')['review_later']
    with pytest.raises(ReviewError,match='saved decision'):
        store.defer('sample-0',1,'Nick','desktop')


def test_deferred_audit_stays_held_and_not_replaced_by_fake_vote(tmp_path):
    settings,store,samples,held=setup_source(tmp_path)
    sample=samples[0]
    store.defer(sample,0,'Nick','desktop')
    stats=store.stats()
    assert stats['source_audit_samples']==2
    assert stats['active_source_audit_samples']==1
    assert stats['deferred_source_audit_samples']==1
    assert sample not in [d['image_id'] for d in store.batch('desktop',status='audit_sample')]
    assert export_reviewed(settings)['exported']==0


def test_deferring_validated_source_holds_it_out_of_new_exports(tmp_path):
    settings,store,samples,held=setup_source(tmp_path)
    approve_samples(store,samples)
    from bcdata.source_validation import refresh_groups
    refresh_groups(settings)
    assert export_reviewed(settings)['exported']==3
    store.defer(held,0,'Nick','desktop')
    assert export_reviewed(settings)['exported']==2
    store.defer(held,0,'Nick','desktop',False)
    assert export_reviewed(settings)['exported']==3


def test_defer_endpoint_uses_phone_session_access_rules(tmp_path):
    settings=build_review_fixture(tmp_path);app=create_app(settings,start_worker=False)
    with TestClient(app,base_url='http://localhost',client=('127.0.0.1',123)) as client:
        response=client.post('/api/images/sample-0/defer',json={'revision':0})
        assert response.status_code==200 and response.json()['review_later']
        assert client.get('/api/next?status=later').json()['image_id']=='sample-0'
    with TestClient(app,base_url='http://localhost',client=('192.168.1.77',123)) as client:
        assert client.post('/api/images/sample-0/defer',json={'revision':0}).status_code==401
