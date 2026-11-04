"""Run the authorized matched-budget sampling trial after its reference finishes."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import psutil

from filelock import FileLock
from .bootstrap import atomic_json
from .teachers.base import file_sha256


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True, type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    output, reference = Path(request['output_run']), Path(request['reference_run'])
    data_root = Path(request['data_root']).resolve()
    if not output.resolve().is_relative_to(data_root / 'experiments/source_balanced'):
        raise ValueError('Trial output must stay in its experiment directory')
    with FileLock(str(output / 'queue.lock'), timeout=0):
        def status(phase, **extra):
            atomic_json(output / 'status.json', {'pid': os.getpid(), 'phase': phase, 'output': str(output),
                'reference_run': str(reference), 'epochs': request['epochs'], 'train_images': request['train_images'],
                'val_images': request['val_images'], 'source_sampling': request['source_sampling'],
                'auto_promote': False, 'test_images_used': 0, 'updated_at': time.time(), **extra})
        try:
            while True:
                state = json.loads((reference / 'status.json').read_text())
                if state.get('phase') == 'trained_awaiting_review':
                    if state.get('epoch', 0) < request['epochs']:
                        raise ValueError('Reference did not complete the matched epoch budget')
                    break
                if state.get('phase') in {'error', 'stopped', 'stopped_by_user', 'cancelled'}:
                    raise RuntimeError('Reference stopped; balanced trial was not started automatically')
                if not psutil.pid_exists(state.get('pid', -1)):
                    raise RuntimeError('Reference worker exited without completing; inspect its run before restarting')
                status('waiting_for_reference')
                time.sleep(10)
            hold = data_root / 'reports/training_hold.json'
            if hold.exists() and json.loads(hold.read_text()).get('active'):
                raise RuntimeError('A newer user training hold is active')
            pipe = Path(__file__).resolve().parents[1]
            for relative, digest in request['code_hashes'].items():
                if file_sha256(pipe / relative) != digest:
                    raise ValueError('Training code changed after trial authorization: ' + relative)
            if file_sha256(output / 'dataset/manifest.json') != request['manifest_sha256']:
                raise ValueError('Frozen dataset manifest changed')
            if file_sha256(Path(request['warm_start'])) != request['warm_start_sha256']:
                raise ValueError('Starting checkpoint changed')
            command = [sys.executable, '-u', '-m', 'bcdata.ufld_baseline', '--prepared-run', str(output),
                '--warm-start', request['warm_start'], '--evaluate-initial', '--source-sampling', 'balanced',
                '--epochs', str(request['epochs']), '--batch-size', str(request['batch_size']),
                '--accumulation', str(request['accumulation']), '--learning-rate', str(request['learning_rate']),
                '--warmup-steps', str(request['warmup_steps']), '--gpu-lock-timeout', '-1',
                '--status-report', str(output / 'experiment_training.json'), '--error-review-root', str(data_root)]
            if request.get('recall_policy'):
                command.extend(['--recall-policy', request['recall_policy']])
            with (output / 'training.log').open('ab', buffering=0) as log:
                child = subprocess.Popen(command, cwd=pipe, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    env={**os.environ, 'BRAKECHECK_DATA_ROOT': str(data_root)},
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            atomic_json(output / 'worker_launch.json', {'pid': child.pid, 'command': command, 'started_at': time.time()})
            code = child.wait()
            if code:
                raise RuntimeError('Balanced trainer exited with code ' + str(code))
        except Exception as error:
            status('error', error_reason=str(error)[:1200])
            raise


if __name__ == '__main__':
    main()
