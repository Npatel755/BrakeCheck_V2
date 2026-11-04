"""Read-only audit of source sample decisions; outputs evidence, never decisions."""
import json
from collections import Counter
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bcdata.local import local_settings
from bcdata.db import Registry


def main():
    s = local_settings()
    r = Registry(s.db_path)
    output = s.path('reports', 'source_audit_investigation')
    output.mkdir(exist_ok=True)
    records = []
    for g in r.conn.execute('SELECT * FROM source_validation_groups').fetchall():
        for image_id in json.loads(g['sample_ids']):
            row = r.image(image_id)
            check = r.conn.execute('SELECT * FROM source_conversion_checks WHERE image_id=?', (image_id,)).fetchone()
            source = json.loads(Path(check['geometry_path']).read_text())
            ann = r.annotation(image_id, 'human')
            human = json.loads(Path(ann['path']).read_text()) if ann else None
            events = [dict(e) for e in r.conn.execute('SELECT * FROM review_events WHERE image_id=? ORDER BY created_at', (image_id,))]
            sl = source.get('lanes', [])
            hl = human.get('lanes', []) if human else []
            byslot = lambda lanes: {lane.get('slot'): lane for lane in lanes if lane.get('slot') in (1, 2, 3, 4)}
            a, b = byslot(sl), byslot(hl)
            changes = []
            for slot in sorted(set(a) | set(b)):
                if slot not in a:
                    changes.append(f'added_slot_{slot}')
                elif slot not in b:
                    changes.append(f'removed_slot_{slot}')
                elif [p[:2] for p in a[slot].get('points', [])] != [p[:2] for p in b[slot].get('points', [])]:
                    changes.append(f'edited_slot_{slot}')
            rec = {'image_id': image_id, 'stratum': g['stratum'], 'group_state': g['state'],
                   'status': row['review_status'], 'source_slots': sorted(a), 'human_slots': sorted(b),
                   'changes': changes, 'source': source, 'human': human,
                   'image': dict(row), 'events': events, 'source_mask': check['source_path']}
            records.append(rec)
    report = {'samples': len(records), 'statuses': dict(Counter(x['status'] for x in records)),
              'blocked_samples': [x for x in records if x['group_state'] == 'blocked'], 'all_samples': records}
    changed_samples = [x for x in records if x['status'] == 'corrected']
    report['findings'] = {
        'blocked_groups': len({x['stratum'] for x in report['blocked_samples']}),
        'corrected_images_adding_boundaries': sum(any(c.startswith('added_') for c in x['changes']) for x in changed_samples),
        'added_boundaries': sum(c.startswith('added_') for x in changed_samples for c in x['changes']),
        'corrected_images_removing_boundaries': sum(any(c.startswith('removed_') for c in x['changes']) for x in changed_samples),
        'corrected_images_with_unassigned_source_fragments': sum(any(lane.get('slot') is None for lane in x['source']['lanes']) for x in changed_samples),
    }
    (output / 'report.json').write_text(json.dumps(report, indent=2))
    from PIL import Image, ImageDraw
    changed = [x for x in records if x['status'] != 'approved']
    for page in range((len(changed)+5)//6):
        sheet = Image.new('RGB', (1200, 6*310), '#171b19')
        for index, rec in enumerate(changed[page*6:page*6+6]):
            photo = Image.open(rec['image']['path']).convert('RGB')
            for col, (label, geo) in enumerate((('SOURCE CONVERSION', rec['source']), ('HUMAN '+rec['status'], rec['human']))):
                view = photo.copy()
                draw = ImageDraw.Draw(view)
                for lane in (geo or {}).get('lanes', []):
                    pts = [tuple(p[:2]) for p in lane.get('points', [])]
                    if len(pts) > 1:
                        color = {1:'#ffbf47',2:'#ff5555',3:'#55ccff',4:'#66ff88'}.get(lane.get('slot'), 'white')
                        draw.line(pts, fill=color, width=5)
                view.thumbnail((598,270))
                sheet.paste(view,(col*600,index*310+40))
                sd = ImageDraw.Draw(sheet)
                sd.text((col*600+5,index*310+2),label+' '+rec['image_id'][-16:],fill='white')
                sd.text((col*600+5,index*310+18),rec['stratum'],fill='white')
        sheet.save(output / f'comparison-{page+1}.jpg')
    print(json.dumps({'samples': report['samples'], 'statuses': report['statuses'],
        'blocked': [{k:x[k] for k in ('image_id','stratum','status','source_slots','human_slots','changes')} for x in report['blocked_samples']]}))
    print('IMAGE_KEYS', list(records[0]['image']))
    print('HUMAN_KEYS', list((records[0]['human'] or {})))
    r.close()


if __name__ == '__main__':
    main()
