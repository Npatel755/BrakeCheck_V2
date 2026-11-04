"""Verify accepted alert resolution and undo; intercept all review mutations."""
import copy
import json
from pathlib import Path
from playwright.sync_api import expect, sync_playwright


def main():
    output = Path(__file__).resolve().parents[3] / 'reports/review-outcomes-ui-20261023'
    output.mkdir(parents=True, exist_ok=True)
    base = 'http://127.0.0.1:8765'
    results = []
    with sync_playwright() as tool:
        browser = tool.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless=True)
        for mobile in (False, True):
            context = browser.new_context(viewport={'width':390 if mobile else 1460,'height':950},is_mobile=mobile,has_touch=mobile)
            page = context.new_page(); errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            collections = page.request.get(base + '/api/studio/model-errors').json()['collections']
            collection = next(c for c in collections if c['phase']=='complete')['id']
            api = f'/api/studio/model-errors/{collection}'
            queue = page.request.get(base + api + '/queue?policy=stock&status=pending&kind=all&limit=1').json()
            row = queue['items'][0]
            accepted = [False]; writes=[]
            initial = dict(alert_photos=1,unreviewed=1,accepted_predictions=0,confirmed_model_errors=0,label_questions=0,skipped=0,open_alerts=1,policy='stock',measured_metrics_changed=False)
            def summary():
                return {**initial,'accepted_predictions':int(accepted[0]),'open_alerts':int(not accepted[0]),'unreviewed':int(not accepted[0])}
            def fake_queue(route):
                from urllib.parse import urlsplit,parse_qs
                status=parse_qs(urlsplit(route.request.url).query).get('status',['pending'])[0]
                item=copy.deepcopy(row)
                item['review']={'action':'acceptable','notes':''} if accepted[0] else None
                visible=(status=='accepted' and accepted[0]) or (status in ['pending','open'] and not accepted[0]) or status=='all'
                route.fulfill(json={'items':[item] if visible else [],'total':int(visible),'phase':'complete','review_summary':summary()})
            def fake_save(route):
                writes.append(route.request.post_data_json);accepted[0]=True
                route.fulfill(json={'saved':True,'message':'Model prediction accepted. This photo is dismissed from open error alerts.','summary':summary()})
            def fake_undo(route):
                accepted[0]=False;route.fulfill(json={'undone':True,'summary':summary()})
            page.route('**'+api+'/queue?*',fake_queue)
            page.route('**'+api+'/summary?*',lambda route:route.fulfill(json=summary()))
            page.route('**'+api+'/decision',fake_save)
            page.route('**'+api+'/decisions/*?*',fake_undo)
            page.goto(base + f'/model-errors?collection={collection}&policy=stock')
            expect(page.locator('#case')).to_be_visible(timeout=30000)
            expect(page.locator('[data-review-filter="open"] strong')).to_have_text('1')
            page.get_by_role('button',name='Accept model prediction',exact=True).click()
            expect(page.locator('[data-review-filter="accepted"] strong')).to_have_text('1')
            expect(page.locator('[data-review-filter="open"] strong')).to_have_text('0')
            expect(page.locator('#decision-result')).to_contain_text('dismissed from open error alerts')
            assert writes[0]['action']=='acceptable' and writes[0]['fingerprint'] == row['fingerprint']
            page.locator('[data-review-filter="accepted"]').click()
            expect(page.locator('#case')).to_be_visible()
            expect(page.locator('#undo-current')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / ('phone.png' if mobile else 'desktop.png')),full_page=True)
            page.locator('#undo-current').click()
            expect(page.locator('[data-review-filter="accepted"] strong')).to_have_text('0')
            expect(page.locator('[data-review-filter="open"] strong')).to_have_text('1')
            expect(page.locator('#status')).to_have_value('pending')
            expect(page.locator('#case')).to_be_visible()
            assert not errors,errors
            results.append({'mobile':mobile,'accept_resolves_alert':True,'accepted_filter':True,'undo':True,'real_reviews_unchanged':True,'javascript_errors':errors})
            context.close()
        browser.close()
    (output / 'result.json').write_text(json.dumps(results,indent=2))
    print(json.dumps(results))


if __name__=='__main__':
    main()
