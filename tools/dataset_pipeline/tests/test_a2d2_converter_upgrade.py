import json,time
from bcdata.upgrade_a2d2_converter import eligible,run
from tests.test_source_validation import setup_source


def test_only_unreviewed_unclaimed_new_flagged_geometries_are_selected(tmp_path):
    settings,store,samples,held=setup_source(tmp_path)
    with store.registry() as reg:
        reg.conn.execute("UPDATE images SET source='a2d2'")
        reg.conn.execute("UPDATE source_conversion_checks SET state='flagged',diagnostics=?",(json.dumps({'reasons':['repair_uncertain_shared_foreground_slot_order']}),))
        reg.conn.commit()
    candidates=eligible(settings,{})
    assert set(candidates)==set(samples+[held])
    assert not eligible(settings,candidates)
    with store.registry() as reg:
        reg.conn.execute("UPDATE images SET locked=1,review_status='approved' WHERE image_id=?",(samples[0],))
        reg.conn.execute('INSERT INTO review_claims VALUES (?,?,?)',(samples[1],'phone',time.time()+60))
        reg.conn.commit()
    assert set(eligible(settings,{}))=={held}
    with store.registry() as reg:
        assert reg.count('SELECT count(*) FROM review_events')==0
        assert reg.count("SELECT count(*) FROM annotations WHERE kind='human'")==0


def test_invalid_evaluation_cannot_start_preparing_or_applying(tmp_path,monkeypatch):
    settings,store,_,_=setup_source(tmp_path)
    import bcdata.upgrade_a2d2_converter as module
    called=[]
    def reject(path):raise ValueError('bad evidence')
    monkeypatch.setattr(module,'verify_regression_report',reject)
    monkeypatch.setattr(module,'prepare',lambda *a,**kw:called.append('prepare'))
    import pytest
    with pytest.raises(ValueError,match='bad evidence'):run(settings,tmp_path/'invalid.json')
    assert not called
    report=json.loads(settings.path('reports','a2d2_converter_v2_upgrade.json').read_text())
    assert report['phase']=='error' and report['installed']==0
