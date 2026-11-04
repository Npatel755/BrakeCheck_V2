"""Small read-only training reports; never load datasets or model checkpoints."""
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import time

import psutil
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

TERMINAL = {'trained_awaiting_review', 'complete', 'complete_awaiting_comparison_review',
            'error', 'failed', 'stopped', 'stopped_by_user', 'paused', 'cancelled', 'smoke_passed', 'candidate_cache_complete'}
FIELDS = ('phase', 'epoch', 'epochs', 'step', 'total_steps', 'processed', 'total',
          'train_images', 'val_images', 'loss', 'train_loss', 'learning_rate',
          'best_val_f1', 'best_short_span_val_f1', 'best_geometry_val_f1', 'peak_vram_gib',
          'error_type', 'error_reason', 'error', 'stop_reason', 'architecture', 'backbone',
          'false_positive_budget', 'baseline_recall', 'selected_recall', 'best_recall_within_budget')


def scalars(value, keys):
    return {k: v[:1200] if isinstance(v, str) else v for k in keys
            if (v := value.get(k)) is not None and
            (isinstance(v, str) or type(v) is bool or
             type(v) in (int, float) and math.isfinite(v))}


def timestamp(value):
    try:
        result = float(value) if type(value) in (int, float) else datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


class ExperimentDashboard:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def safe(self, path):
        path = Path(path).resolve()
        if not any(path.is_relative_to(self.root / name) for name in ('models/ufld_baseline', 'experiments')):
            raise ValueError('Report leaves training directories')
        return path

    def read(self, path):
        path = self.safe(path)
        with path.open('rb') as stream:
            raw = stream.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError('Report is too large')
        return json.loads(raw)

    def reports(self):
        paths = set()
        base = self.root / 'models/ufld_baseline'
        experiments = self.root / 'experiments'
        paths.update(base.glob('*/status.json'))
        for pattern in ('*/*/status.json', '*/*/*/status.json'):
            paths.update(experiments.glob(pattern))
        for path in experiments.glob('*/*/experiment.json'):
            if not (path.parent / 'status.json').exists() and not any(path.parent.glob('*/status.json')):
                paths.add(path.parent / 'ufld_training.json' if (path.parent / 'ufld_training.json').exists() else path)
        result = {}
        for path in paths:
            try:
                path = self.safe(path)
                key = hashlib.sha256(str(path.relative_to(self.root)).encode()).hexdigest()[:20]
                result[key] = path
            except (OSError, ValueError):
                continue
        return result

    @staticmethod
    def worker(state, run):
        pid = state.get('pid')
        if type(pid) is not int or pid <= 0:
            return None
        try:
            process = psutil.Process(pid)
            arguments = process.cmdline()
            command = ' '.join(arguments).replace('\\', '/').lower()
            # A saved PID can be reused by another process after training exits.
            matches = str(run).replace('\\', '/').lower() in command
            if not matches and hasattr(process, 'cwd'):
                cwd = Path(process.cwd())
                matches = any((cwd / value).resolve() == run.resolve() for value in arguments if value and not value.startswith('-'))
            return process.is_running() and matches
        except psutil.NoSuchProcess:
            return False
        except (psutil.AccessDenied, OSError):
            return None

    def summary(self, key, path):
        state = self.read(path)
        if not isinstance(state, dict):
            raise ValueError('Expected an object')
        result = scalars(state, FIELDS)
        phase = result.get('phase', state.get('state', 'unknown'))
        alive = self.worker(state, path.parent)
        effective = phase if phase in TERMINAL else 'interrupted' if alive is False else phase
        updated = timestamp(state.get('updated_at')) or path.stat().st_mtime
        result.update(id=key, run_id=path.parent.name,
                      name=str(path.parent.relative_to(self.root)),
                      kind='Cleared training' if path.is_relative_to(self.root / 'models') else 'Experiment',
                      reported_phase=phase, phase=effective, worker_alive=alive,
                      updated_at=updated, age_seconds=max(0, time.time() - updated),
                      active=alive is True and phase not in TERMINAL and phase not in {'waiting_for_reference', 'waiting_for_gpu'})
        validation = state.get('validation')
        result['validation'] = scalars(validation, ('loss', 'lane_f1', 'precision', 'recall', 'tp', 'fp', 'fn')) if isinstance(validation, dict) else {}
        comparisons = validation.get('decoder_comparisons', {}) if isinstance(validation, dict) else {}
        result['decoder_comparisons'] = {k: scalars(v, ('lane_f1', 'precision', 'recall', 'tp', 'fp', 'fn'))
                                        for k, v in comparisons.items() if isinstance(v, dict)}
        sampling = state.get('source_sampling')
        if isinstance(sampling, dict):
            result['sampling_mode'] = sampling.get('mode')
        return result

    def catalog(self):
        runs, warnings = [], []
        for key, path in self.reports().items():
            try:
                runs.append(self.summary(key, path))
            except (OSError, ValueError, TypeError):
                warnings.append(f'{path.parent.name}: status unavailable or being updated')
        runs.sort(key=lambda r: (r['active'], r['updated_at']), reverse=True)
        queues = []
        for path in (self.root / 'experiments/overnight_collections').glob('active.json'):
            try:
                state = self.read(path)
                queues.append({'state': state.get('state', 'unknown'),
                               'worker_alive': self.worker(state, path.parent),
                               'jobs': [scalars(j, ('id', 'collection_id', 'state', 'stop_reason', 'error'))
                                        for j in state.get('jobs', [])[:20] if isinstance(j, dict)]})
            except (OSError, ValueError, TypeError, AttributeError):
                warnings.append('Experiment queue status unavailable')
        return {'runs': runs, 'queues': queues, 'warnings': warnings, 'server_time': time.time()}

    def detail(self, key):
        path = self.reports().get(key)
        if path is None:
            raise HTTPException(404, 'Training run not found')
        try:
            result = self.summary(key, path)
            history_path = path.parent / 'history.json'
            history = self.read(history_path) if history_path.exists() else []
            result['history'] = []
            if isinstance(history, list):
                for row in history[-200:]:
                    if isinstance(row, dict):
                        point = scalars(row, ('epoch', 'step', 'train_loss'))
                        validation = row.get('validation')
                        point['validation'] = scalars(validation if isinstance(validation, dict) else {}, ('loss', 'lane_f1'))
                        result['history'].append(point)
            result['log'] = ''
            for name in ('training.log', 'ufld.stdout.log', 'train.log'):
                log = self.safe(path.parent / name)
                if log.is_file():
                    with log.open('rb') as stream:
                        stream.seek(max(0, log.stat().st_size - 12000))
                        result['log'] = stream.read(12000).decode('utf-8', errors='replace')
                    break
            return result
        except (OSError, ValueError, TypeError):
            raise HTTPException(503, 'Run status is being updated; retry shortly')


def create_experiment_dashboard_router(root, require_session):
    router = APIRouter(prefix='/api/studio/experiments', dependencies=[Depends(require_session)])
    store = ExperimentDashboard(root)

    @router.get('')
    def catalog():
        return JSONResponse(store.catalog(), headers={'Cache-Control': 'no-store'})

    @router.get('/{run_id}')
    def detail(run_id: str):
        return JSONResponse(store.detail(run_id), headers={'Cache-Control': 'no-store'})

    return router
