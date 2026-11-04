"""Evaluate native-source trace drafts without modifying active annotations."""
import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import time

import cv2

from .bootstrap import atomic_json
from .local import local_settings
from .recheck_flagged_conversions import digest, load_paint, protected, reasons_for, snapshot
from .source_run_evidence import SOFT
from .source_target_evidence import measure
from .source_tip_evidence import measure as measure_tip
from .source_shared_order_evidence import measure as measure_shared_order
from .source_trace_refinement import VERSION, refine, retains_slot_traces, retains_physical_traces
from .source_component_reconstruction import reconstruct
from .source_order_reconstruction import reconstruct as reconstruct_order, retains_source_slots, normalize_reference
from .teachers.base import file_sha256

POLICY = 'native-trace-repair-audit-v2'


def dependencies():
    package = Path(__file__).resolve().parent
    names = ('repair_trace_backlog.py', 'source_trace_refinement.py', 'source_component_reconstruction.py',
             'source_converter_a2d2_v2.py', 'source_run_evidence.py', 'source_target_evidence.py',
             'recheck_flagged_conversions.py', 'source_validation.py', 'qc.py', 'geometry.py',
             'sources/comma10k.py', 'quality.py', 'source_order_reconstruction.py', 'source_tip_evidence.py',
             'source_shared_order_evidence.py',
             '../tools/evaluate_a2d2_repair_candidate.py', '../tools/evaluate_source_v4.py',
             '../tools/evaluate_source_v3.py', 'apply_trace_repairs.py')
    return {str(package / name): file_sha256(package / name) for name in names}


