"""Verify explicit one-click approval; intercept every review/claim write."""
from copy import deepcopy
import json
from pathlib import Path
import sys

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bcdata.zod_main_review import detail
from bcdata.zod_mirror_review import ZodMirrorReview

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'reports/zod-single-action-20261024'
OUT.mkdir(parents=True, exist_ok=True)
item = detail(ZodMirrorReview(ROOT / 'BrakeCheck_Dataset'), 'zod-000001')
assert item['geometry']['lanes'] and item['source_qc_reasons']
results = []

with sync_playwright() as p:
    browser = p.chromium.launch(channel='msedge', headless=True)
    for phone in (False, True):
        for scenario in ('approve', 'approve_empty', 'clear_and_save'):
            print(f'Checking phone={phone} scenario={scenario}', flush=True)
            context = browser.new_context(viewport={'width': 390 if phone else 1440, 'height': 950},
                                          is_mobile=phone, has_touch=phone)
            page = context.new_page()
            fixture = deepcopy(item)
            if scenario == 'approve_empty':
                fixture['geometry']['lanes'] = []
                fixture['source_geometry']['lanes'] = []
            errors, posts = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/images/zod-000001', lambda route: route.fulfill(json=fixture))
            page.route('**/api/images/*/claim', lambda route: route.fulfill(json={'read_only_check': True}))
            page.route('**/api/next?**', lambda route: route.fulfill(body='null', content_type='application/json'))
            def intercept(route):
                body = route.request.post_data_json
                posts.append(body)
                route.fulfill(json={'event_id': body['event_id'], 'image_id': body['image_id'],
                                    'revision': body['revision'] + 1, 'training_eligible': False})
            page.route('**/api/reviews', intercept)
            page.goto('http://127.0.0.1:8765/?queue=zod_audit&image=zod-000001')
            page.wait_for_function('() => imageReady && !busy && current?.image_id === "zod-000001"')
            expect(page.locator('#zod-complete')).to_have_count(0)
            expect(page.locator('#zod-no-paint')).to_have_attribute('type', 'button')
            assert posts == [], 'Loading must never create an approval'
            if scenario == 'clear_and_save':
                page.locator('#zod-no-paint').click()
                page.wait_for_function('() => editing && !lanes.length && zodNoVisiblePaint')
                page.evaluate('undoEdit()')
                page.wait_for_function('() => lanes.length > 0 && !zodNoVisiblePaint')
                page.locator('#zod-no-paint').click()
                page.wait_for_function('() => editing && !lanes.length && zodNoVisiblePaint')
                page.reload()
                page.wait_for_function('() => imageReady && !busy')
                page.locator('#restore-draft').click()
                page.wait_for_function('() => editing && !lanes.length && zodNoVisiblePaint')
                action = '#phone-approve' if phone else '#save-correction'
            else:
                action = '#phone-approve' if phone else '#approve'
            expect(page.locator(action)).to_be_enabled()
            if scenario == 'approve_empty':
                expect(page.locator(action)).to_contain_text('no paint' if phone else 'no-visible-paint')
            if scenario == 'approve':
                page.screenshot(path=str(OUT / ('phone.png' if phone else 'desktop.png')), full_page=True)
            page.locator(action).click()
            page.wait_for_function('() => !busy && current === null')
            assert len(posts) == 1
            assert posts[0]['complete_annotation'] is True
            assert posts[0]['no_visible_lane_paint'] is (scenario != 'approve')
            assert posts[0]['fingerprint'] == fixture['fingerprint']
            if scenario == 'clear_and_save':
                assert posts[0]['lanes'] == [] and posts[0]['candidate'] is None
            assert not errors, errors
            results.append({'phone': phone, 'scenario': scenario, 'no_required_checkboxes': True,
                            'explicit_save_only': True, 'intercepted_review_posts': len(posts),
                            'javascript_errors': errors})
            context.close()
    browser.close()
(OUT / 'verification.json').write_text(json.dumps(results, indent=2) + '\n')
print(json.dumps(results, indent=2))
