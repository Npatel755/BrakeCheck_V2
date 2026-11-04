"""Recheck held source conversions and audit cause-specific V3 replacements."""
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .bootstrap import atomic_json
from .db import Registry
from .local import local_settings
from .qc import check, tier_for
from .repair_source_conversions import conversion_risk
from .source_boundary_maps import load_boundary_maps
from .source_converter import source_candidate
from .source_converter_v3 import VERSION, repair_geometry_v3, risk_diagnostics
from .source_repair_audit import requeue_repairs
from .teachers.base import file_sha256


def verify_regression_report(path):
    report = json.loads(Path(path).read_text())
    code = Path(__file__).with_name('source_converter_v3.py')
    checks = report.get('safety_checks',{})
    if report.get('converter_code_sha256') != file_sha256(code):
        raise ValueError('Regression report belongs to a different converter')
    if checks.get('approved_sample_regressions') != 0 or not checks.get('original_annotations_unchanged'):
        raise ValueError('Regression evidence has unresolved annotation regressions')
    if not checks.get('converter_and_dependencies_stable') or not checks.get('source_fingerprints_verified'):
        raise ValueError('Regression evidence was not verified')
    for dependency,expected in report.get('dependency_sha256',{}).items():
        if file_sha256(dependency) != expected:
            raise ValueError('Converter dependency changed after regression checks')
    return report


def prepare(settings, regression_report, limit=None):
    verify_regression_report(regression_report)
    code = Path(__file__).with_name('source_converter_v3.py').resolve()
    evidence = {'converter_code_path':str(code), 'converter_code_sha256':file_sha256(code),
                'regression_report_path':str(Path(regression_report).resolve()),
                'regression_report_sha256':file_sha256(regression_report)}
    output = settings.path('lane_geometry','source_repairs',VERSION)
    output.mkdir(parents=True,exist_ok=True)
    reg = Registry(settings.db_path)
    drafts, proposal_paths, errors = [], {}, []
    try:
        rows = reg.conn.execute("""SELECT c.* FROM source_conversion_checks c
            JOIN source_validation_groups g USING(group_key) JOIN images i ON c.image_id=i.image_id
            WHERE c.state='awaiting_audit' AND g.state='blocked' AND i.status='curated' AND i.locked=0
            AND i.review_status NOT IN ('approved','corrected','rejected')
            AND NOT EXISTS(SELECT 1 FROM review_events e WHERE e.image_id=c.image_id)
            ORDER BY c.image_id""").fetchall()
        if limit is not None:
            rows = rows[:limit]
        for index, row in enumerate(rows):
            previous = dict(row)
            image_id = previous['image_id']
            try:
                source = reg.annotation(image_id,'source_label')
                if source['id'] != previous['source_id'] or file_sha256(source['path']) != previous['source_sha256']:
                    raise ValueError('Source changed after audit')
                if file_sha256(previous['geometry_path']) != previous['geometry_sha256']:
                    raise ValueError('Geometry changed after audit')
                original = json.loads(Path(previous['geometry_path']).read_text())
                maps = load_boundary_maps(source)
                if maps['lane'].shape != (original['height'],original['width']):
                    raise ValueError('Source dimensions changed')
                risk = risk_diagnostics(original)
                if risk['risky']:
                    geometry = repair_geometry_v3(original, maps['lane'], maps['my_car'], maps['movable'])
                else:
                    geometry = deepcopy(original)
                    geometry['source_converter'] = {**geometry.get('source_converter',{}), 'version':VERSION,
                        'raw_reextracted':False, 'original_risk':risk, 'source_fingerprints_rechecked':True}
                geometry['image_id'] = image_id
                geometry['mode'] = 'source_label'
                version_hash = hashlib.sha256((previous['geometry_sha256'] + previous['source_sha256']
                                               + evidence['converter_code_sha256']).encode()).hexdigest()[:20]
                geometry['version'] = VERSION + ':' + version_hash
                diagnostics = conversion_risk(original,geometry)
                diagnostics.update(risk_family='reassociated_dashes' if risk['risky'] else
                                   'supported_curve' if risk.get('curved_long_gaps') else 'clean_source',
                                   original_risk=risk, converter_evidence=evidence)
                geometry['conversion_repair'] = diagnostics
                score, issues = check(geometry,{'mode':'source_label'},maps['lane'],None,settings)
                residual = risk_diagnostics(geometry)
                if risk['risky'] and residual['risky']:
                    issues.append({'code':'v3_unresolved_dash_association','detail':residual})
                for warning in geometry.get('source_converter',{}).get('warnings',[]):
                    issues.append({'code':'repair_'+warning})
                path = output / (image_id+'-'+version_hash+'.json')
                if path.exists() and json.loads(path.read_text()) != geometry:
                    raise ValueError('Immutable V3 draft conflict')
                if not path.exists():
                    atomic_json(path,geometry)
                geometry_sha = file_sha256(path)
                drafts.append({'image_id':image_id,'previous_check':previous,'geometry_path':str(path),
                               'geometry_version':geometry['version'],'geometry_sha256':geometry_sha,
                               'diagnostics':diagnostics,
                               'qc':{'score':score,'tier':tier_for(score,issues,'source_label',settings),
                                     'priority':1-score,'issues':issues,'qc_sample':False}})
                if risk['risky']:
                    candidate = source_candidate(geometry,maps['lane'],previous['source_sha256'],geometry_sha)
                    if candidate.get('source_converter',{}).get('recovered_short_chains'):
                        candidate_path = output/'proposals'/(image_id+'-'+version_hash+'.json')
                        atomic_json(candidate_path,candidate)
                        proposal_paths[image_id] = str(candidate_path)
            except (OSError,ValueError,KeyError) as error:
                errors.append({'image_id':image_id,'error':str(error)})
            if (index+1)%250 == 0:
                print(json.dumps({'phase':'rechecking_source_conversions','processed':index+1,
                                  'total':len(rows),'errors':len(errors)}),flush=True)
    finally:
        reg.close()
    report = requeue_repairs(settings,drafts,apply=False)
    plan = {'converter':VERSION,'converter_evidence':evidence,'repairs':drafts,'proposal_paths':proposal_paths,
            'proposal_sha256':{key:file_sha256(path) for key,path in proposal_paths.items()},'errors':errors}
    plan_path = settings.path('reports','source_v3_repairs.plan.json')
    atomic_json(plan_path,plan)
    report.update(converter=VERSION,plan_path=str(plan_path),errors=errors,
                  risk_families=dict(Counter(d['diagnostics']['risk_family'] for d in drafts)),
                  geometry_changed=sum(d['diagnostics']['risk']!='unchanged' for d in drafts),
                  optional_proposals=len(proposal_paths))
    atomic_json(settings.path('reports','source_v3_repairs.json'),report)
    return report


