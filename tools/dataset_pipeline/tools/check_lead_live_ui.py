"""Read-only visual QA of actual curated photos; no real claims or saves."""
import json
from pathlib import Path
import sys

from playwright.sync_api import expect,sync_playwright
from PIL import Image,ImageDraw

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bcdata.lead_review import LeadReview,inside

ROOT=Path(__file__).resolve().parents[3];OUT=ROOT/'reports/lead-cars-20261024'
OUT.mkdir(parents=True,exist_ok=True)
store=LeadReview(ROOT/'BrakeCheck_Dataset');db=store.db()
rows=list(db.execute('SELECT * FROM images ORDER BY image_id'))
assert len(rows)==500
item=store._detail(rows[0]);db.close()
results=[]
with sync_playwright() as p:
    browser=p.chromium.launch(channel='msedge',headless=True)
    for phone in (False,True):
        context=browser.new_context(viewport={'width':390 if phone else 1440,'height':950},is_mobile=phone,has_touch=phone)
        page=context.new_page();errors=[];writes=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.route('**/api/studio/lead-cars/next?**',lambda r:r.fulfill(json=item))
        page.route('**/api/studio/lead-cars/images/*/claim',lambda r:r.fulfill(json={'read_only_test':True}))
        def intercept(r):
            writes.append(r.request.url);r.abort()
        page.route('**/api/studio/lead-cars/labels',intercept)
        page.goto('http://127.0.0.1:8765/lead-cars')
        expect(page.locator('#image-info')).to_contain_text(item['image_id'])
        page.wait_for_function('() => document.getElementById("lead-canvas").width>1000')
        assert '/ 500 labeled' in page.locator('#count').inner_text()
        expect(page.locator('#save')).to_be_disabled()
        page.screenshot(path=str(OUT/('phone-real.png' if phone else 'desktop-real.png')),full_page=True)
        assert not errors and not writes
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        results.append({'phone':phone,'actual_curated_photo':item['image_id'],'no_real_writes':True,
                        'javascript_errors':errors,'500_images_available':True})
        context.close()
    browser.close()
(OUT/'live-browser-verification.json').write_text(json.dumps(results,indent=2)+'\n')
# Contact sheets are curation inspection aids, not human ground truth.
chosen=[json.loads(r['record_json']) for r in rows]
for sheet_no in (0,1,2):
    sheet=Image.new('RGB',(1440,900),'#0c1410');draw=ImageDraw.Draw(sheet)
    for tile_index,record in enumerate(chosen[sheet_no*25:sheet_no*25+25]):
        im=Image.open(inside(store.root,record['image'])).convert('RGB')
        im.thumbnail((288,155));col,row=tile_index%5,tile_index//5
        x,y=col*288,row*180;sheet.paste(im,(x,y))
        draw.text((x+5,y+157),record['image_id'][-16:]+' '+record['curation_bucket'],fill='#b4edce')
    sheet.save(OUT/f'contact-{sheet_no}.jpg',quality=90)
print(json.dumps(results,indent=2))
