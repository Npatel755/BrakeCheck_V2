import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import psutil
import pytest

from bcdata.experiment_dashboard import ExperimentDashboard, create_experiment_dashboard_router


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


@pytest.fixture
def dashboard(tmp_path):
    run = tmp_path / 'models/ufld_baseline/run-cleared'
    write(run / 'status.json', {'pid': 99999999, 'phase': 'training', 'epoch': 2,
                              'epochs': 20, 'step': 50, 'total_steps': 1000,
                              'train_images': 32638, 'updated_at': 100,
                              'initialization': {'secret': 'must not be returned'},
                              'validation': {'lane_f1': .72}})
    return ExperimentDashboard(tmp_path), run


def test_dead_worker_does_not_look_live_and_report_is_read_only(dashboard):
    store, run = dashboard
    before = (run / 'status.json').read_bytes()
    result = store.catalog()['runs'][0]
    assert result['phase'] == 'interrupted'
    assert result['reported_phase'] == 'training'
    assert result['worker_alive'] is False and not result['active']
    assert result['train_images'] == 32638
    assert result['validation']['lane_f1'] == .72
    assert 'initialization' not in result
    assert (run / 'status.json').read_bytes() == before


def test_pid_reuse_and_real_worker(monkeypatch, dashboard):
    store, run = dashboard

    class Process:
        def __init__(self, pid): pass
        def cmdline(self): return ['python', '-m', 'unrelated']
        def is_running(self): return True

    monkeypatch.setattr(psutil, 'Process', Process)
    assert store.catalog()['runs'][0]['worker_alive'] is False
    monkeypatch.setattr(Process, 'cmdline', lambda self: ['python', '--prepared-run', str(run)])
    assert store.catalog()['runs'][0]['active'] is True
    write(run / 'status.json', {'phase': 'trained_awaiting_review', 'pid': 1})
    assert store.catalog()['runs'][0]['active'] is False


def test_bad_report_does_not_hide_valid_run(dashboard):
    store, run = dashboard
    broken = store.root / 'experiments/trial/run-broken/status.json'
    broken.parent.mkdir(parents=True)
    broken.write_text('{')
    result = store.catalog()
    assert len(result['runs']) == 1 and result['warnings']


def test_detail_history_logs_and_live_updates(dashboard):
    store, run = dashboard
    write(run / 'history.json', [{'epoch': 1, 'train_loss': 2.1, 'validation': {'lane_f1': .73}},
                                 {'epoch': 2, 'validation': []}])
    (run / 'training.log').write_text('x' * 20000 + '\nrecent output')
    key = store.catalog()['runs'][0]['id']
    result = store.detail(key)
    assert result['history'][0]['validation']['lane_f1'] == .73
    assert result['history'][1]['validation'] == {}
    assert len(result['log']) <= 12000 and result['log'].endswith('recent output')
    write(run / 'status.json', {'phase': 'error', 'error_reason': 'CUDA failed'})
    assert store.detail(key)['error_reason'] == 'CUDA failed'
    with pytest.raises(HTTPException) as error:
        store.detail('../../private')
    assert error.value.status_code == 404
    with pytest.raises(ValueError):
        store.read(store.root.parent / 'private.json')


def test_auth_and_no_cache(dashboard):
    store, run = dashboard
    app = FastAPI()

    def session(request: Request):
        if request.headers.get('x-paired') != 'yes':
            raise HTTPException(401, 'Pair first')

    app.include_router(create_experiment_dashboard_router(store.root, session))
    with TestClient(app) as client:
        url = '/api/studio/experiments'
        assert client.get(url).status_code == 401
        response = client.get(url, headers={'x-paired': 'yes'})
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        key = response.json()['runs'][0]['id']
        assert client.get(url + '/' + key).status_code == 401
        assert client.get(url + '/' + key, headers={'x-paired': 'yes'}).status_code == 200


def test_queue_and_experiment_discovery(dashboard):
    store, run = dashboard
    exp = store.root / 'experiments/flagged_teacher/run-proposals'
    write(exp / 'ufld_run/status.json', {'phase': 'validation', 'epoch': 3})
    write(exp / 'experiment.json', {'phase': 'training_ufld'})
    write(store.root / 'experiments/overnight_collections/active.json',
          {'state': 'stopped', 'pid': 99999999, 'jobs': [{'id': 'collection_2', 'state': 'stopped'}]})
    result = store.catalog()
    assert len(result['runs']) == 2
    assert result['queues'][0]['jobs'][0]['state'] == 'stopped'
    assert result['queues'][0]['worker_alive'] is False