def examine(payload):
    record, directory = payload
    cv2.setNumThreads(1)
    result = {'image_id': record['image']['image_id'], 'input': record, 'eligible': False}
    try:
        fingerprints = {}
        for key in ('geometry', 'source'):
            path = record[key]['path']
            fingerprints[path] = file_sha256(path)
            if fingerprints[path] != record['check'][key + '_sha256']:
                raise ValueError('Bound source or geometry changed')
        params = json.loads(record['source']['params'] or '{}')
        if params.get('human') is not True:
            raise ValueError('Original source mask is not human authored')
        if params.get('palette_path'):
            path = params['palette_path']
            fingerprints[path] = file_sha256(path)
            if fingerprints[path] != params['palette_sha256']:
                raise ValueError('Pinned palette changed')
        original = json.loads(Path(record['geometry']['path']).read_text())
        paint, solid, dashed = load_paint(record['source'])
        if paint.shape != (record['image']['height'], record['image']['width']):
            raise ValueError('Source mask dimensions changed')
        reconstructed, reconstruction = reconstruct(original, paint, solid=solid, dashed=dashed)
        candidate, refinement = refine(reconstructed, paint)
        refinement = {'reconstruction': reconstruction, 'trace': refinement} if reconstruction or refinement else None
        retain = (retains_source_slots(original, candidate, paint) if record['image']['source'] == 'a2d2'
                  else retains_slot_traces(original, candidate))
        if not retain:
            candidate, refinement = refine(original, paint)
        candidate, reference = normalize_reference(candidate, paint)
        if reference:
            refinement = {**(refinement or {}), 'reference_normalization': reference}
        settings = local_settings(record['data_root'])
        teacher = None
        if record['teacher_path']:
            fingerprints[record['teacher_path']] = file_sha256(record['teacher_path'])
            teacher = json.loads(Path(record['teacher_path']).read_text())

        def assess(draft, repair):
            evidence = measure(draft, paint)
            # Only complete native evidence can explain an unassigned fragment.
            if evidence['eligible_for_fresh_audit']:
                draft = deepcopy(draft)
                converter = draft.setdefault('source_converter', {})
                converter['warnings'] = [warning for warning in converter.get('warnings', [])
                                         if warning != 'unassigned_source_fragment_requires_review']
            reasons, quality = reasons_for(draft, paint, teacher, record['image']['source'], settings)
            tip_evidence = None
            if (record['image']['source'] == 'a2d2'
                    and 'transverse_source_promoted_to_boundary' in reasons):
                tip_evidence = measure_tip(draft, paint, evidence)
                if tip_evidence:
                    reasons = [reason for reason in reasons
                               if reason != 'transverse_source_promoted_to_boundary']
            shared_order_evidence = None
            if record['image']['source'] == 'a2d2' and 'slot_conflict' in reasons:
                shared_order_evidence = measure_shared_order(draft, paint, evidence)
                if shared_order_evidence:
                    reasons = [reason for reason in reasons if reason != 'slot_conflict']
            retain = (retains_physical_traces(original, draft, paint) if record['image']['source'] == 'a2d2'
                      else retains_slot_traces(original, draft))
            audit_risks = sorted(set(reasons) & {'model_source_count_disagreement'})
            eligible = evidence['eligible_for_fresh_audit'] and not set(reasons) - SOFT - set(audit_risks) and retain
            if record['group'].get('state') == 'blocked' and not repair:
                eligible = False
            return draft, {'eligible': eligible, 'evidence': evidence, 'refinement': repair,
                           'quality': quality, 'remaining_reasons': reasons, 'audit_risks': audit_risks,
                           'tip_evidence': tip_evidence, 'shared_order_evidence': shared_order_evidence}

        candidate, assessment = assess(candidate, refinement)
        alternative = None
        # Evaluate the new method on ALL human controls, including easy cases.
        # Studying only cases where the old method failed would leave the new
        # method's regression evidence biased by fallback selection.
        if record['image']['source'] == 'a2d2' and (record['human'] or not assessment['eligible']):
            ordered, order_evidence = reconstruct_order(original, paint, solid=solid, dashed=dashed)
            if order_evidence:
                ordered, ordered_assessment = assess(ordered, order_evidence)
                alternative = (ordered, ordered_assessment)
                if not assessment['eligible'] and ordered_assessment['eligible']:
                    candidate, assessment = ordered, ordered_assessment
        result.update(**assessment, fingerprints=fingerprints)
        if record['human']:
            from tools.evaluate_a2d2_repair_candidate import source_supported_raster_score
            path = record['human']['path']
            fingerprints[path] = file_sha256(path)
            human = json.loads(Path(path).read_text())
            old = source_supported_raster_score(original, human, paint)
            new = source_supported_raster_score(candidate, human, paint)
            result['human_comparison'] = {'old': old, 'candidate': new}
            result['regression'] = new['tp'] < old['tp'] or new['fp'] > old['fp'] or new['fn'] > old['fn']
            if alternative:
                alternate_score = source_supported_raster_score(alternative[0], human, paint)
                result['alternative_controls'] = [{**alternative[1],
                    'human_comparison': {'old': old, 'candidate': alternate_score},
                    'regression': alternate_score['tp'] < old['tp'] or alternate_score['fp'] > old['fp']
                        or alternate_score['fn'] > old['fn']}]
        if result['eligible']:
            version = POLICY + ':' + digest({'check': record['check'], 'candidate': candidate})[:20]
            candidate.update(image_id=result['image_id'], version=version, mode='source_label')
            path = Path(directory) / (result['image_id'] + '.json')
            atomic_json(path, candidate)
            result.update(candidate_path=str(path), candidate_sha256=file_sha256(path), candidate_version=version)
        return result
    except Exception as error:
        return {**result, 'eligible': False, 'error': type(error).__name__ + ': ' + str(error)}


def read_image_ids(path):
    if path is None:
        return None
    path = Path(path).resolve()
    ids = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(ids, list) or not ids
            or any(not isinstance(item, str) or not item.strip() for item in ids)
            or len(ids) != len(set(ids))):
        raise ValueError('Image IDs file must contain a nonempty list of unique image IDs')
    return {'path': str(path), 'sha256': file_sha256(path), 'image_ids': sorted(ids)}


