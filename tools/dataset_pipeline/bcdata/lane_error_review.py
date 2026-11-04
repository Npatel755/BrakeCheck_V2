"""Separate review notes for model mistakes; no Registry or label writes."""
import json
from pathlib import Path
import re
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

ID = re.compile(r'[A-Za-z0-9_-]{1,180}\Z')
ACTIONS = {'confirmed_model_error', 'label_question', 'acceptable', 'skip'}
OUTCOMES = {'acceptable': 'accepted_predictions', 'confirmed_model_error': 'confirmed_model_errors',
            'label_question': 'label_questions', 'skip': 'skipped'}


def checked(value):
    if not ID.fullmatch(value):
        raise HTTPException(400, 'Invalid review identifier')
    return value


class ErrorDecision(BaseModel):
    image_id: str
    policy: str
    fingerprint: str
    action: str
    notes: str = Field(default='', max_length=4000)


class LaneErrorReview:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.base = self.root / 'reports/model_errors'

    def inside(self, path, root=None):
        root = (root or self.base).resolve()
        path = Path(path).resolve()
        if not path.is_relative_to(root):
            raise HTTPException(400, 'Artifact path leaves review directory')
        return path

    def read(self, path):
        try:
            return json.loads(self.inside(path).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise HTTPException(404, 'Review artifact unavailable')

    def metadata(self, collection):
        value = self.read(self.base / checked(collection) / 'metadata.json')
        if value.get('schema') != 'lane_errors:v1' or value.get('split') != 'val' or value.get('test_images_used') != 0:
            raise HTTPException(400, 'Invalid validation review collection')
        return value

    def collections(self):
        result = []
        for path in self.base.glob('*/metadata.json'):
            try:
                value = self.metadata(path.parent.name)
                result.append({k: value.get(k) for k in ('id', 'phase', 'stage', 'created_at', 'cases', 'metrics',
                                                         'checkpoint_sha256', 'run', 'processed', 'historical',
                                                         'false_positive_budget', 'baseline_recall', 'selected_recall',
                                                         'device', 'precision', 'interpretation')})
            except HTTPException:
                continue
        return sorted(result, key=lambda r: r.get('created_at', 0), reverse=True)

    def decisions(self, collection):
        path = self.inside(self.base / checked(collection) / 'reviews.sqlite')
        if not path.exists():
            return {}
        with sqlite3.connect(path) as con:
            con.row_factory = sqlite3.Row
            return {(r['image_id'], r['policy']): dict(r) for r in con.execute('SELECT * FROM decisions')}

    @staticmethod
    def review_state(row, policy, decisions):
        saved = decisions.get((row['image_id'], policy))
        return saved if saved and saved['fingerprint'] == row['fingerprint'] else None

    def summary(self, collection, policy, *, rows=None, decisions=None):
        meta = self.metadata(collection)
        result = {'alert_photos': 0, 'unreviewed': 0, 'accepted_predictions': 0,
                  'confirmed_model_errors': 0, 'label_questions': 0, 'skipped': 0,
                  'open_alerts': 0, 'policy': policy, 'measured_metrics_changed': False}
        if meta['phase'] != 'complete':
            return result
        rows = self.read(self.base / collection / 'index.json') if rows is None else rows
        decisions = self.decisions(collection) if decisions is None else decisions
        for row in rows:
            counts = row['policies'].get(policy)
            if not counts or not (counts['fp'] or counts['fn']):
                continue
            result['alert_photos'] += 1
            saved = self.review_state(row, policy, decisions)
            result[OUTCOMES.get(saved['action'], 'unreviewed') if saved else 'unreviewed'] += 1
        result['open_alerts'] = result['alert_photos'] - result['accepted_predictions']
        return result

    def queue(self, collection, policy, kind, status, after, limit):
        meta = self.metadata(collection)
        if meta['phase'] != 'complete':
            return {'items': [], 'phase': meta['phase'], 'total': 0}
        if kind not in {'all', 'missed', 'false_positive', 'geometry'} or status not in {
                'pending', 'reviewed', 'all', 'accepted', 'confirmed_model_error', 'label_question', 'skipped', 'open', 'follow_up'}:
            raise HTTPException(400, 'Unknown review filter')
        rows = self.read(self.base / collection / 'index.json')
        decisions, selected = self.decisions(collection), []
        for row in rows:
            counts = row['policies'].get(policy)
            if not counts or not (counts['fp'] or counts['fn']):
                continue
            if kind == 'missed' and not counts['fn'] or kind == 'false_positive' and not counts['fp']:
                continue
            if kind == 'geometry' and not (counts['fp'] and counts['fn']):
                continue
            saved = self.review_state(row, policy, decisions)
            reviewed = bool(saved)
            if status == 'pending' and reviewed or status == 'reviewed' and not reviewed:
                continue
            action = saved['action'] if saved else None
            if status == 'accepted' and action != 'acceptable' or status == 'skipped' and action != 'skip':
                continue
            if status in {'confirmed_model_error', 'label_question'} and action != status:
                continue
            if status == 'open' and action == 'acceptable':
                continue
            if status == 'follow_up' and action not in {'confirmed_model_error', 'label_question', 'skip'}:
                continue
            selected.append({**row, 'counts': counts, 'review': saved if reviewed else None})
        total = len(selected)
        if after:
            position = next((i for i, row in enumerate(selected) if row['image_id'] == after), -1)
            selected = selected[position + 1:]
        return {'items': selected[:limit], 'total': total, 'phase': 'complete',
                'review_summary': self.summary(collection, policy, rows=rows, decisions=decisions),
                'next_after': selected[min(limit, len(selected)) - 1]['image_id'] if selected else None}

    def case(self, collection, image_id):
        self.metadata(collection)
        value = self.read(self.base / checked(collection) / 'cases' / (checked(image_id) + '.json'))
        if value.get('split') != 'val' or value.get('image_id') != image_id:
            raise HTTPException(400, 'Invalid validation case')
        return value

    def photo(self, collection, image_id):
        meta, case = self.metadata(collection), self.case(collection, image_id)
        snapshot = Path(meta['snapshot']).resolve()
        if not any(snapshot.is_relative_to(self.root / part) for part in ('models/ufld_baseline', 'experiments')):
            raise HTTPException(400, 'Snapshot outside training directories')
        path = self.inside(snapshot / case['image'], snapshot)
        if path.suffix.lower() not in {'.jpg', '.jpeg', '.png'} or not path.is_file():
            raise HTTPException(404, 'Validation image unavailable')
        return path

    def decide(self, collection, body, reviewer):
        case = self.case(collection, body.image_id)
        if body.fingerprint != case['fingerprint']:
            raise HTTPException(409, 'Predictions changed; reload this case')
        if body.policy not in case['policies'] or body.action not in ACTIONS:
            raise HTTPException(400, 'Unknown policy or review action')
        path = self.inside(self.base / checked(collection) / 'reviews.sqlite')
        with sqlite3.connect(path) as con:
            con.execute('CREATE TABLE IF NOT EXISTS decisions(image_id TEXT,policy TEXT,fingerprint TEXT,action TEXT,notes TEXT,reviewer TEXT,updated_at REAL,PRIMARY KEY(image_id,policy))')
            con.execute('INSERT OR REPLACE INTO decisions VALUES(?,?,?,?,?,?,?)',
                        (body.image_id, body.policy, body.fingerprint, body.action, body.notes, reviewer, time.time()))
        messages = {'acceptable': 'Model prediction accepted. This photo is dismissed from open error alerts.',
                    'confirmed_model_error': 'Model mistake confirmed. Added to the confirmed mistakes queue.',
                    'label_question': 'Label question flagged. Added to the label questions queue.',
                    'skip': 'Deferred. Available in the skipped queue.'}
        return {'saved': True, 'labels_changed': False, 'training_eligibility_changed': False,
                'message': messages[body.action], 'summary': self.summary(collection, body.policy)}

    def undo(self, collection, image_id, policy, fingerprint):
        case = self.case(collection, image_id)
        if fingerprint != case['fingerprint'] or policy not in case['policies']:
            raise HTTPException(409, 'Predictions changed; reload this case')
        path = self.inside(self.base / checked(collection) / 'reviews.sqlite')
        if path.exists():
            with sqlite3.connect(path) as con:
                con.execute('DELETE FROM decisions WHERE image_id=? AND policy=? AND fingerprint=?',
                            (image_id, policy, fingerprint))
        return {'undone': True, 'summary': self.summary(collection, policy)}


def create_lane_error_router(root, require_session):
    store = LaneErrorReview(root)
    router = APIRouter(prefix='/api/studio/model-errors', dependencies=[Depends(require_session)])

    @router.get('')
    def collections():
        return {'collections': store.collections()}

    @router.get('/{collection}/queue')
    def queue(collection: str, policy: str = 'stock', kind: str = 'all', status: str = 'pending',
              after: str = '', limit: int = Query(1, ge=1, le=24)):
        return store.queue(collection, policy, kind, status, after, limit)

    @router.get('/{collection}/summary')
    def summary(collection: str, policy: str = 'stock'):
        return store.summary(collection, policy)

    @router.delete('/{collection}/decisions/{image_id}')
    def undo(collection: str, image_id: str, policy: str, fingerprint: str):
        return store.undo(collection, image_id, policy, fingerprint)

    @router.get('/{collection}/cases/{image_id}')
    def case(collection: str, image_id: str):
        return store.case(collection, image_id)

    @router.get('/{collection}/cases/{image_id}/photo')
    def photo(collection: str, image_id: str):
        return FileResponse(store.photo(collection, image_id), headers={'Cache-Control': 'private, no-store'})

    @router.post('/{collection}/decision')
    def decide(collection: str, body: ErrorDecision, session=Depends(require_session)):
        return store.decide(collection, body, session['reviewer'])

    return router
