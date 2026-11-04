"""Prepare immutable source-only repairs and install them behind a fresh audit."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import numpy as np

from .bootstrap import atomic_json
from .db import Registry
from .local import local_settings
from .qc import check, tier_for
from .source_converter import VERSION, repair_geometry, source_candidate
from .source_repair_audit import requeue_repairs
from .teachers.source_labels import load_classes
from .teachers.base import file_sha256


def conversion_risk(old, new):
    before = {l['slot']: l for l in old['lanes'] if l.get('slot')}
    after = {l['slot']: l for l in new['lanes'] if l.get('slot')}
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    extent = any(abs(before[k]['y_top'] - after[k]['y_top']) > 5 or abs(before[k]['y_bottom'] - after[k]['y_bottom']) > 5 for k in set(before)&set(after))
    changed = any([p[:2] for p in before[k]['points']] != [p[:2] for p in after[k]['points']] for k in set(before)&set(after))
    if removed or (added and changed):
        risk = 'multiple'
    elif added:
        risk = 'lane_added'
    elif extent:
        risk = 'extent_changed'
    elif changed:
        risk = 'shape_changed'
    else:
        risk = 'unchanged'
    return {'risk': risk, 'added_slots': added, 'removed_slots': removed, 'changed_extent': extent,
            'converter': new.get('source_converter', {})}


def run(settings, apply=False, sample_target=64, limit=None, reuse_drafts=False):
    reg = Registry(settings.db_path)
    drafts = []
    proposals = {}
    failures = []
    output = settings.path('lane_geometry', 'source_repairs', VERSION)
    output.mkdir(parents=True, exist_ok=True)
    try:
        checks = reg.conn.execute("""SELECT c.* FROM source_conversion_checks c
             JOIN source_validation_groups g ON c.group_key=g.group_key
             JOIN images i ON c.image_id=i.image_id
             WHERE c.state='awaiting_audit' AND g.state='blocked'
             AND i.status='curated' AND i.locked=0
             AND i.review_status NOT IN ('approved','corrected','rejected')
             AND NOT EXISTS (SELECT 1 FROM review_events e WHERE e.image_id=i.image_id)
             ORDER BY c.image_id""").fetchall()
        if limit is not None:
            checks = checks[:limit]
        for index, item in enumerate(checks):
            old = dict(item)
            image_id = old['image_id']
            try:
                source = reg.annotation(image_id, 'source_label')
                if source['id'] != old['source_id'] or file_sha256(source['path']) != old['source_sha256']:
                    raise ValueError('Source fingerprint changed')
                if file_sha256(old['geometry_path']) != old['geometry_sha256']:
                    raise ValueError('Geometry fingerprint changed')
                geo = json.loads(Path(old['geometry_path']).read_text())
                version = VERSION + ':' + hashlib.sha256((old['geometry_sha256'] + old['source_sha256']).encode()).hexdigest()[:20]
                path = output / (image_id + '-' + version.rsplit(':',1)[1] + '.json')
                lane_prob = None
                if reuse_drafts and path.exists():
                    repaired = json.loads(path.read_text())
                    if repaired.get('version') != version or repaired.get('image_id') != image_id:
                        raise ValueError('Cached draft identity/version mismatch')
                else:
                    classes = load_classes(source)
                    if classes.shape != (geo['height'], geo['width']):
                        raise ValueError('Source mask dimensions changed')
                    lane_prob = (classes == 2).astype(np.float32)
                    repaired = repair_geometry(geo, lane_prob)
                if repaired.get('source_converter', {}).get('short_recovery_candidates'):
                    proposals[image_id] = source_candidate(geo, lane_prob, old['source_sha256'], old['geometry_sha256'], repaired=repaired)
                repaired.update(image_id=image_id, mode='source_label')
                repaired['version'] = version
                diagnostics = conversion_risk(geo, repaired)
                # No human/model data enters repair geometry. Preserve any
                # unrelated user metadata in immutable human files, not here.
                repaired['conversion_repair'] = diagnostics
                if path.exists():
                    if json.loads(path.read_text()) != repaired:
                        raise ValueError('Immutable repair draft conflicts with an existing version')
                else:
                    atomic_json(path, repaired)
                old_qc = reg.conn.execute('SELECT * FROM qc WHERE image_id=?',(image_id,)).fetchone()
                same_geometry = all(geo.get(key) == repaired.get(key) for key in ('lanes','width','height','horizon_y','valid_bottom','max_run_frac'))
                if same_geometry and old_qc and old_qc['geometry_version'] == old['geometry_version']:
                    score, issues = old_qc['score'], json.loads(old_qc['issues'] or '[]')
                else:
                    if lane_prob is None:
                        lane_prob = (load_classes(source) == 2).astype(np.float32)
                    score, issues = check(repaired, {'mode':'source_label'}, lane_prob, None, settings)
                # Any ambiguous recovery stays an individual task. A new audit
                # may qualify a coherent cohort, but must not silently overlook
                # already-visible shape or slot conflicts.
                if diagnostics['removed_slots']:
                    issues.append({'code':'repair_removed_boundary'})
                for warning in repaired.get('source_converter', {}).get('warnings', []):
                    issues.append({'code':'repair_' + warning})
                tier = tier_for(score, issues, 'source_label', settings)
                drafts.append({'image_id':image_id, 'previous_check':old,
                               'geometry_path':str(path), 'geometry_version':version, 'geometry_sha256':file_sha256(path),
                               'diagnostics':diagnostics,
                               'qc':{'score':score, 'tier':tier, 'priority':1-score,
                                     'issues':issues, 'qc_sample':False}})
            except (OSError, ValueError, KeyError) as exc:
                failures.append({'image_id':image_id,'error':str(exc)})
            if (index+1) % 250 == 0:
                print(json.dumps({'phase':'preparing_repairs','processed':index+1,'total':len(checks),'errors':len(failures)}),flush=True)
    finally:
        reg.close()
    report = requeue_repairs(settings, drafts, sample_target=sample_target, apply=apply)
    proposal_paths = {}
    for image_id, proposal in proposals.items():
        proposal_path = output / 'proposals' / (image_id + '.json')
        atomic_json(proposal_path, proposal)
        proposal_paths[image_id] = str(proposal_path)
    plan_path = settings.path('reports','source_conversion_repairs.plan.json')
    atomic_json(plan_path, {'converter':VERSION, 'converter_sha256':file_sha256(Path(__file__).with_name('source_converter.py')),
                           'repairs':drafts, 'proposal_paths':proposal_paths,
                           'proposal_sha256':{k:file_sha256(v) for k,v in proposal_paths.items()}, 'sample_target':sample_target})
    if apply:
        for image_id in report['applied_ids']:
            if image_id in proposals:
                atomic_json(settings.path('lane_geometry','teachers','source_refined',image_id+'.json'), proposals[image_id])
    report.update(converter=VERSION, preparation_errors=failures,
                  risks=dict(Counter(d['diagnostics']['risk'] for d in drafts)),
                  plan_path=str(plan_path),
                  source_fragment_proposals=sum(image_id in proposals for image_id in report['applied_ids']) if apply else len(proposals))
    atomic_json(settings.path('reports','source_conversion_repairs.json'),report)
    return report


def apply_plan(settings, path):
    plan = json.loads(Path(path).read_text())
    if plan.get('converter') != VERSION or plan.get('converter_sha256') != file_sha256(Path(__file__).with_name('source_converter.py')):
        raise ValueError('Converter changed after plan preparation; prepare a fresh plan')
    for repair in plan['repairs']:
        if not repair.get('geometry_sha256') or file_sha256(repair['geometry_path']) != repair['geometry_sha256']:
            raise ValueError('Planned geometry draft changed: ' + repair['image_id'])
    for image_id, proposal in plan.get('proposal_paths',{}).items():
        if file_sha256(proposal) != plan.get('proposal_sha256',{}).get(image_id):
            raise ValueError('Planned source proposal changed: ' + image_id)
    report = requeue_repairs(settings, plan['repairs'], sample_target=plan['sample_target'], apply=True)
    for image_id in report['applied_ids']:
        proposal = plan.get('proposal_paths', {}).get(image_id)
        if proposal:
            atomic_json(settings.path('lane_geometry','teachers','source_refined',image_id+'.json'),json.loads(Path(proposal).read_text()))
    report.update(converter=VERSION, preparation_errors=[],plan_path=str(path),
                  risks=dict(Counter(d['diagnostics']['risk'] for d in plan['repairs'])),
                  source_fragment_proposals=sum(image_id in plan.get('proposal_paths',{}) for image_id in report['applied_ids']))
    atomic_json(settings.path('reports','source_conversion_repairs.json'),report)
    return report


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--sample-size',type=int,default=64)
    parser.add_argument('--limit',type=int)
    parser.add_argument('--plan-file',type=Path)
    parser.add_argument('--reuse-drafts',action='store_true')
    args=parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error('Limit must be positive')
    if args.plan_file and not args.apply:
        parser.error('--plan-file requires --apply')
    report=apply_plan(local_settings(),args.plan_file) if args.plan_file else run(local_settings(),args.apply,args.sample_size,args.limit,args.reuse_drafts)
    print(json.dumps({k:v for k,v in report.items() if k not in ('applied_ids','sample_ids','skipped','original_groups')}))


if __name__=='__main__': main()
