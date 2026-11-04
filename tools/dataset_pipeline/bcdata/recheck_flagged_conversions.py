"""Recheck the whole conversion backlog, preserving human decisions and audit gates.

Preparation is read-only. Applying a sealed plan rechecks files and current
database bindings under a write transaction. Model disagreement alone may
receive a separate human audit; it never becomes an automatic approval.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

import cv2
import numpy as np

from .bootstrap import atomic_json
from .db import Registry
from .geometry import lane_xs
from .local import local_settings
from .quality import read_gray_u8, read_rgb
from .qc import check, mask_polyline_consistency, a2d2_source_coverage, tier_for
from .source_fragment_continuation import repair_geometry, VERSION
from .source_validation import classify, a2d2_conversion_reasons, select_samples
from .teachers.base import file_sha256

POLICY = 'source-backlog-recheck-v1'
PACKAGE = Path(__file__).resolve().parent


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def dependencies():
    names = ('recheck_flagged_conversions.py', 'source_fragment_continuation.py', 'source_validation.py',
             'qc.py', 'geometry.py', 'sources/comma10k.py', 'quality.py')
    return {str(PACKAGE / name): file_sha256(PACKAGE / name) for name in names}


def load_paint(source):
    """Boundary classes only; equivalent to the full loader's lane channel."""
    params = json.loads(source['params'] or '{}')
    fmt = params.get('format')
    if fmt == 'class_map':
        classes = read_gray_u8(source['path'])
        if classes is None or classes.ndim != 2:
            raise ValueError('Invalid source class map')
        return (classes == 2).astype(np.float32), None, None
    rgb = read_rgb(source['path'])
    if rgb is None:
        raise ValueError('Undecodable source mask')
    if fmt == 'a2d2_rgb':
        # The official solid/dashed colours are exact, not colour-distance matches.
        palette = json.loads(Path(params['palette_path']).read_text(encoding='utf-8'))
        if palette.get('#ffc125') != 'Solid line' or palette.get('#8000ff') != 'Dashed line':
            raise ValueError('Audi source palette differs from the pinned paint classes')
        pixels = rgb.astype(np.uint32)
        packed = (pixels[:, :, 0] << 16) | (pixels[:, :, 1] << 8) | pixels[:, :, 2]
        solid, dashed = packed == 0xffc125, packed == 0x8000ff
        return (solid | dashed).astype(np.float32), solid, dashed
    if fmt == 'comma10k_rgb':
        from .sources.comma10k import CLASS_COLORS, LANE
        distance = np.abs(rgb.astype(np.int16) - np.asarray(CLASS_COLORS[LANE], np.int16)).sum(axis=2)
        return (distance <= 24).astype(np.float32), None, None
    raise ValueError('Unsupported source format: ' + str(fmt))


def reasons_for(geometry, paint, teacher, source, settings):
    score, issues = check(geometry, {'mode': 'source_label'}, paint, None, settings, source=source)
    reasons = classify(geometry, [issue['code'] for issue in issues], teacher)
    if source == 'a2d2':
        reasons = sorted(set(reasons + a2d2_conversion_reasons(geometry)))
    return reasons, {'score': score, 'issues': issues,
                     'tier': tier_for(score, issues, 'source_label', settings)}


def unchanged_spans(before, after):
    """All old slots and native curve samples must remain within one pixel."""
    proposed = {line['slot']: line for line in after['lanes'] if line.get('slot')}
    for line in before['lanes']:
        if not line.get('slot'):
            continue
        other = proposed.get(line['slot'])
        if not other or other['y_top'] > line['y_top'] or other['y_bottom'] < line['y_bottom']:
            return False
        ys = np.arange(line['y_top'], line['y_bottom'] + 1, dtype=float)
        if np.max(np.abs(lane_xs(line, ys) - lane_xs(other, ys)), initial=0) > 1:
            return False
    return set(proposed) == {line['slot'] for line in before['lanes'] if line.get('slot')}