def apply_plan(settings,path):
    plan = json.loads(Path(path).read_text())
    if plan['converter'] != VERSION or plan['errors']:
        raise ValueError('Prepared V3 plan has errors or a different converter')
    verify_regression_report(plan['converter_evidence']['regression_report_path'])
    for draft in plan['repairs']:
        if file_sha256(draft['geometry_path']) != draft['geometry_sha256']:
            raise ValueError('Prepared V3 geometry changed')
    for image_id,proposal in plan['proposal_paths'].items():
        if file_sha256(proposal) != plan['proposal_sha256'][image_id]:
            raise ValueError('Prepared V3 proposal changed')
    report = requeue_repairs(settings,plan['repairs'],apply=True)
    for image_id in report['applied_ids']:
        if image_id in plan['proposal_paths']:
            atomic_json(settings.path('lane_geometry','teachers','source_refined',image_id+'.json'),
                        json.loads(Path(plan['proposal_paths'][image_id]).read_text()))
    report.update(converter=VERSION,plan_path=str(path),errors=[],
                  risk_families=dict(Counter(d['diagnostics']['risk_family'] for d in plan['repairs'])),
                  geometry_changed=sum(d['diagnostics']['risk']!='unchanged' for d in plan['repairs']),
                  optional_proposals=sum(image_id in plan['proposal_paths'] for image_id in report['applied_ids']))
    atomic_json(settings.path('reports','source_v3_repairs.json'),report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--regression-report',type=Path)
    parser.add_argument('--plan-file',type=Path)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--limit',type=int)
    args=parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error('Limit must be positive')
    if args.apply:
        if not args.plan_file:
            parser.error('--apply requires --plan-file')
        report=apply_plan(local_settings(),args.plan_file)
    else:
        if not args.regression_report:
            parser.error('Preparation requires --regression-report')
        report=prepare(local_settings(),args.regression_report,args.limit)
    print(json.dumps({key:value for key,value in report.items() if key not in ('applied_ids','sample_ids','skipped','original_groups')}))


if __name__=='__main__':main()
