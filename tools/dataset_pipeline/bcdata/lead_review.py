"""Independent, append-only human lead-vehicle boxes in the existing Studio.

Image-level box or explicit no-lead decisions are authoritative. Detector boxes
are candidate evidence only; skips and unlabeled photos never become negatives.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .bootstrap import atomic_json

POLICY = 'lead-vehicle-visible-box-v1'
ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}\Z')
API = '/api/studio/lead-cars'


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(2**20), b''):
            h.update(block)
    return h.hexdigest()


def candidate_targets(record, box, decision):
    """Keep detector-missed human boxes out of false ranking negatives."""
    def iou(a, b):
        intersection = max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
        union = (a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-intersection
        return intersection/union if union>0 else 0
    overlaps = [iou(d['box'],box) if box else 0 for d in record.get('detections',[])]
    matched = bool(overlaps and max(overlaps)>=.5)
    return {'usable_for_ranking': decision=='no_lead' or matched,
            'detector_missed_lead': decision=='box' and not matched,
            'lead_iou': overlaps,
            'candidate_labels': ([int(v>=.5) for v in overlaps] if matched else
                                 [0]*len(overlaps) if decision=='no_lead' else None)}


def inside(root, relative):
    if not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative:
        raise ValueError('Invalid lead image path')
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        raise ValueError('Lead image path escapes collection')
    return path


class LeadDecision(BaseModel):
    image_id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=0)
    fingerprint: str = Field(min_length=64, max_length=64)
    event_id: str = Field(min_length=8, max_length=120)
    action: Literal['box', 'no_lead', 'skip']
    box: list[float] | None = Field(default=None, min_length=4, max_length=4)
    notes: str = Field(default='', max_length=2000)


class LeadClaim(BaseModel):
    revision: int = Field(ge=0)
    fingerprint: str = Field(min_length=64, max_length=64)
    release: bool = False


class LeadUndo(BaseModel):
    event_id: str = Field(min_length=8, max_length=120)


class LeadReview:
    def __init__(self, data_root):
        self.root = Path(data_root).resolve() / 'experiments/lead_cars'

    def db(self):
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.root / 'review.sqlite', timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
          CREATE TABLE IF NOT EXISTS images(image_id TEXT PRIMARY KEY,record_json TEXT NOT NULL,
            fingerprint TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,action TEXT,
            box_json TEXT,reviewer TEXT,notes TEXT NOT NULL DEFAULT '',updated_at REAL);
          CREATE INDEX IF NOT EXISTS lead_queue ON images(action,image_id);
          CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,image_id TEXT NOT NULL,
            reviewer TEXT NOT NULL,request_digest TEXT NOT NULL,before_json TEXT NOT NULL,
            after_json TEXT NOT NULL,result_json TEXT NOT NULL,created_at REAL NOT NULL);
          CREATE INDEX IF NOT EXISTS lead_events_image ON events(image_id,created_at);
          CREATE TABLE IF NOT EXISTS claims(image_id TEXT PRIMARY KEY,owner TEXT NOT NULL,expires REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS batches(batch_id TEXT PRIMARY KEY,receipt_json TEXT NOT NULL);
        ''')
        return db

    @staticmethod
    def _row(db, image_id):
        if not ID.fullmatch(image_id):
            raise HTTPException(400, 'Invalid lead image ID')
        row = db.execute('SELECT * FROM images WHERE image_id=?', (image_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Lead image not found')
        return row

    def _detail(self, row):
        record = json.loads(row['record_json'])
        return {**record, 'revision': row['revision'], 'fingerprint': row['fingerprint'],
                'decision': row['action'], 'box': json.loads(row['box_json']) if row['box_json'] else None,
                'reviewer': row['reviewer'], 'notes': row['notes'],
                'image_url': API + '/images/' + row['image_id'] + '/photo'}

    def status(self):
        db = self.db()
        try:
            counts = Counter()
            sources, splits, buckets = Counter(), Counter(), Counter()
            for row in db.execute('SELECT action,record_json FROM images'):
                counts['total'] += 1
                counts[row['action'] or 'pending'] += 1
                record = json.loads(row['record_json'])
                sources[record['source']] += 1
                splits[record['split']] += 1
                buckets[record['curation_bucket']] += 1
            training = self.training_status()
            return {'policy': POLICY, 'counts': {k: counts[k] for k in ('total', 'pending', 'box', 'no_lead', 'skip')},
                    'sources': dict(sources), 'splits': dict(splits), 'curation_buckets': dict(buckets),
                    'training_started': training is not None, 'training': training,
                    'instructions': 'Draw the tight visible box of the nearest vehicle ahead in your lane, including stopped traffic. '
                    'Other-lane, parked and oncoming vehicles are not leads. Choose No lead vehicle when there is none; '
                    'choose Unclear / skip if lane membership or vehicle identity cannot be judged from this photo.'}
        finally:
            db.close()

    def training_status(self):
        paths = sorted((self.root / 'runs').glob('*/status.json'),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for path in paths:
            try:
                status = json.loads(path.read_text())
                if status.get('status') not in ('training', 'complete', 'failed'):
                    continue
                fields = ('status', 'epoch', 'epochs', 'best_epoch', 'elapsed_seconds', 'validation_all')
                return {'run': path.parent.name, **{k: status[k] for k in fields if k in status}}
            except (OSError, ValueError, TypeError):
                continue
        return None

    def photo(self, image_id):
        db = self.db()
        try:
            record = json.loads(self._row(db, image_id)['record_json'])
        finally:
            db.close()
        path = inside(self.root, record['image'])
        if not path.is_file() or file_sha(path) != record['source_sha256']:
            raise HTTPException(409, 'Lead source image changed')
        return path

    def queue(self, queue='pending', owner='desktop', exclude=()):
        if queue not in ('pending', 'labeled', 'skip', 'all'):
            raise HTTPException(400, 'Unknown lead queue')
        db = self.db()
        try:
            db.execute('BEGIN IMMEDIATE')
            where = {'pending': 'i.action IS NULL', 'labeled': "i.action IN ('box','no_lead')",
                     'skip': "i.action='skip'", 'all': '1=1'}[queue]
            rows = db.execute('SELECT i.* FROM images i LEFT JOIN claims c USING(image_id) WHERE '
                + where + ' AND (c.owner IS NULL OR c.owner=? OR c.expires<=?) ORDER BY i.image_id',
                (owner, time.time()))
            row = next((r for r in rows if r['image_id'] not in exclude), None)
            if row:
                db.execute('DELETE FROM claims WHERE owner=? OR expires<=?', (owner, time.time()))
                db.execute('INSERT OR REPLACE INTO claims VALUES(?,?,?)', (row['image_id'], owner, time.time() + 600))
            db.commit()
            return self._detail(row) if row else None
        finally:
            db.close()

    def detail(self, image_id, owner):
        db = self.db()
        try:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db, image_id)
            self._check_claim(db, image_id, owner)
            db.execute('DELETE FROM claims WHERE owner=? AND image_id!=?', (owner, image_id))
            db.execute('INSERT OR REPLACE INTO claims VALUES(?,?,?)', (image_id, owner, time.time() + 600))
            db.commit()
            return self._detail(row)
        finally:
            db.close()

    @staticmethod
    def _check_claim(db, image_id, owner):
        claim = db.execute('SELECT * FROM claims WHERE image_id=?', (image_id,)).fetchone()
        if claim and claim['expires'] > time.time() and claim['owner'] != owner:
            raise HTTPException(409, 'Another reviewer is labeling this image. Your draft is kept.')

    def claim(self, image_id, body, owner):
        db = self.db()
        try:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db, image_id)
            if body.release:
                db.execute('DELETE FROM claims WHERE image_id=? AND owner=?', (image_id, owner))
            else:
                if row['revision'] != body.revision or row['fingerprint'] != body.fingerprint:
                    raise HTTPException(409, 'This lead image changed; reload before saving.')
                self._check_claim(db, image_id, owner)
                if db.execute('SELECT 1 FROM claims WHERE owner=? AND image_id!=? AND expires>?',
                              (owner, image_id, time.time())).fetchone():
                    raise HTTPException(409, 'This browser has moved to another image.')
                db.execute('INSERT OR REPLACE INTO claims VALUES(?,?,?)', (image_id, owner, time.time() + 600))
            db.commit()
            return {'ok': True}
        finally:
            db.close()

    @staticmethod
    def _state(row):
        return {k: row[k] for k in ('revision', 'action', 'box_json', 'reviewer', 'notes', 'updated_at')}

    def save(self, body, reviewer, owner):
        db = self.db()
        try:
            db.execute('BEGIN IMMEDIATE')
            request = digest({'body': body.model_dump(), 'reviewer': reviewer})
            retry = db.execute('SELECT * FROM events WHERE event_id=?', (body.event_id,)).fetchone()
            if retry:
                if retry['request_digest'] != request:
                    raise HTTPException(409, 'Save ID was reused with a different label')
                return json.loads(retry['result_json'])
            row = self._row(db, body.image_id)
            if row['revision'] != body.revision or row['fingerprint'] != body.fingerprint:
                raise HTTPException(409, 'A newer label exists. Your draft is kept; reload before saving.')
            self._check_claim(db, body.image_id, owner)
            record = json.loads(row['record_json'])
            if digest(record) != row['fingerprint'] or file_sha(inside(self.root, record['image'])) != record['source_sha256']:
                raise HTTPException(409, 'Source image or manifest changed')
            if body.action == 'box':
                box = body.box
                if (not box or any(not math.isfinite(v) for v in box)
                        or not 0 <= box[0] < box[2] <= record['width']
                        or not 0 <= box[1] < box[3] <= record['height']
                        or box[2] - box[0] < 3 or box[3] - box[1] < 3):
                    raise HTTPException(422, 'Draw one box at least 3 pixels wide and high inside the image')
            elif body.box is not None:
                raise HTTPException(422, 'No-lead and skip decisions cannot contain a box')
            before = self._state(row)
            db.execute('UPDATE images SET revision=revision+1,action=?,box_json=?,reviewer=?,notes=?,updated_at=? WHERE image_id=?',
                (body.action, encoded(body.box) if body.box else None, reviewer, body.notes, time.time(), body.image_id))
            after_row = self._row(db, body.image_id)
            result = {'event_id': body.event_id, 'image_id': body.image_id, 'revision': after_row['revision'], 'saved': True}
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?)',
                (body.event_id, body.image_id, reviewer, request, encoded(before), encoded(self._state(after_row)), encoded(result), time.time()))
            db.execute('DELETE FROM claims WHERE image_id=? AND owner=?', (body.image_id, owner))
            db.commit()
            return result
        finally:
            db.close()

    def undo(self, event_id, reviewer, owner):
        db = self.db()
        try:
            db.execute('BEGIN IMMEDIATE')
            undo_id = 'undo-' + digest(event_id)[:40]
            retry = db.execute('SELECT * FROM events WHERE event_id=?', (undo_id,)).fetchone()
            if retry:
                if retry['reviewer'] != reviewer:
                    raise HTTPException(403, 'Only your own labels can be undone')
                return json.loads(retry['result_json'])
            event = db.execute('SELECT * FROM events WHERE event_id=?', (event_id,)).fetchone()
            if not event or event_id.startswith('undo-'):
                raise HTTPException(404, 'Saved label not found')
            if event['reviewer'] != reviewer:
                raise HTTPException(403, 'Only your own labels can be undone')
            row = self._row(db, event['image_id'])
            if self._state(row) != json.loads(event['after_json']):
                raise HTTPException(409, 'A later label exists; undo cannot replace it')
            self._check_claim(db, row['image_id'], owner)
            before = json.loads(event['before_json'])
            db.execute('UPDATE images SET revision=revision+1,action=?,box_json=?,reviewer=?,notes=?,updated_at=? WHERE image_id=?',
                (before['action'], before['box_json'], before['reviewer'], before['notes'], before['updated_at'], row['image_id']))
            result = {'event_id': undo_id, 'image_id': row['image_id'], 'saved': True}
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?)', (undo_id, row['image_id'], reviewer, digest(event_id),
                       encoded(self._state(row)), encoded(self._state(self._row(db, row['image_id']))), encoded(result), time.time()))
            db.commit()
            return result
        finally:
            db.close()

    def export(self):
        """A new immutable, reviewed-only export; no pending/skipped negatives."""
        db = self.db()
        try:
            db.execute('BEGIN')
            rows = list(db.execute("SELECT * FROM images WHERE action IN ('box','no_lead') ORDER BY image_id"))
            records = []
            for row in rows:
                record = json.loads(row['record_json'])
                if digest(record) != row['fingerprint'] or file_sha(inside(self.root, record['image'])) != record['source_sha256']:
                    raise HTTPException(409, 'Image evidence changed before export')
                box = json.loads(row['box_json']) if row['box_json'] else None
                records.append({**record, 'decision': row['action'], 'box': box,
                    'candidate_targets': candidate_targets(record, box, row['action']),
                    'reviewer': row['reviewer'], 'revision': row['revision'], 'notes': row['notes'], 'policy': POLICY})
            identity = digest(records)
            folder = self.root / 'exports' / identity[:20]
            if (folder / 'READY.json').exists():
                return json.loads((folder / 'READY.json').read_text())
            folder.mkdir(parents=True, exist_ok=True)
            atomic_json(folder / 'manifest.json', records)
            # The original-image paths and evidence are retained for later
            # candidate ranking. COCO supports a full-image lead detector too.
            for split in ('train', 'val'):
                images, annotations = [], []
                for index, record in enumerate(records, 1):
                    if record['split'] != split:
                        continue
                    images.append({'id': index, 'file_name': str(inside(self.root, record['image'])),
                        'width': record['width'], 'height': record['height'], 'source': record['source'],
                        'session_id': record['session_id'], 'license_id': record['license_id'],
                        'no_lead_confirmed': record['decision'] == 'no_lead'})
                    if record['box']:
                        x1, y1, x2, y2 = record['box']
                        annotations.append({'id': index, 'image_id': index, 'category_id': 1,
                            'bbox': [x1, y1, x2-x1, y2-y1], 'area': (x2-x1)*(y2-y1), 'iscrowd': 0})
                atomic_json(folder / (split + '.coco.json'), {'images': images, 'annotations': annotations,
                            'categories': [{'id': 1, 'name': 'lead_vehicle'}]})
            atomic_json(folder / 'human-events.json', [dict(r) for r in db.execute('SELECT * FROM events ORDER BY created_at,event_id')])
            if (self.root / 'licenses').exists():
                import shutil
                shutil.copytree(self.root / 'licenses',folder / 'licenses',dirs_exist_ok=True)
            result = {'path': str(folder), 'images': len(records), 'boxes': sum(r['box'] is not None for r in records),
                      'no_lead': sum(r['decision']=='no_lead' for r in records),
                      'manifest_sha256': file_sha(folder / 'manifest.json'), 'export_id': identity,
                      'splits': dict(Counter(r['split'] for r in records)), 'training_started': False}
            atomic_json(folder / 'READY.json', result)
            return result
        finally:
            db.close()


def create_lead_router(data_root, require_session):
    store = LeadReview(data_root)
    router = APIRouter(prefix=API)

    @router.get('')
    def status(session=Depends(require_session)):
        return store.status()

    @router.get('/next')
    def next_image(queue: str = 'pending', exclude: str = '', session=Depends(require_session)):
        return store.queue(queue, session['owner'], exclude.split(',') if exclude else ())

    @router.get('/images/{image_id}')
    def detail(image_id: str, session=Depends(require_session)):
        return store.detail(image_id, session['owner'])

    @router.get('/images/{image_id}/photo')
    def photo(image_id: str, session=Depends(require_session)):
        return FileResponse(store.photo(image_id), headers={'Cache-Control': 'private, max-age=3600'})

    @router.post('/images/{image_id}/claim')
    def claim(image_id: str, body: LeadClaim, session=Depends(require_session)):
        return store.claim(image_id, body, session['owner'])

    @router.post('/labels')
    def save(body: LeadDecision, session=Depends(require_session)):
        return store.save(body, session['reviewer'], session['owner'])

    @router.post('/undo')
    def undo(body: LeadUndo, session=Depends(require_session)):
        return store.undo(body.event_id, session['reviewer'], session['owner'])

    @router.post('/export')
    def export(session=Depends(require_session)):
        return store.export()

    return router