def evaluate_one(payload):
    cv2.setNumThreads(1)
    record, directory = payload
    settings = local_settings(record['data_root'])
    image_id, source = record['image']['image_id'], record['image']['source']
    output = {'image_id': image_id, 'input': record}
    try:
        fingerprints = {record['geometry']['path']: record['check']['geometry_sha256'],
                        record['source']['path']: record['check']['source_sha256']}
        for key, expected in fingerprints.items():
            if file_sha256(key) != expected:
                raise ValueError('Bound source/geometry bytes changed')
        params = json.loads(record['source']['params'] or '{}')
        if params.get('human') is not True:
            raise ValueError('Source is not a human-authored semantic annotation')
        if params.get('palette_path'):
            path = params['palette_path']
            fingerprints[path] = file_sha256(path)
            if fingerprints[path] != params.get('palette_sha256'):
                raise ValueError('Source palette fingerprint changed')
        original = json.loads(Path(record['geometry']['path']).read_text())
        paint, solid, dashed = load_paint(record['source'])
        if paint.shape != (record['image']['height'], record['image']['width']):
            raise ValueError('Source image dimensions changed')
        teacher = None
        if record['teacher_path']:
            fingerprints[record['teacher_path']] = file_sha256(record['teacher_path'])
            teacher = json.loads(Path(record['teacher_path']).read_text())
        before, before_quality = reasons_for(original, paint, teacher, source, settings)
        candidate, continuation = repair_geometry(original, paint, solid=solid, dashed=dashed)
        after, quality = reasons_for(candidate, paint, teacher, source, settings) if continuation else (before, before_quality)
        accepted = False
        rejection = None
        if continuation:
            from tools.evaluate_source_v4 import crossing_checks
            old_cov, old_on = mask_polyline_consistency(original, paint)
            new_cov, new_on = mask_polyline_consistency(candidate, paint)
            cov_before, cov_after = old_cov, new_cov
            if source == 'a2d2':
                cov_before = a2d2_source_coverage(original, paint)['coverage']
                cov_after = a2d2_source_coverage(candidate, paint)['coverage']
            alignment = (new_on >= old_on - 1e-9 and (cov_before is None or cov_after is not None and cov_after >= cov_before - 1e-9))
            accepted = (unchanged_spans(original, candidate) and alignment
                        and not set(after) - set(before)
                        and len(crossing_checks(candidate)) <= len(crossing_checks(original))
                        and (set(after) < set(before) or cov_before is not None and cov_after >= cov_before + .015))
            if not accepted:
                rejection = 'extension_failed_retention_alignment_or_quality_gate'
                candidate, after, quality = original, before, before_quality
        audit_risks = []
        # A weak independent teacher may disagree with a complete human source
        # annotation. Keep this a distinct risk cohort requiring real samples.
        if after == ['model_source_count_disagreement']:
            _, on = mask_polyline_consistency(candidate, paint)
            if on >= .95:
                audit_risks = after
                after = []
        if not after and record.get('group', {}).get('state') == 'blocked' and not accepted:
            after = ['previous_audit_failed_requires_conversion_fix']
        output.update(before_reasons=before, reasons=after, quality=quality,
                      audit_risks=audit_risks, extension=continuation if accepted else [],
                      rejected_extension=rejection, fingerprints=fingerprints,
                      source_paint_pixels=int((paint >= .5).sum()))
        if record.get('human'):
            from tools.evaluate_a2d2_repair_candidate import source_supported_raster_score
            human_path = record['human']['path']
            fingerprints[human_path] = file_sha256(human_path)
            human = json.loads(Path(human_path).read_text())
            old_score = source_supported_raster_score(original, human, paint)
            new_score = source_supported_raster_score(candidate, human, paint)
            output['human_comparison'] = {'old': old_score, 'candidate': new_score}
            output['regression'] = (new_score['tp'] < old_score['tp'] or new_score['fp'] > old_score['fp'] or new_score['fn'] > old_score['fn'])
        if accepted:
            version = VERSION + ':' + digest({'input': record['check'], 'candidate': candidate})[:20]
            candidate.update(image_id=image_id, mode='source_label', version=version)
            path = Path(directory) / (image_id + '.json')
            atomic_json(path, candidate)
            output.update(candidate_path=str(path), candidate_sha256=file_sha256(path), candidate_version=version)
        return output
    except Exception as error:
        return {**output, 'error': type(error).__name__ + ': ' + str(error)}


def _latest(con, image_id, kind, producer=None):
    query = 'SELECT * FROM annotations WHERE image_id=? AND kind=?'
    args = [image_id, kind]
    if producer:
        query += ' AND producer=?'
        args.append(producer)
    row = con.execute(query + ' ORDER BY created_at DESC,id DESC LIMIT 1', args).fetchone()
    return dict(row) if row else None


def protected(con, image_id):
    row = con.execute('SELECT * FROM images WHERE image_id=?', (image_id,)).fetchone()
    if not row or row['locked'] or row['review_status'] in ('approved', 'corrected', 'rejected'):
        return True
    if any(con.execute('SELECT 1 FROM ' + table + ' WHERE image_id=? LIMIT 1', (image_id,)).fetchone()
           for table in ('review_events', 'reviews')):
        return True
    if con.execute('SELECT 1 FROM review_deferrals WHERE image_id=?', (image_id,)).fetchone():
        return True
    return bool(_latest(con, image_id, 'human') or con.execute(
        'SELECT 1 FROM review_claims WHERE image_id=? AND expires>?', (image_id, time.time())).fetchone())


