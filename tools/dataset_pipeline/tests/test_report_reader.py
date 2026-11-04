import pytest
from fastapi.testclient import TestClient

from bcdata.report_reader import optional_report
from bcdata.review_server import create_app
from tests.test_review import build_review_fixture


@pytest.mark.parametrize('payload',[b'',b'\0'*32,b'{broken',b'null',b'[]',b'\xff\xfe'])
def test_damaged_optional_reports_do_not_block_reviewing(tmp_path,payload):
    settings=build_review_fixture(tmp_path/'data')
    for name in ('ufld_training.json','ufld_queue.json','model_status.json','paint_proposals.json'):
        settings.path('reports',name).write_bytes(payload)
    with TestClient(create_app(settings,start_worker=False),base_url='http://127.0.0.1',client=('127.0.0.1',12345)) as client:
        health=client.get('/api/health')
        assert health.status_code==200 and health.json()['training']=='status_unavailable'
        stats=client.get('/api/stats')
        assert stats.status_code==200
        assert stats.json()['reviewed']==0
        assert stats.json()['ufld_training']['phase']=='status_unavailable'
        assert stats.json()['ufld_queue'] is None
        image=client.get('/api/images/sample-0')
        assert image.status_code==200 and image.json()['image_id']=='sample-0'
    assert settings.path('reports','ufld_training.json').read_bytes()==payload


def test_optional_report_preserves_valid_data_and_independent_fallback(tmp_path):
    path=tmp_path/'report.json';path.write_text('{"phase":"training","epoch":7}')
    assert optional_report(path)=={'phase':'training','epoch':7}
    fallback={'phase':'status_unavailable'}
    first=optional_report(tmp_path/'missing.json',fallback);first['phase']='changed'
    assert fallback=={'phase':'status_unavailable'}
