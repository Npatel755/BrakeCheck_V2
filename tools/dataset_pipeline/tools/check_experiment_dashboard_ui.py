"""Read-only desktop/phone and live-refresh checks for the experiment monitor."""
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    output = Path(__file__).resolve().parents[3] / 'reports/experiment-dashboard-ui-20261022'
    output.mkdir(parents=True, exist_ok=True)
    edge = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
    results = []
    with sync_playwright() as tool:
        browser = tool.chromium.launch(executable_path=str(edge), headless=True)
        for mobile in (False, True):
            context = browser.new_context(viewport={'width': 390 if mobile else 1460, 'height': 900},
                                          is_mobile=mobile, has_touch=mobile)
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto('http://127.0.0.1:8765/experiments')
            expect(page.locator('#detail')).to_be_visible(timeout=30000)
            expect(page.locator('#connection')).to_contain_text('Live')
            expect(page.locator('#metrics')).to_contain_text('32,638')
            assert page.locator('.run-card').count() > 1
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / ('phone.png' if mobile else 'desktop.png')), full_page=True)
            payload = page.request.get('http://127.0.0.1:8765/api/studio/experiments').json()
            key = payload['runs'][0]['id']
            detail = page.request.get(f'http://127.0.0.1:8765/api/studio/experiments/{key}').json()
            # Serve browser-only fixtures; training reports are never changed.
            detail.update(step=1234, total_steps=2000, epoch=4, phase='training', worker_alive=True,
                          history=[{'epoch': 1, 'train_loss': 2.3, 'validation': {'lane_f1': .72}},
                                   {'epoch': 2, 'train_loss': 1.9, 'validation': {'lane_f1': .75}}])
            page.route(f'**/api/studio/experiments/{key}', lambda route: route.fulfill(json=detail))
            # Polling must update without a user click.
            expect(page.locator('#progress-label')).to_contain_text('1,234', timeout=15000)
            expect(page.locator('#percent')).to_have_text('61.7%')
            assert page.locator('#loss-chart svg').count() == 1
            assert page.locator('#f1-chart svg').count() == 1
            point = page.locator('#f1-chart .chart-point').nth(1)
            point.hover()
            expect(page.locator('#f1-chart .chart-tooltip')).to_have_attribute('visibility', 'visible')
            expect(page.locator('#f1-chart .chart-tooltip')).to_contain_text('Epoch 2 · Lane F1 75.00%')
            # An unchanged history poll must preserve the hovered point.
            page.wait_for_timeout(5500)
            expect(page.locator('#f1-chart .chart-tooltip')).to_have_attribute('visibility', 'visible')
            point.focus()
            point.press('Escape')
            expect(page.locator('#f1-chart .chart-tooltip')).to_have_attribute('visibility', 'hidden')
            loss_point = page.locator('#loss-chart .chart-point').first
            loss_point.focus()
            expect(page.locator('#loss-chart .chart-tooltip')).to_contain_text('Epoch 1 · Training loss 2.3')
            if mobile:
                point.tap()
                expect(page.locator('#f1-chart .chart-tooltip')).to_have_attribute('visibility', 'visible')
            page.screenshot(path=str(output / ('phone-tooltips.png' if mobile else 'desktop-tooltips.png')), full_page=True)
            detail.update(phase='interrupted', reported_phase='training', worker_alive=False)
            page.get_by_role('button', name='Refresh now').click()
            expect(page.locator('#error')).to_contain_text('worker has exited')
            page.route('**/api/studio/experiments', lambda route: route.fulfill(status=503, json={}))
            page.get_by_role('button', name='Refresh now').click()
            expect(page.locator('#connection')).to_contain_text('Displayed data may be out of date')
            page.unroute('**/api/studio/experiments')
            page.get_by_role('button', name='Refresh now').click()
            expect(page.locator('#connection')).to_contain_text('Live')
            assert not errors, errors
            results.append({'mobile': mobile, 'auto_refresh': True, 'charts': True, 'point_tooltips': True,
                            'stopped_worker': True, 'disconnect_recovery': True, 'javascript_errors': errors})
            context.close()
        browser.close()
    (output / 'result.json').write_text(json.dumps(results, indent=2))
    print(json.dumps(results))


if __name__ == '__main__':
    main()
