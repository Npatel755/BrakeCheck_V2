import json
import os
from pathlib import Path
import sys

import pytest
from bcdata import balanced_trial


def test_trial_waits_then_launches_same_budget_and_balanced_sampling(tmp_path, monkeypatch):
    output = tmp_path / 'experiments/source_balanced/run-balanced'
    reference = tmp_path / 'models/ufld_baseline/run-reference'
    output.mkdir(parents=True); reference.mkdir(parents=True)
    (output / 'dataset').mkdir()
    (output / 'dataset/manifest.json').write_text('[]')
    warm = tmp_path / 'best.pt'; warm.write_bytes(b'checkpoint')
    request = {'output_run': str(output), 'reference_run': str(reference), 'data_root': str(tmp_path),
               'epochs': 20, 'train_images': 32638, 'val_images': 1586, 'source_sampling': {'mode': 'balanced'},
               'code_hashes': {}, 'manifest_sha256': 'pinned', 'warm_start': str(warm),
               'warm_start_sha256': 'pinned', 'batch_size': 4, 'accumulation': 8,
               'learning_rate': .000625, 'warmup_steps': 40}
    request['recall_policy'] = str(tmp_path / 'recall-policy.json')
    path = output / 'request.json'; path.write_text(json.dumps(request))
    (reference / 'status.json').write_text(json.dumps({'phase': 'training', 'pid': os.getpid()}))
    monkeypatch.setattr(sys, 'argv', ['trial', '--request', str(path)])
    monkeypatch.setattr(balanced_trial, 'file_sha256', lambda p: 'pinned')
    def finish(_):
        assert json.loads((output / 'status.json').read_text())['phase'] == 'waiting_for_reference'
        (reference / 'status.json').write_text(json.dumps({'phase': 'trained_awaiting_review', 'epoch': 20}))
    monkeypatch.setattr(balanced_trial.time, 'sleep', finish)
    commands = []
    class Child:
        pid = 123
        def __init__(self, command, **kwargs): commands.append(command)
        def wait(self): return 0
    monkeypatch.setattr(balanced_trial.subprocess, 'Popen', Child)
    balanced_trial.main()
    assert len(commands) == 1
    command = commands[0]
    assert command[command.index('--source-sampling')+1] == 'balanced'
    assert command[command.index('--epochs')+1] == '20'
    assert command[command.index('--learning-rate')+1] == '0.000625'
    assert command[command.index('--warm-start')+1] == str(warm)
    assert command[command.index('--status-report')+1] == str(output / 'experiment_training.json')
    assert command[command.index('--recall-policy')+1] == request['recall_policy']


def test_failed_reference_does_not_launch_trial(tmp_path, monkeypatch):
    output = tmp_path / 'experiments/source_balanced/run-balanced';output.mkdir(parents=True)
    reference = tmp_path / 'reference';reference.mkdir()
    (reference / 'status.json').write_text('{"phase":"error"}')
    request = {'output_run': str(output), 'reference_run': str(reference), 'data_root': str(tmp_path),
               'epochs': 20, 'train_images': 1, 'val_images': 1, 'source_sampling': {'mode':'balanced'}}
    path = output / 'request.json';path.write_text(json.dumps(request))
    monkeypatch.setattr(sys, 'argv', ['trial', '--request', str(path)])
    with pytest.raises(RuntimeError, match='Reference stopped'):
        balanced_trial.main()
    assert json.loads((output / 'status.json').read_text())['phase'] == 'error'
