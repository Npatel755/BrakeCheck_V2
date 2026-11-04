"""Independently evaluate source-only Audi repair drafts; never write dataset state.

Development cases, untouched evaluation cases, and approved audit regressions
stay separate. Human polylines are read only after conversion, and unsupported
human extensions are never required as source-only converter targets.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import numpy as np

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_ROOT))
from bcdata.config import load_settings
from bcdata.lanes import params_for
from bcdata.qc import check, tier_for
from bcdata.source_boundary_maps import load_boundary_maps
from bcdata.teachers.source_labels import load_classes
from tools.evaluate_source_repair import score
from tools.evaluate_source_v3 import observed_extents, slotted, support
from tools.evaluate_source_v4 import (
    canonical_sha256, crossing_checks, duplicate_slots, geometry_identity,
    paint_rows, paint_supported_identity, source_boundary_retention, source_fragment_overlap,
    unsupported_inward_promotions,
)

KNOWN_FAILURES = {
    'a2d2-8843a58c874945fd': {'cause':'continuous_curve_off_source', 'slots':[1,2],
                              'mechanisms':['curve','off_mask','uncovered','support']},
    'a2d2-af66b0c926db2957': {'cause':'missing_observed_dash_extent',
                              'mechanisms':['continuation','extent','uncovered','fragment','support']},
    'a2d2-85cc2b7cbe649894': {'cause':'one_divider_split_across_three_slots', 'slots':[2],
                              'mechanisms':['slot','association','join','duplicate','fragment']},
    'a2d2-107e088058bcb0a0': {'cause':'patch_gap_splits_boundary_identity', 'slots':[1,2,3],
                              'mechanisms':['slot','association','join','continuation','fragment','uncovered']},
}
SUBSETS = ('development','untouched_evaluation','approved_regression','failed_audit_cases')


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def invoke_source_only(converter, original, maps, params):
    """Pass only source inputs and fail if the converter mutates any of them."""
    original_sha = canonical_sha256(original)
    map_sha = {name:hashlib.sha256(value.tobytes()).hexdigest() for name,value in maps.items()}
    candidate = converter(original, maps['lane'], maps.get('my_car'), maps.get('movable'),
                          params=params, solid_mask=maps.get('solid'), dashed_mask=maps.get('dashed'))
    if canonical_sha256(original) != original_sha or any(
            hashlib.sha256(value.tobytes()).hexdigest() != map_sha[name] for name,value in maps.items()):
        raise ValueError('Converter mutated its source inputs')
    if (candidate.get('height'),candidate.get('width')) != maps['lane'].shape:
        raise ValueError('Converter changed source coordinate dimensions')
    return candidate


def approved_regression_reasons(record):
    """Keep same-slot geometry and source-supported extent, not just counts."""
    reasons=[]
    if record['candidate_duplicate_slots']:
        reasons.append('duplicate_lane_slots')
    old,new = record['raster_scores']['old'],record['raster_scores']['candidate']
    if new['tp'] < old['tp'] or new['fp'] > old['fp'] or new['fn'] > old['fn']:
        reasons.append('same_slot_human_raster_regressed')
    identity=record['paint_supported_identity']
    for slot,before in identity['old'].items():
        after=identity['candidate'].get(slot,{})
        if (before['sufficient_source_rows']
                and before['matched_rows_within_12px'] - after.get('matched_rows_within_12px',0) >= 5
                and (before['source_supported_row_recall'] or 0) - (after.get('source_supported_row_recall') or 0) > .02):
            reasons.append('source_supported_same_slot_extent_regressed_'+slot)
    if len(record['crossings']['candidate']) > len(record['crossings']['old']):
        reasons.append('additional_adjacent_slot_crossing')
    return reasons


def source_supported_raster_score(candidate,human,paint):
    """Same-slot 20px IoU restricted to source-supported human rows.

    Full human-raster IoU is retained separately as a diagnostic. Unsupported
    hand-drawn extensions and wholly unsupported human slots cannot become
    mandatory converter targets. New slots absent from the approved human
    geometry still count as extra predictions, rather than disappearing.
    """
    import cv2
    h,w=paint.shape
    targets=slotted(human);proposals=slotted(candidate)
    supported={};ignored=[]
    for slot,lane in targets.items():
        _,rows=paint_rows(lane,paint)
        if int(rows.sum())>=4:
            supported[slot]=rows
        else:
            ignored.append(slot)
    matches=0
    for slot,rows in supported.items():
        if slot not in proposals:
            continue
        masks=[]
        for lane in (targets[slot],proposals[slot]):
            canvas=np.zeros((h,w),np.uint8)
            pts=np.round(np.asarray(lane['points'],float)[:,:2]).astype(np.int32)
            if len(pts)>1:
                cv2.polylines(canvas,[pts],False,1,20)
            canvas[~rows]=0
            masks.append(canvas.astype(bool))
        matches+=int((masks[0]&masks[1]).sum()/max(1,(masks[0]|masks[1]).sum())>=.5)
    predicted=sum(slot not in ignored for slot in proposals)
    return {'tp':matches,'fp':predicted-matches,'fn':len(supported)-matches,
            'count_match':predicted==len(supported),
            'source_supported_human_slots':sorted(supported),'ignored_unsupported_human_slots':sorted(ignored)}


def promotion_evidence(original,candidate,paint):
    """Require simultaneous distinct paint before moving an old inner line out.

    Candidate traces are only probes for source pixels; their geometry alone
    cannot establish the new boundary. Existing helper thresholds demand both
    lateral separation and simultaneous source-supported native rows.
    """
    augmented=deepcopy(original)
    for slot,lane in slotted(candidate).items():
        if slot in (2,3):
            fragment=deepcopy(lane);fragment['slot']=None
            augmented['lanes'].append(fragment)
    return source_fragment_overlap(augmented,paint)


def known_failure_check(record):
    specification=KNOWN_FAILURES[record['image_id']]
    failures=[]
    if record['candidate_duplicate_slots']:
        failures.append('duplicate_slots')
    if record['crossings']['candidate']:
        failures.append('adjacent_slot_crossing')
    if specification.get('slots') and record['candidate_slots'] != specification['slots']:
        failures.append('source_boundary_slot_identities_not_recovered')
    fraction=record['source_paint_support']['candidate']['fraction']
    if fraction is None or fraction < .95:
        failures.append('observed_centres_not_supported_by_source_paint')
    qc_codes={issue['code'] for issue in record['qc']['candidate']['issues']}
    if qc_codes - {'no_lanes'}:
        failures.append('source_completeness_or_geometry_qc_remains')
    if record['unsupported_inward_promotions']:
        failures.append('unsupported_inward_slot_promotion')
    warnings=record['warnings'] + sorted(qc_codes)
    mechanism=[warning for warning in warnings if any(token in warning for token in specification['mechanisms'])]
    return {'image_id':record['image_id'],'cause':specification['cause'],
            'status':'passed' if not failures else 'flagged' if mechanism else 'unresolved',
            'criteria_failures':failures,'flagged':bool(failures and mechanism),
            'warnings':mechanism,'human_geometry_used_as_converter_input':False}


def _dependencies():
    paths=[PIPELINE_ROOT/'bcdata'/name for name in (
        'source_converter_a2d2.py','source_converter.py','source_converter_v3.py','source_converter_v4.py',
        'geometry.py','source_boundary_maps.py','teachers/source_labels.py','sources/a2d2.py',
        'qc.py','quality.py','config.py','lanes.py','sources/comma10k.py','teachers/base.py')]
    paths += [PIPELINE_ROOT/'tools'/name for name in (
        'evaluate_source_v4.py','evaluate_source_v3.py','evaluate_source_repair.py')]
    paths += [PIPELINE_ROOT/'config/pipeline.yaml']
    return {str(path.resolve()):sha256(path) for path in paths}


def evaluate(data_root:Path, selection_root:Path, output:Path, subsets=SUBSETS, converter_module='source_converter_a2d2'):
    import importlib
    if converter_module not in ('source_converter_a2d2', 'source_converter_a2d2_v2'):
        raise ValueError('Unknown Audi source converter')
    converter = importlib.import_module('bcdata.' + converter_module)
    VERSION, repair_geometry = converter.VERSION, converter.repair_geometry
    output=output.resolve();data_root=data_root.resolve();selection_root=selection_root.resolve()
    if not output.is_relative_to(data_root/'reports'):
        raise ValueError('Evaluation output must stay under the dataset reports directory')
    if (output/'report.json').exists():
        raise ValueError('Refusing to overwrite a frozen evaluation report')
    output.mkdir(parents=True,exist_ok=True)
    (output/'geometry').mkdir(exist_ok=True)
    dependencies=_dependencies(); evaluator_sha=sha256(__file__)
    dependencies[str(Path(converter.__file__).resolve())] = sha256(converter.__file__)
    settings=load_settings(overrides={'data_root':str(data_root)})
    params=params_for(settings)
    selections={name:selection_root/(name+'.json') for name in subsets}
    selection_sha={str(path):sha256(path) for path in selections.values()}
    if set(subsets) & {'approved_regression','failed_audit_cases'}:
        references=selection_root/'human_qa_reference.json'
        selection_sha[str(references)]=sha256(references)
    cases={};buckets={}
    for name,path in selections.items():
        buckets[name]=json.loads(path.read_text())['cases']
        for row in buckets[name]:
            if row['image_id'] in cases:
                raise ValueError('Diagnostic subsets overlap: '+row['image_id'])
            cases[row['image_id']]={**row,'subset':name}
    if 'development' in subsets and any(row['split']!='train' or row['human_history'] for row in buckets['development']):
        raise ValueError('Development sample is not untouched training data')
    if 'untouched_evaluation' in subsets and any(row['split'] not in ('val','test') or row['human_history'] for row in buckets['untouched_evaluation']):
        raise ValueError('Held evaluation contains training/development or reviewed data')
    con=sqlite3.connect((data_root/'metadata.sqlite').as_uri()+'?mode=ro',uri=True)
    con.row_factory=sqlite3.Row;con.execute('PRAGMA query_only=ON');con.execute('BEGIN')
    fingerprints={};records=[];audit_controls=[]
    try:
        for index,(image_id,item) in enumerate(sorted(cases.items())):
            image=con.execute('SELECT * FROM images WHERE image_id=?',(image_id,)).fetchone()
            binding=con.execute('SELECT * FROM source_conversion_checks WHERE image_id=?',(image_id,)).fetchone()
            if image is None or image['source']!='a2d2' or binding is None:
                raise ValueError('Missing Audi binding: '+image_id)
            geometry_ann=con.execute("SELECT * FROM annotations WHERE image_id=? AND kind='geometry' AND producer='geometry' ORDER BY created_at DESC,id DESC LIMIT 1",(image_id,)).fetchone()
            source=con.execute("SELECT * FROM annotations WHERE image_id=? AND kind='source_label' ORDER BY created_at DESC,id DESC LIMIT 1",(image_id,)).fetchone()
            if (not geometry_ann or not source or binding['geometry_id']!=geometry_ann['id']
                    or binding['source_id']!=source['id'] or geometry_ann['path']!=item['geometry_path']
                    or source['path']!=item['source_path']):
                raise ValueError('Source geometry binding changed: '+image_id)
            for kind in ('source','geometry','image'):
                path=Path(item[kind+'_path']);expected=item[kind+'_sha256']
                if sha256(path)!=expected:
                    raise ValueError('Frozen input fingerprint changed: '+str(path))
                fingerprints[str(path)]=expected
            original=json.loads(Path(item['geometry_path']).read_text())
            maps=load_boundary_maps(source);classes=load_classes(source)
            if not all(np.array_equal(maps[name]>=.5,classes==cls) for name,cls in (('lane',2),('my_car',5),('movable',4))):
                raise ValueError('Boundary loader disagrees with source taxonomy: '+image_id)
            if not {'solid','dashed'} <= set(maps) or not np.array_equal(
                    (maps['solid']>=.5)|(maps['dashed']>=.5),maps['lane']>=.5):
                raise ValueError('Audi marking types do not partition source lane paint: '+image_id)
            for name in ('palette_path','calibration_path'):
                source_params=json.loads(source['params'] or '{}')
                path=source_params.get(name)
                if path:
                    digest=sha256(path)
                    expected=source_params.get(name.replace('_path','_sha256'))
                    if expected and digest!=expected:
                        raise ValueError('Source metadata fingerprint changed: '+path)
                    fingerprints[path]=digest
            candidate=invoke_source_only(repair_geometry,original,maps,params)
            candidate['image_id']=image_id
            path=output/'geometry'/(image_id+'.json');path.write_text(json.dumps(candidate,indent=1,sort_keys=True))
            # Human labels enter only this post-conversion diagnostic section.
            human_ann=con.execute("SELECT * FROM annotations WHERE image_id=? AND kind='human' ORDER BY created_at DESC,id DESC LIMIT 1",(image_id,)).fetchone()
            human=None
            if human_ann:
                fingerprints[human_ann['path']]=sha256(human_ann['path'])
                human=json.loads(Path(human_ann['path']).read_text())
            if item['subset']=='approved_regression' and (image['review_status']!='approved' or not image['locked'] or human is None):
                raise ValueError('Approved regression review changed: '+image_id)
            if item['subset'] in ('development','untouched_evaluation') and (
                    image['locked'] or con.execute('SELECT 1 FROM review_events WHERE image_id=? LIMIT 1',(image_id,)).fetchone()):
                raise ValueError('Untouched diagnostic input acquired a human review: '+image_id)
            paint=maps['lane'];old_qc=check(original,{'mode':'source_label'},paint,None,settings,source='a2d2');new_qc=check(candidate,{'mode':'source_label'},paint,None,settings,source='a2d2')
            record={'image_id':image_id,'subset':item['subset'],'split':image['split'],'profile':item['profile'],
                    'decision':image['review_status'],'old_slots':sorted(slotted(original)),
                    'candidate_slots':sorted(slotted(candidate)),'v4_slots':sorted(slotted(candidate)),
                    'geometry_changed':geometry_identity(original)!=geometry_identity(candidate),
                    'candidate_duplicate_slots':duplicate_slots(candidate),
                    'source_paint_support':{'old':support(original,paint),'candidate':support(candidate,paint)},
                    'source_boundary_retention':source_boundary_retention(original,candidate,paint),
                    'source_fragment_overlap':promotion_evidence(original,candidate,paint),
                    'observed_extents':observed_extents(original,candidate),
                    'crossings':{'old':crossing_checks(original),'candidate':crossing_checks(candidate)},
                    'warnings':list(candidate.get('source_converter',{}).get('warnings',[])),
                    'source_converter':candidate.get('source_converter',{}),
                    'qc':{'old':{'score':old_qc[0],'issues':old_qc[1]},
                          'candidate':{'score':new_qc[0],'issues':new_qc[1],'tier':tier_for(new_qc[0],new_qc[1],'source_label',settings)}},
                    'candidate_path':str(path),'candidate_sha256':sha256(path),
                    'fingerprints':{kind+'_path':item[kind+'_path'] for kind in ('image','source','geometry')}
                        | {kind+'_sha256':item[kind+'_sha256'] for kind in ('image','source','geometry')},
                    'raster_scores':{'old':source_supported_raster_score(original,human,paint),
                                     'candidate':source_supported_raster_score(candidate,human,paint)} if human else None,
                    'full_human_raster_scores':{'old':score(original,human),'candidate':score(candidate,human)} if human else None,
                    'paint_supported_identity':{'old':paint_supported_identity(original,human,paint),
                                                'candidate':paint_supported_identity(candidate,human,paint)} if human else None}
            if human_ann:
                record['human_path']=human_ann['path']
                record['human_sha256']=fingerprints[human_ann['path']]
                record['human_annotation_id']=human_ann['id']
            record['unsupported_inward_promotions']=unsupported_inward_promotions(record)
            record['approved_regression_reasons']=approved_regression_reasons(record) if item['subset']=='approved_regression' else []
            records.append(record)
            if item['subset']=='failed_audit_cases':
                notes=[dict(row) for row in con.execute('SELECT decision,notes FROM reviews WHERE image_id=? ORDER BY created_at,id',(image_id,))]
                audit_controls.append({'image_id':image_id,'status':'human_decision_preserved',
                    'actual_decision':image['review_status'],'review_notes':notes,
                    'human_geometry_used_as_converter_input':False,
                    'source_supported_identity':record['paint_supported_identity'],
                    'unsupported_human_spans_are_not_targets':True,
                    'meaning':'Locked human decision is not overridden; missing source mask paint is not invented.'})
            if (index+1)%20==0:
                print(json.dumps({'processed':index+1,'total':len(cases)}),flush=True)
    finally:
        con.rollback();con.close()
    stable=dependencies=={**_dependencies(), str(Path(converter.__file__).resolve()):sha256(converter.__file__)} and sha256(__file__)==evaluator_sha
    if not stable or any(sha256(path)!=digest for path,digest in selection_sha.items()):
        raise ValueError('Converter, dependencies, evaluator or selection changed during evaluation')
    if any(sha256(path)!=digest for path,digest in fingerprints.items()):
        raise ValueError('An immutable source/image/geometry/human input changed during evaluation')
    regressions=[{'image_id':r['image_id'],'reasons':r['approved_regression_reasons']} for r in records if r['approved_regression_reasons']]
    known=[known_failure_check(record) for record in records if record['image_id'] in KNOWN_FAILURES]
    if 'development' in subsets and len(known)!=len(KNOWN_FAILURES):
        raise ValueError('A required known source failure is absent from the development sample')
    unresolved=[row for row in known if row['status']!='passed']
    promotions=[item for record in records for item in record['unsupported_inward_promotions']]
    summaries={}
    for name in subsets:
        rows=[record for record in records if record['subset']==name]
        summaries[name]={'images':len(rows),'changed':sum(row['geometry_changed'] for row in rows),
            'old_slots':sum(len(row['old_slots']) for row in rows),'candidate_slots':sum(len(row['candidate_slots']) for row in rows),
            'old_qc_issues':dict(Counter(issue['code'] for row in rows for issue in row['qc']['old']['issues'])),
            'candidate_qc_issues':dict(Counter(issue['code'] for row in rows for issue in row['qc']['candidate']['issues'])),
            'old_crossings':sum(bool(row['crossings']['old']) for row in rows),
            'candidate_crossings':sum(bool(row['crossings']['candidate']) for row in rows)}
    code=Path(converter.__file__)
    report={'schema':'a2d2_source_repair_evaluation:v1','created_at':datetime.now(timezone.utc).isoformat(),
        'converter_version':VERSION,'converter_code_path':str(code.resolve()),'converter_code_sha256':sha256(code),
        'dependency_sha256':dependencies,'evaluator_code_path':str(Path(__file__).resolve()),'evaluator_code_sha256':evaluator_sha,
        'selection_sha256':selection_sha,'input_fingerprints':fingerprints,
        'limitation':'Source-conversion diagnostics and approved audit regression controls; not official UFLDv2 accuracy or human approval.',
        'raster_metric':'Same-slot 20px raster IoU >=0.5 over source-supported human rows only; full human-raster scores are diagnostic and unsupported extensions are not required.',
        'held_out_policy':'Frozen untouched val/test subset is inspected only after the candidate code is frozen; never a converter input.',
        'safety_checks':{'database_opened_read_only':True,'database_writes':False,
            'approved_sample_regressions':len(regressions),'original_annotations_unchanged':True,
            'converter_and_dependencies_stable':True,'source_fingerprints_verified':True,
            'known_failures_resolved_or_flagged':all(row['status'] in ('passed','flagged') for row in known),
            'no_unsupported_inward_slot_promotions':not promotions,
            'candidate_slot_uniqueness_verified':not any(row['candidate_duplicate_slots'] for row in records)},
        'subsets_evaluated':list(subsets),
        'summaries':summaries,'approved_regressions':regressions,'known_failure_checks':known,
        'unresolved_known_failures':unresolved,'human_audit_qa_controls':audit_controls,
        'unsupported_inward_promotions':promotions,'records':records}
    report['safety_checks'].update(
        complete_required_evaluation=set(SUBSETS)<=set(subsets),
        approved_samples_evaluated=sum(row['subset']=='approved_regression' for row in records),
        untouched_samples_evaluated=sum(row['subset']=='untouched_evaluation' for row in records))
    (output/'report.json').write_text(json.dumps(report,indent=2,sort_keys=True))
    print(json.dumps({'output':str(output),'summaries':summaries,'safety_checks':report['safety_checks'],
                      'known_failure_checks':known,'regressions':regressions}),flush=True)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',type=Path,default=PIPELINE_ROOT.parent.parent/'BrakeCheck_Dataset')
    parser.add_argument('--selection-root',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--subsets',nargs='+',choices=SUBSETS,default=list(SUBSETS))
    parser.add_argument('--converter',choices=('source_converter_a2d2','source_converter_a2d2_v2'),default='source_converter_a2d2')
    args=parser.parse_args()
    selection=args.selection_root or args.data_root/'reports/a2d2_repair-20261018/independent_evaluation'
    evaluate(args.data_root,selection,args.output,tuple(args.subsets),args.converter)


if __name__=='__main__':
    main()
