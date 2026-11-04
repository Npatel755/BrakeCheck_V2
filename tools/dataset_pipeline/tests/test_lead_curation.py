import json
import sqlite3

import numpy as np
from PIL import Image
import pytest

from bcdata.bootstrap import atomic_json
from bcdata.lead_curation import CHECKPOINT_SHA, RECIPE, prepare_pool, publish
from bcdata.lead_review import LeadDecision, LeadReview, file_sha
from bcdata.quality import phash, read_rgb


def setup(tmp_path):
    (tmp_path/'licenses').mkdir()
    (tmp_path/'licenses/comma10k-LICENSE.txt').write_text('Fixture source licence')
    db=sqlite3.connect(tmp_path/'metadata.sqlite')
    db.execute('''CREATE TABLE images(image_id TEXT PRIMARY KEY,source TEXT,session_id TEXT,source_ref TEXT,
        split TEXT,status TEXT,review_status TEXT,pool TEXT,synthetic INTEGER,path TEXT,sha256 TEXT,phash TEXT,tags TEXT,license_id TEXT)''')
    records=[]
    for index in range(4):
        path=tmp_path/f'original-{index}.png'
        rgb=np.random.default_rng(index).integers(0,256,(180,320,3),dtype=np.uint8)
        Image.fromarray(rgb).save(path)
        h=f'{phash(read_rgb(path)):016x}'
        record={'image_id':f'original-{index}','source':'comma10k','session_id':f'session-{index}',
            'source_ref':'fixture','source_split':'train','pool':'commercial','image_source':path.name,
            'source_sha256':file_sha(path),'license_id':'comma10k','lighting':'day','phash':h,
            'width':320,'height':180,'detections':[{'label':'car','box':[130,60,190,120],'confidence':.9,'forward_candidate':True}],
            'has_forward_vehicle_candidate':True,'curation_bucket':'central_vehicle'}
        db.execute('INSERT INTO images VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (record['image_id'],'comma10k',record['session_id'],'fixture','train','curated','none','commercial',0,
             str(path),record['source_sha256'],h,'{}','comma10k'))
        records.append(record)
    db.commit();db.close()
    LeadReview(tmp_path).db().close()
    return records


def selected(tmp_path,records,target):
    root=tmp_path/'experiments/lead_cars'
    atomic_json(root/f'selected-{target}.json',{'recipe':RECIPE,'checkpoint_sha256':CHECKPOINT_SHA,
                                              'records':records,'target_total':target})


def test_append_preserves_real_labels_and_source_splits(tmp_path):
    records=setup(tmp_path)
    selected(tmp_path,records[:2],2)
    first=publish(tmp_path,2)
    assert first['added']==2 and first['human_labels_created']==0
    assert publish(tmp_path,2)==first
    store=LeadReview(tmp_path);item=store.queue(owner='a')
    store.save(LeadDecision(image_id=item['image_id'],revision=item['revision'],fingerprint=item['fingerprint'],
        event_id='genuine-human-box',action='box',box=[130,60,190,120]),'Human','a')
    db=store.db();before=[tuple(r) for r in db.execute('SELECT * FROM images ORDER BY image_id')];events=[tuple(r) for r in db.execute('SELECT * FROM events')];db.close()
    selected(tmp_path,records[2:3],3)
    assert publish(tmp_path,3)['added']==1
    db=store.db()
    assert [tuple(r) for r in db.execute("SELECT * FROM images WHERE image_id IN ('original-0','original-1') ORDER BY image_id")]==before
    assert [tuple(r) for r in db.execute('SELECT * FROM events')]==events
    assert all(json.loads(r[0])['source_split']=='train' for r in db.execute('SELECT record_json FROM images'))
    db.close()


@pytest.mark.parametrize('field,value',[('split','test'),('split','val'),('review_status','rejected'),('session_id','changed-session')])
def test_changed_or_protected_registry_image_cannot_be_published(tmp_path,field,value):
    records=setup(tmp_path);selected(tmp_path,records[:1],1)
    db=sqlite3.connect(tmp_path/'metadata.sqlite')
    db.execute(f'UPDATE images SET {field}=? WHERE image_id=?',(value,'original-0'));db.commit();db.close()
    with pytest.raises(ValueError,match='authoritative'):
        publish(tmp_path,1)
    assert LeadReview(tmp_path).status()['counts']['total']==0


def test_source_pool_excludes_protected_sessions_and_rejected_photos(tmp_path):
    setup(tmp_path)
    db=sqlite3.connect(tmp_path/'metadata.sqlite')
    db.execute("UPDATE images SET split='val',session_id='session-1' WHERE image_id='original-0'")
    db.execute("UPDATE images SET review_status='rejected' WHERE image_id='original-2'")
    db.commit();db.close()
    assert prepare_pool(tmp_path)['candidate_pool']==1
    pool=json.loads((tmp_path/'experiments/lead_cars/screening-pool.json').read_text())
    assert pool['records'][0]['image_id']=='original-3'


def test_source_tampering_never_publishes_partial_batch(tmp_path):
    records=setup(tmp_path);selected(tmp_path,records[:2],2)
    (tmp_path/'original-1.png').write_bytes(b'changed')
    with pytest.raises(ValueError,match='bytes changed'):
        publish(tmp_path,2)
    assert LeadReview(tmp_path).status()['counts']['total']==0
