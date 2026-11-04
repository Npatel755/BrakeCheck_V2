"""Apply evaluated v2 drafts to untouched flagged Audi conversions after each import wave.

Every change uses the existing guarded installer and fresh real-human sample
policy. Original labels, human decisions and the running importer are untouched.
"""
from __future__ import annotations
import argparse,json,os,time
from pathlib import Path
import sqlite3
from .bootstrap import atomic_json
from .local import local_settings
from .repair_a2d2_conversions import prepare,apply_plan,verify_regression_report
from .teachers.base import file_sha256


def eligible(settings, checked):
    con=sqlite3.connect(settings.db_path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        rows=con.execute("""SELECT i.image_id,c.geometry_sha256 FROM source_conversion_checks c JOIN images i USING(image_id)
          WHERE i.source='a2d2' AND i.status='curated' AND i.locked=0
          AND i.review_status NOT IN ('approved','corrected','rejected') AND c.state='flagged'
          AND c.diagnostics LIKE '%shared_foreground_slot_order%'
          AND NOT EXISTS(SELECT 1 FROM review_events e WHERE e.image_id=i.image_id)
          AND NOT EXISTS(SELECT 1 FROM reviews r WHERE r.image_id=i.image_id)
          AND NOT EXISTS(SELECT 1 FROM annotations a WHERE a.image_id=i.image_id AND a.kind='human')
          AND NOT EXISTS(SELECT 1 FROM review_claims r WHERE r.image_id=i.image_id AND r.expires>?)
          ORDER BY i.image_id""",(time.time(),)).fetchall()
        return {i:sha for i,sha in rows if checked.get(i)!=sha}
    finally:con.close()


def run(settings,report,*,watch=False,poll_seconds=30):
    from filelock import FileLock
    import cv2
    cv2.setNumThreads(1)
    state_path=settings.path('reports','a2d2_converter_v2_upgrade.json')
    with FileLock(str(settings.path('logs','a2d2-converter-v2-upgrade.lock')),timeout=0):
        state={'schema':'a2d2_converter_v2_upgrade:v1','pid':os.getpid(),'phase':'verifying_evaluation',
               'regression_report':str(Path(report).resolve()),'checked':{},'batches':[],
               'installed':0,'human_decisions_created':0,'originals_modified':False}
        if state_path.exists():
            old=json.loads(state_path.read_text(encoding='utf-8'))
            if old.get('regression_report')==state['regression_report']:
                state.update({k:old[k] for k in ('checked','batches','installed','backup') if k in old})
        def update(**changes):
            state.update(changes,updated_at=time.time());atomic_json(state_path,state)
        update()
        try:
            validated=verify_regression_report(report)
            if validated['converter_version']!='a2d2-pairwise-source-v2':
                raise ValueError('This worker only handles the independently evaluated v2 converter')
            state['regression_report_sha256']=file_sha256(report)
            while True:
                candidates=eligible(settings,state['checked'])
                if candidates:
                    selected=dict(list(candidates.items())[:128])
                    update(phase='preparing',selected=len(selected),remaining_candidates=len(candidates))
                    plan=prepare(settings,report,image_ids=list(selected))
                    if plan['errors']:
                        raise RuntimeError('Draft preparation errors; nothing from this plan installed')
                    if plan['drafts']:
                        if not state.get('backup'):
                            from .review_store import ReviewStore
                            update(phase='backing_up')
                            state['backup']=ReviewStore(settings).backup()
                        update(phase='installing',plan_path=plan['plan_path'])
                        result=apply_plan(settings,plan['plan_path'])
                        count=len(result.get('applied_ids',[]))
                        state['installed']+=count
                    else:result={'installed':0,'samples':0}
                    state['batches'].append({'plan':plan['plan_path'],'selected':len(selected),'drafts':plan['drafts'],'result':result})
                    # Mark the CURRENT geometry so an unchanged v2 warning does
                    # not immediately requeue the same repair over and over.
                    con=sqlite3.connect(settings.db_path.resolve().as_uri()+'?mode=ro',uri=True)
                    try:
                        for identifier,old_sha in selected.items():
                            row=con.execute('SELECT geometry_sha256 FROM source_conversion_checks WHERE image_id=?',(identifier,)).fetchone()
                            state['checked'][identifier]=row[0] if row else old_sha
                    finally:con.close()
                    update(phase='batch_complete')
                    continue
                progress=settings.path('reports','a2d2_remaining-20261018','progress.json')
                complete=progress.exists() and json.loads(progress.read_text(encoding='utf-8')).get('phase')=='complete'
                if not watch or complete:
                    update(phase='complete');return state
                update(phase='waiting_for_next_import_wave')
                time.sleep(poll_seconds)
        except Exception as error:
            update(phase='error',error_type=type(error).__name__,error=str(error)[:700]);raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root')
    parser.add_argument('--regression-report',type=Path,required=True)
    parser.add_argument('--watch-import',action='store_true')
    args=parser.parse_args()
    print(json.dumps(run(local_settings(args.data_root),args.regression_report,watch=args.watch_import)),flush=True)

if __name__=='__main__':main()
