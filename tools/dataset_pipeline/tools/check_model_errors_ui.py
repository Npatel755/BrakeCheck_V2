"""Read-only live review checks; save requests are intercepted in the browser."""
import json
from pathlib import Path
from playwright.sync_api import expect, sync_playwright


def main():
    output = Path(__file__).resolve().parents[3] / 'reports/model-errors-ui-20261023'
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as tool:
        browser = tool.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless=True)
        results = []
        for mobile in (False, True):
            context = browser.new_context(viewport={'width': 390 if mobile else 1460, 'height': 950}, is_mobile=mobile, has_touch=mobile)
            page = context.new_page(); errors=[]
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto('http://127.0.0.1:8765/model-errors')
            expect(page.locator('#case')).to_be_visible(timeout=30000)
            expect(page.locator('#evaluation-metrics')).to_contain_text('Geometry v3')
            assert page.locator('#photo polyline').count() > 0
            photo_url = page.locator('#photo image').get_attribute('href')
            assert page.request.get('http://127.0.0.1:8765' + photo_url).status == 200
            page.locator('#kind').select_option('missed')
            expect(page.locator('#case')).to_be_visible()
            page.locator('#policy').select_option('short_span_geometry_v3')
            expect(page.locator('#case')).to_be_visible()
            page.locator('#show-labels').uncheck()
            assert page.locator('#photo polyline[stroke="#ffdb69"]').count() == 0
            page.locator('#show-labels').check()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / ('phone.png' if mobile else 'desktop.png')), full_page=True)
            saves=[]
            def save(route):
                saves.append(route.request.post_data_json)
                route.fulfill(json={'saved': True, 'labels_changed': False})
            page.route('**/api/studio/model-errors/*/decision', save)
            page.locator('#notes').fill('Browser-only verification; never saved to real reviews.')
            page.get_by_role('button', name='Flag label question', exact=True).click()
            expect(page.locator('#case')).to_be_visible()
            assert saves and saves[0]['action'] == 'label_question' and saves[0]['fingerprint']
            assert not errors, errors
            results.append({'mobile':mobile,'filters':True,'overlays':True,'save_intercepted':True,'javascript_errors':errors})
            context.close()
        browser.close()
    (output / 'result.json').write_text(json.dumps(results, indent=2))
    print(json.dumps(results))


if __name__ == '__main__':
    main()
