"""Read-only desktop/phone QA; queue reservations and review writes are intercepted."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[3] / 'reports/zod-clearance-20261024'
OUT.mkdir(parents=True, exist_ok=True)
checks = []
with sync_playwright() as p:
    browser = p.chromium.launch(channel='msedge', headless=True)
    for width in (1440, 390):
        context = browser.new_context(viewport={'width': width, 'height': 950},
                                      is_mobile=width == 390, has_touch=width == 390)
        page = context.new_page()
        errors, writes = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/api/next?**', lambda route: route.fulfill(body='null', content_type='application/json'))
        def intercept(route):
            if route.request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
                writes.append(route.request.url)
                route.fulfill(json={})
            else:
                route.continue_()
        page.route('**/api/images/**', intercept)
        page.route('**/api/reviews**', intercept)
        page.goto('http://127.0.0.1:8765/?queue=zod_audit')
        page.wait_for_function("() => document.getElementById('zod-audit-counts').textContent.includes('labels cleared')")
        summary = page.locator('#zod-audit-counts').inner_text()
        assert '490 labels cleared' in summary and '461 individually approved' in summary, summary
        assert '6,691 photos need repair' in summary, summary
        if width == 390:
            assert '485 exported' in page.locator('#phone-progress').inner_text()
        assert '485 ZOD images / 33,123 total training images' in summary
        assert 'Repair-held and skipped samples remain separate' in page.locator('#stage-message').inner_text()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors, errors
        assert not writes, writes
        page.screenshot(path=str(OUT / f'counts-{width}.png'), full_page=True)
        checks.append({'viewport_width': width, 'summary': summary, 'javascript_errors': errors,
                       'review_writes': writes, 'no_horizontal_overflow': True})
        context.close()
    browser.close()
(OUT / 'browser-verification.json').write_text(json.dumps(checks, indent=2) + '\n')
print(json.dumps(checks, indent=2))
