"""Read-only live checks of the labeling page's actual worker status."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

root = Path(__file__).resolve().parents[3]
out = root / 'reports/lead-cars-training-20261024'
out.mkdir(parents=True, exist_ok=True)
results = []
with sync_playwright() as p:
    browser = p.chromium.launch(channel='msedge', headless=True)
    for width in [1440, 390]:
        context = browser.new_context(viewport={'width': width, 'height': 950},
                                      is_mobile=width == 390, has_touch=width == 390)
        page = context.new_page(); errors = []; writes = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        def guard(route):
            if route.request.method != 'GET':
                writes.append(route.request.url); route.abort()
            else:
                route.continue_()
        page.route('**/api/**', guard)
        page.goto('http://127.0.0.1:8765/lead-cars')
        expect(page.locator('#count')).to_have_text('500 / 500 labeled')
        expect(page.locator('#training-status')).to_be_visible()
        expect(page.locator('#training-status')).to_contain_text('Lead model training')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(out / f'training-status-{width}.png'), full_page=True)
        assert not writes and not errors
        results.append({'width': width, 'text': page.locator('#training-status').inner_text(),
                        'no_label_writes': True, 'javascript_errors': errors})
        context.close()
    browser.close()
(out / 'studio-verification.json').write_text(json.dumps(results, indent=2) + '\n')
print(json.dumps(results, indent=2))