def prepare(settings, directory, workers=6, limit=None, image_ids_file=None):
    selection_filter = read_image_ids(image_ids_file)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / 'geometry').mkdir()
    deps = dependencies()
    con = sqlite3.connect(settings.db_path, timeout=60)
    con.row_factory = sqlite3.Row
    backlog, controls = [], []
    try:
        for check in con.execute("SELECT c.* FROM source_conversion_checks c JOIN images i USING(image_id) WHERE i.status='curated' ORDER BY c.image_id").fetchall():
            reasons = json.loads(check['diagnostics']).get('reasons', [])
            image = con.execute('SELECT * FROM images WHERE image_id=?', (check['image_id'],)).fetchone()
            record = snapshot(con, check, settings)
            if image['locked'] and image['review_status'] in ('approved', 'corrected') and record['human']:
                controls.append(record)
            elif check['state'] == 'flagged' and not protected(con, image['image_id']) and record['group'].get('state') != 'blocked':
                backlog.append(record)
    finally:
        con.close()
    if selection_filter:
        selected_ids = set(selection_filter['image_ids'])
        backlog = [record for record in backlog if record['image']['image_id'] in selected_ids]
        selection_filter['unavailable_image_ids'] = sorted(selected_ids - {
            record['image']['image_id'] for record in backlog})
    if limit:
        # Stable mixed-source pilot instead of selecting only Audi by name.
        backlog.sort(key=lambda row: digest(row['image']['image_id']))
        backlog = backlog[:limit]
    selection = {'backlog': backlog, 'controls': controls, 'code_sha256': deps,
                 'config_sha256': digest(settings.cfg), 'selection_filter': selection_filter}
    atomic_json(directory / 'selection.json', selection)
    progress = {'phase': 'controls', 'pid': os.getpid(), 'total': len(backlog), 'control_total': len(controls),
                'processed': 0, 'controls_processed': 0, 'eligible': 0, 'errors': 0, 'started_at': time.time()}
    atomic_json(directory / 'progress.json', progress)
    results, control_results = [], []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for label, records, destination in [('controls', controls, control_results), ('backlog', backlog, results)]:
            progress['phase'] = label
            with (directory / (label + '.jsonl')).open('w', encoding='utf-8') as output:
                for row in pool.map(examine, ((record, str(directory / 'geometry')) for record in records), chunksize=4):
                    destination.append(row)
                    output.write(json.dumps(row, allow_nan=False) + '\n'); output.flush()
                    if (len(results) + len(control_results)) % 40 == 0:
                        progress.update(processed=len(results), controls_processed=len(control_results),
                                        eligible=sum(row['eligible'] for row in results),
                                        errors=sum('error' in row for row in results + control_results), updated_at=time.time())
                        atomic_json(directory / 'progress.json', progress)
                        print(json.dumps(progress), flush=True)
    if deps != dependencies():
        raise ValueError('Evaluation dependencies changed')
    qualifying = [row for row in control_results if row['eligible']]
    approved = [row for row in qualifying if row['input']['image']['review_status'] == 'approved']
    regressions = [row['image_id'] for row in approved if row.get('regression')]
    report = {**progress, 'phase': 'prepared', 'processed': len(results), 'controls_processed': len(control_results),
              'eligible': sum(row['eligible'] for row in results), 'errors': sum('error' in row for row in results + control_results),
              'policy': POLICY, 'code_sha256': deps, 'config_sha256': selection['config_sha256'],
              'selection_filter': selection_filter,
              'selection_sha256': file_sha256(directory / 'selection.json'),
              'backlog_sha256': file_sha256(directory / 'backlog.jsonl'),
              'controls_sha256': file_sha256(directory / 'controls.jsonl'),
              'qualifying_past_controls': len(qualifying), 'qualifying_approved_controls': len(approved),
              'approved_regressions': regressions,
              'qualifying_past_outcomes': dict(Counter(row['input']['image']['review_status'] for row in qualifying)),
              'eligible_by_source': dict(Counter(row['input']['image']['source'] for row in results if row['eligible'])),
              'geometry_refinements': sum(bool(row.get('refinement')) and row['eligible'] for row in results),
              'human_decisions_created': 0, 'active_geometry_changes': 0}
    atomic_json(directory / 'report.json', report)
    atomic_json(directory / 'progress.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generation', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--image-ids-file', type=Path,
                        help='Restrict backlog only; all actual human controls remain included')
    args = parser.parse_args()
    print(json.dumps(prepare(local_settings(), args.generation.resolve(), args.workers, args.limit,
                             args.image_ids_file)), flush=True)
