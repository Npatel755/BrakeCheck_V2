"""Refresh reviewed export, freeze it, and start one hidden local baseline."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import re
from collections import Counter

PIPE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPE))
from filelock import FileLock, Timeout
from bcdata.bootstrap import atomic_json
from bcdata.local import local_settings
from bcdata.review_export import export_reviewed
from bcdata.source_validation import refresh_groups
from bcdata.ufld_baseline import freeze_export
from bcdata.ufld_queue import completed_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--epochs', type=int, default=20)
    parser.add_argument('--learning-rate', type=float, default=.00625)
    parser.add_argument('--warmup-steps', type=int, default=110)
    parser.add_argument('--evaluate-initial', action='store_true')
    parser.add_argument('--run-label', default='latest-audits')
    args = parser.parse_args()
    if args.epochs < 1 or not 0 < args.learning_rate <= 1 or args.warmup_steps < 0:
        parser.error('Use positive epochs and learning rate, and a nonnegative warmup')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', args.run_label):
        parser.error('Run label must use lowercase letters, digits and hyphens')
    settings = local_settings()
    report = settings.path('reports', 'latest_ufld_launch.json')
    state = {'phase': 'preparing', 'pid': os.getpid(), 'epochs': args.epochs,
             'learning_rate': args.learning_rate, 'warmup_steps': args.warmup_steps,
             'evaluate_initial': args.evaluate_initial, 'run_label': args.run_label,
             'started_at': time.time()}

    def update(**changes):
        state.update(changes, updated_at=time.time())
        atomic_json(report, state)
        print(json.dumps(state), flush=True)

    with FileLock(str(settings.path('logs', 'latest-ufld-start.lock')), timeout=0):
        try:
            hold_path = settings.path('reports', 'training_hold.json')
            if hold_path.exists() and json.loads(hold_path.read_text()).get('active'):
                update(phase='held_for_audits', reason=json.loads(hold_path.read_text()).get('reason'))
                return
            previous = json.loads(settings.path('reports', 'ufld_training.json').read_text())
            predecessor = Path(previous['output'])
            completed = completed_run(predecessor)
            if not completed:
                raise ValueError('The previous UFLDv2 run is not complete; refusing an overlapping run')
            update(phase='refreshing_reviewed_export', predecessor=str(predecessor))
            refresh_groups(settings)
            while True:
                try:
                    export = export_reviewed(settings)
                    break
                except Timeout:
                    update(phase='waiting_for_review_export_lock')
            if export.get('errors'):
                raise ValueError('Reviewed export validation failed: ' + str(export['errors'])[:600])
            run = settings.path('models', 'ufld_baseline', 'run-' + time.strftime('%Y%m%d-%H%M%S') + '-' + args.run_label)
            run.mkdir(exist_ok=False)
            update(phase='freezing_training_snapshot', output=str(run))
            _, records, marker = freeze_export(settings, run)
            counts = dict(Counter(row['split'] for row in records))
            reserved = json.loads((run / 'dataset/reserved_test_ids.json').read_text())
            update(phase='starting_gpu_worker', train_images=counts['train'], val_images=counts['val'],
                   reserved_test_images=len(reserved), export_revision=marker['revision'],
                   batch_size=4, accumulation=8, effective_batch_size=32)
            if hold_path.exists() and json.loads(hold_path.read_text()).get('active'):
                update(phase='held_for_audits', reason=json.loads(hold_path.read_text()).get('reason'))
                return
            command = [str(PIPE / '.gpu-venv/Scripts/python.exe'), '-u', '-m', 'bcdata.ufld_baseline',
                       '--prepared-run', str(run), '--warm-start', completed['checkpoint'], '--epochs', str(args.epochs),
                       '--learning-rate', str(args.learning_rate), '--warmup-steps', str(args.warmup_steps),
                       '--batch-size', '4', '--accumulation', '8', '--gpu-lock-timeout', '-1']
            if args.evaluate_initial:
                command.append('--evaluate-initial')
            with (run / 'training.log').open('ab', buffering=0) as log:
                child = subprocess.Popen(command, cwd=PIPE, stdout=log, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL,
                                         env={**os.environ, 'BRAKECHECK_DATA_ROOT': str(settings.root)},
                                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            update(phase='gpu_worker_started', worker_pid=child.pid, log_path=str(run / 'training.log'))
        except BaseException as error:
            update(phase='error', error_type=type(error).__name__, error=str(error)[:700])
            raise


if __name__ == '__main__':
    main()
