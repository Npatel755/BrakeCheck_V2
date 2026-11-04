"""Exercise real box saves on a disposable collection, never the user's labels."""
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright
import uvicorn

from bcdata.lead_review import LeadReview, create_lead_router, digest, encoded, file_sha

ROOT=Path(__file__).resolve().parents[3]
UI=ROOT/'tools/dataset_pipeline/review_ui'
OUT=ROOT/'reports/lead-cars-20261024'
OUT.mkdir(parents=True,exist_ok=True)
results=[]

with tempfile.TemporaryDirectory(prefix='lead-ui-') as temp, sync_playwright() as p:
    browser=p.chromium.launch(channel='msedge',headless=True)
    for phone in (False,True):
        store=LeadReview(Path(temp)/('phone' if phone else 'desktop'))
        db=store.db()
        for index in range(4):
            path=store.root/f'images/test-{index}.png';path.parent.mkdir(exist_ok=True)
            im=Image.new('RGB',(640,360),(90,120,140));draw=ImageDraw.Draw(im)
            draw.polygon([(0,360),(270,120),(370,120),(640,360)],fill=(55,60,60))
            draw.rectangle([240,160,360,280],fill=(160,190,200))
            im.save(path)
            record={'image_id':f'test-{index}','image':path.relative_to(store.root).as_posix(),
                'source':'disposable-test','source_sha256':file_sha(path),'session_id':f'session-{index}',
                'source_split':'train','split':'val' if index==3 else 'train','license_id':'fixture',
                'width':640,'height':360,'curation_bucket':'central_vehicle',
                'detections':[{'box':[240,160,360,280],'label':'car','confidence':.9}]}
            db.execute('INSERT INTO images(image_id,record_json,fingerprint) VALUES(?,?,?)',(record['image_id'],encoded(record),digest(record)))
        db.commit();db.close()
        app=FastAPI()
        def session_for(request:Request):
            return {'owner':request.headers.get('X-Review-Client','missing-client'),'reviewer':'UI test'}
        app.include_router(create_lead_router(store.root.parent.parent,session_for))
        @app.get('/api/session')
        def session():return {'paired':True,'reviewer':'UI test'}
        @app.get('/lead-cars')
        def page():return FileResponse(UI/'lead_cars.html')
        app.mount('/static',StaticFiles(directory=UI),name='static')
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
        thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
        for _ in range(100):
            if server.started:break
            time.sleep(.05)
        context=browser.new_context(viewport={'width':390 if phone else 1440,'height':950},is_mobile=phone,has_touch=phone)
        page=context.new_page();errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(f'http://127.0.0.1:{port}/lead-cars')
        expect(page.locator('#image-info')).to_contain_text('test-0')
        expect(page.locator('#save')).to_be_disabled()
        expect(page.locator('#count')).to_have_text('0 / 4 labeled')
        canvas=page.locator('#lead-canvas');r=canvas.bounding_box()
        x1,y1=r['x']+240/640*r['width'],r['y']+160/360*r['height']
        x2,y2=r['x']+360/640*r['width'],r['y']+280/360*r['height']
        page.mouse.move(x1,y1);page.mouse.down();page.mouse.move(x2,y2,steps=8);page.mouse.up()
        expect(page.locator('#save')).to_be_enabled()
        page.reload()
        expect(page.locator('#draft-notice')).to_be_visible()
        expect(page.locator('#save')).to_be_enabled()
        page.screenshot(path=str(OUT/('phone-fixture.png' if phone else 'desktop-fixture.png')),full_page=True)
        page.locator('#save').click()
        expect(page.locator('#image-info')).to_contain_text('test-1')
        expect(page.locator('#count')).to_have_text('1 / 4 labeled')
        db=store.db();row=db.execute("SELECT * FROM images WHERE image_id='test-0'").fetchone()
        assert row['action']=='box'
        b=json.loads(row['box_json']);assert all(abs(a-b)<1 for a,b in zip(b,[240,160,360,280]))
        db.close()
        page.locator('#no-lead').click()
        expect(page.locator('#image-info')).to_contain_text('test-2')
        expect(page.locator('#count')).to_have_text('2 / 4 labeled')
        page.locator('#undo-saved').click()
        expect(page.locator('#image-info')).to_contain_text('test-1')
        expect(page.locator('#count')).to_have_text('1 / 4 labeled')
        page.locator('#skip').click()
        expect(page.locator('#image-info')).to_contain_text('test-2')
        assert store.status()['counts']['skip']==1
        page.locator('#export').click()
        expect(page.locator('#export-status')).to_contain_text('Exported 1 saved labels')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert not errors,errors
        results.append({'phone':phone,'draw_native_coordinates':True,'draft_reload':True,'real_disposable_box_save':True,
                        'no_lead_save':True,'saved_undo':True,'skip_excluded_from_export':True,
                        'javascript_errors':errors,'no_horizontal_overflow':True})
        context.close();server.should_exit=True;thread.join(10);sock.close()
    browser.close()
(OUT/'browser-verification.json').write_text(json.dumps(results,indent=2)+'\n')
print(json.dumps(results,indent=2))
