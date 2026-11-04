"""Compare source converter revisions against immutable sample reviews."""
import json
from pathlib import Path
import sys
import cv2
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bcdata.local import local_settings
from bcdata.db import Registry
from bcdata.teachers.source_labels import load_classes
from bcdata.source_converter import repair_geometry


def score(geo, human):
    h, w = geo['height'], geo['width']
    masks = []
    for record in (geo, human):
        lanes = {}
        for lane in record.get('lanes', []):
            if lane.get('slot') not in (1,2,3,4): continue
            canvas = np.zeros((h,w), np.uint8)
            pts = np.round([p[:2] for p in lane['points']]).astype(np.int32)
            cv2.polylines(canvas, [pts], False, 1, thickness=20)
            lanes[lane['slot']] = canvas.astype(bool)
        masks.append(lanes)
    a,b = masks
    tp = sum((a[k] & b[k]).sum() / max(1,(a[k] | b[k]).sum()) >= .5 for k in set(a)&set(b))
    return {'tp':int(tp), 'fp':len(a)-int(tp), 'fn':len(b)-int(tp), 'count_match':len(a)==len(b)}


def main():
    s=local_settings(); r=Registry(s.db_path)
    source = json.loads(s.path('reports','source_audit_investigation','report.json').read_text())['all_samples']
    output = s.path('reports','source_repair_evaluation'); output.mkdir(exist_ok=True)
    records=[]
    for item in source:
        if item['status']=='rejected': continue
        classes=load_classes(r.annotation(item['image_id'],'source_label'))
        geo=repair_geometry(item['source'], (classes==2).astype(np.float32))
        geo['image_id']=item['image_id']
        (output/(item['image_id']+'.json')).write_text(json.dumps(geo))
        records.append({'image_id':item['image_id'],'decision':item['status'],
                        'old':score(item['source'],item['human']), 'new':score(geo,item['human']),
                        'changes':geo['source_converter']})
    totals={}
    for subset in ('all','approved','corrected'):
        rows=[x for x in records if subset=='all' or x['decision']==subset]
        totals[subset]={}
        for version in ('old','new'):
            tp,fp,fn=(sum(x[version][key] for x in rows) for key in ('tp','fp','fn'))
            totals[subset][version]={'tp':tp,'fp':fp,'fn':fn,'f1':2*tp/max(1,2*tp+fp+fn),
                                   'count_matches':sum(x[version]['count_match'] for x in rows),'images':len(rows)}
    report={'metric':'same-slot 20px raster IoU>=0.5 against sample reviews; converter development check, not held-out accuracy',
            'totals':totals,'records':records}
    (output/'report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(totals))
    r.close()


if __name__=='__main__': main()
