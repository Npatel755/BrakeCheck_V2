import json,secrets,uuid
import pytest
from fastapi.testclient import TestClient
from bcdata.review_server import create_app
from bcdata.review_store import ReviewStore
from bcdata.remote_access import load_remote, valid_key
from tests.test_review import build_review_fixture


@pytest.mark.parametrize('config', [
    None, [], 'text', {}, {'enabled': True, 'url': []},
    {'enabled': True, 'url': 'https://[', 'access_key': 'a' * 43},
    {'enabled': True, 'url': 'https://test.trycloudflare.com', 'access_key': ['a'] * 43},
    {'enabled': True, 'url': 'https://test.trycloudflare.com', 'access_key': 'é' * 43},
    *({'enabled': True, 'url': url, 'access_key': 'a' * 43} for url in [
        'http://test.trycloudflare.com', 'https://user:secret@test.trycloudflare.com',
        'https://test.trycloudflare.com:0', 'https://test.trycloudflare.com:99999999999999999999',
        'https://test.trycloudflare.com/path', 'https://test.trycloudflare.com?key=value',
        'https://test.trycloudflare.com#fragment', 'https://test.trycloudflare.com.evil.example',
    ]),
])
def test_invalid_remote_configuration_fails_closed_and_is_preserved(settings, config):
    path = settings.path('reports', 'remote_access.json')
    original = json.dumps(config)
    path.write_text(original, encoding='utf-8')
    assert load_remote(settings) == {}
    assert not valid_key(settings, 'a' * 43)
    assert path.read_text(encoding='utf-8') == original


def test_corrupt_remote_configuration_does_not_break_local_review(settings):
    path = settings.path('reports', 'remote_access.json')
    path.write_text('not JSON', encoding='utf-8')
    app = create_app(settings, start_worker=False)
    with TestClient(app, base_url='http://localhost', client=('127.0.0.1', 123)) as local:
        assert local.get('/api/session').json()['local']
    assert path.read_text(encoding='utf-8') == 'not JSON'


def test_public_tunnel_never_inherits_loopback_desktop_privileges(tmp_path):
    settings=build_review_fixture(tmp_path);key=secrets.token_urlsafe(32)
    config={'enabled':True,'url':'https://brakecheck-test.trycloudflare.com','access_key':key}
    settings.path('reports','remote_access.json').write_text(json.dumps(config))
    app=create_app(settings,start_worker=False)
    with TestClient(app,base_url=config['url'],client=('127.0.0.1',123)) as remote:
        assert remote.get('/api/stats').status_code==401
        assert remote.get('/api/images/sample-0/photo').status_code==401
        assert remote.post('/api/pair',json={'code':app.state.pairing['pin']}).status_code==401
        assert remote.post('/api/remote-pair',json={'access_key':'x'*43}).status_code==401
        response=remote.post('/api/remote-pair',json={'access_key':key,'reviewer':'Alex'})
        assert response.status_code==200
        assert 'Secure' in response.headers['set-cookie'] and 'HttpOnly' in response.headers['set-cookie']
        assert remote.get('/api/stats').status_code==200
        assert remote.get('/api/host').status_code==403
        assert remote.get('/api/host/remote-qr').status_code==403
        assert remote.post('/api/host/revoke').status_code==403
        assert remote.get('/api/leaderboard').status_code==200
        with TestClient(app,base_url='http://localhost',client=('127.0.0.1',123)) as local:
            assert local.get('/api/stats',headers={'cf-ray':'remote-proxy'}).status_code==401
            assert local.post('/api/host/revoke').status_code==200
        assert load_remote(settings)['access_key']!=key
        assert remote.get('/api/stats').status_code==401
        assert remote.post('/api/remote-pair',json={'access_key':key}).status_code==401


def test_leaderboard_counts_current_human_decisions_and_separates_skips(tmp_path):
    settings=build_review_fixture(tmp_path);store=ReviewStore(settings)
    store.decide('sample-0','approved','Nick',0,str(uuid.uuid4()))
    store.decide('sample-0','approved','Nick',1,str(uuid.uuid4()))
    store.decide('sample-1','rejected','Alex',0,str(uuid.uuid4()))
    store.defer('sample-2',0,'Sam','desktop')
    board=store.leaderboard()['reviewers'];by={row['reviewer']:row for row in board}
    assert sum(row['reviewed'] for row in board)==2
    assert by['Nick']['reviewed']==by['Nick']['approved']==1
    assert by['Alex']['reviewed']==by['Alex']['rejected']==1
    assert by['Sam']['reviewed']==0 and by['Sam']['review_later']==1