def snapshot(con, check_row, settings):
    image_id = check_row['image_id']
    teacher = settings.path('lane_geometry', 'teachers', 'comma_segnet_fresh', image_id + '.json')
    q = con.execute('SELECT * FROM qc WHERE image_id=?', (image_id,)).fetchone()
    group = con.execute('SELECT * FROM source_validation_groups WHERE group_key=?', (check_row['group_key'],)).fetchone()
    return {'data_root': str(settings.root), 'group': dict(group) if group else {},
            'image': dict(con.execute('SELECT * FROM images WHERE image_id=?', (image_id,)).fetchone()),
            'check': dict(check_row), 'geometry': _latest(con, image_id, 'geometry', 'geometry'),
            'source': _latest(con, image_id, 'source_label'), 'qc': dict(q) if q else None,
            'human': _latest(con, image_id, 'human'),
            'teacher_path': str(teacher) if teacher.is_file() else None}


def prepare(settings, output, workers=4, limit=None):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / 'geometry').mkdir()
    deps = dependencies()
    con = sqlite3.connect(settings.db_path, timeout=60)
    con.row_factory = sqlite3.Row
    inputs, controls, past = [], [], defaultdict(Counter)
    try:
        for check_row in con.execute('SELECT c.* FROM source_conversion_checks c JOIN images i USING(image_id) WHERE i.status=\'curated\' ORDER BY c.image_id').fetchall():
            row = con.execute('SELECT * FROM images WHERE image_id=?', (check_row['image_id'],)).fetchone()
            old_reasons = json.loads(check_row['diagnostics']).get('reasons', [])
            for reason in old_reasons:
                past[row['source'] + ':' + reason][row['review_status']] += 1
            if check_row['state'] == 'flagged' and not protected(con, row['image_id']):
                inputs.append(snapshot(con, check_row, settings))
            elif row['locked'] and row['review_status'] in ('approved', 'corrected'):
                control = snapshot(con, check_row, settings)
                if control['human']:
                    controls.append(control)
    finally:
        con.close()
    if limit:
        inputs = inputs[:limit]
    selection = {'backlog': inputs, 'past_review_controls': controls, 'code_sha256': deps,
                 'config_sha256': digest(settings.cfg), 'created_at': time.time()}
    atomic_json(output / 'selection.json', selection)
    progress = {'phase': 'rechecking', 'pid': os.getpid(), 'total': len(inputs), 'controls': len(controls),
                'processed': 0, 'errors': 0, 'workers': workers, 'started_at': time.time()}
    atomic_json(output / 'progress.json', progress)
    results, regression = [], []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for label, records, destination in [('past_review_controls', controls, regression), ('backlog', inputs, results)]:
            progress['phase'] = label
            with (output / (label + '.jsonl')).open('w', encoding='utf-8') as handle:
                for result in pool.map(evaluate_one, ((record, str(output / 'geometry')) for record in records), chunksize=8):
                    handle.write(json.dumps(result, allow_nan=False) + '\n')
                    handle.flush()
                    destination.append(result)
                    progress.update(processed=len(results), controls_processed=len(regression),
                                    errors=sum('error' in row for row in results + regression), updated_at=time.time())
                    if (len(results) + len(regression)) % 100 == 0:
                        atomic_json(output / 'progress.json', progress)
                        print(json.dumps(progress), flush=True)
    if deps != dependencies() or selection['config_sha256'] != digest(settings.cfg):
        raise ValueError('Recheck code or configuration changed during evaluation')
    approved = [row for row in regression if row['input']['image']['review_status'] == 'approved']
    regressions = [row['image_id'] for row in approved if row.get('regression') or row.get('error')]
    errors = [row for row in results + regression if row.get('error')]
    report = {'policy': POLICY, 'converter': VERSION, 'code_sha256': deps,
              'config_sha256': selection['config_sha256'], 'selection_sha256': file_sha256(output / 'selection.json'),
              'backlog_sha256': file_sha256(output / 'backlog.jsonl'),
              'controls_sha256': file_sha256(output / 'past_review_controls.jsonl'),
              'rechecked': len(results), 'human_controls': len(regression), 'approved_controls': len(approved),
              'approved_regressions': regressions, 'errors': errors,
              'geometry_improvements': sum(bool(row.get('extension')) for row in results),
              'eligible_for_fresh_audit': sum(not row.get('reasons') and 'error' not in row for row in results),
              'audit_only_model_disagreements': sum(bool(row.get('audit_risks')) for row in results),
              'remaining_reasons': dict(Counter(reason for row in results for reason in row.get('reasons', []))),
              'past_review_outcomes': {key: dict(value) for key, value in past.items()},
              'human_decisions_created': 0, 'created_at': time.time()}
    atomic_json(output / 'report.json', report)
    progress.update(phase='prepared', report_path=str(output / 'report.json'), updated_at=time.time())
    atomic_json(output / 'progress.json', progress)
    print(json.dumps({key: value for key, value in report.items() if key not in ('past_review_outcomes', 'errors')}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    prepare(local_settings(), args.output, args.workers, args.limit)


if __name__ == '__main__':
    main()
