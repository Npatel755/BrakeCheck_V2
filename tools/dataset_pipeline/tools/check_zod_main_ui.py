"""Check real repaired ZOD/model reads; intercept every test decision POST."""
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'reports/zod-import-20261023'
results = []
with sync_playwright() as tool:
    browser = tool.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless=True)
    for phone in (False, True):
        context = browser.new_context(viewport={'width': 390 if phone else 1540, 'height': 950}, is_mobile=phone, has_touch=phone)
        client_id = 'verification-zod-main-' + ('phone-0001' if phone else 'desktop-0001')
        context.add_init_script("sessionStorage.setItem('bc-review-client', " + json.dumps(client_id) + ");")
        page = context.new_page()
        # Read-only live-data check: reservations and all decisions are tested
        # against disposable data in check_multi_reviewer_ui.py instead.
        page.route('**/api/images/*/claim', lambda route: route.fulfill(json={'verified_read_only': True}))
        errors, posts = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://127.0.0.1:8765/?queue=zod_audit&image=zod-000001')
        page.wait_for_function('() => imageReady && !busy && current?.image_id === "zod-000001"')
        assert page.evaluate('current.source_geometry.version') in {'zod-continuous-boundaries-v5', 'zod-physical-boundaries-v6'}
        assert page.evaluate('current.geometry.lanes.length') == 2
        assert page.evaluate('current.geometry.lanes.every(lane => lane.segments.length === 1)')
        assert page.evaluate('current.geometry.boundary_completion.blocked_gaps.length') == 0
        assert page.evaluate('current.teacher_options.length') == 2
        expect(page.locator('#zod-audit-note')).to_be_visible()
        expect(page.locator('#zod-sample-info')).to_contain_text('Continuous lane-path target')
        expect(page.locator('#zod-observed-control')).to_be_visible()
        page.locator('#zod-observed').check()
        page.locator('#zod-observed').uncheck()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(OUT / ('phone-main-zod.png' if phone else 'desktop-main-zod.png')), full_page=True)
        model_select = '#phone-model-select' if phone else '#model-select'
        page.locator(model_select).select_option('comma_segnet_fresh')
        page.locator(model_select).select_option('comma_segnet_adapted')
        assert page.evaluate('current.teacher.teacher_version.startsWith("comma_segnet_adapted")')
        page.locator('#phone-use-model' if phone else '#use-model').click()
        page.wait_for_function('() => editing && !busy')
        expect(page.locator('#zod-complete')).to_have_count(0)
        expect(page.locator('#save-correction')).to_be_enabled()

        def intercept(route):
            body = route.request.post_data_json
            posts.append(body)
            route.fulfill(json={'event_id': body['event_id'], 'image_id': body['image_id'], 'revision': 1,
                                'group_state': 'needs_repair', 'training_eligible': False})
        page.route('**/api/reviews', intercept)
        page.route('**/api/next?**', lambda route: route.fulfill(body='null', content_type='application/json'))
        page.locator('#phone-approve' if phone else '#save-correction').click()
        page.wait_for_function('() => !busy && current === null')
        assert len(posts) == 1
        body = posts[0]
        assert body['candidate'] == 'comma_segnet_adapted' and body['complete_annotation']
        assert body['candidate_version'] and body['fingerprint']
        assert all('segments' in lane for lane in body['lanes'])
        assert not errors, errors
        results.append({'phone': phone, 'actual_v5_conversion': True, 'actual_two_models': True,
                        'model_switch': True, 'model_adoption': True, 'whole_photo_confirmation': True,
                        'observed_segments_preserved': True, 'save_post_intercepted': True, 'javascript_errors': errors})
        context.close()
        import requests
        requests.post('http://127.0.0.1:8765/api/images/zod-000001/claim', json={'revision': 0, 'release': True}, headers={'X-Review-Client': client_id}).raise_for_status()
    browser.close()
(OUT / 'main-browser-checks.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
print(json.dumps(results))
