"""Verify reported duplicate-boundary examples without production mutations."""
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

OUT = Path(__file__).resolve().parents[3] / 'reports/zod-import-20261023'
results = []
with sync_playwright() as tool:
    browser = tool.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless=True)
    context = browser.new_context(viewport={'width': 1600, 'height': 1000})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/api/images/*/claim', lambda route: route.fulfill(json={'read_only_check': True}))
    page.route('**/api/reviews', lambda route: route.abort())
    for image, slots in [('zod-005033', [2]), ('zod-005909', [2]), ('zod-006567', [2, 3, 4]),
                         ('zod-004719', [1, 2, 3, 4]), ('zod-000777', [2, 3])]:
        page.goto('http://127.0.0.1:8765/?queue=zod_repair&image=' + image)
        page.wait_for_function('(id) => imageReady && !busy && current?.image_id === id', arg=image)
        state = page.evaluate('({image_id: current.image_id, slots: current.geometry.lanes.map(l=>l.slot), source_version:current.source_geometry.version, decision:current.decision, group:current.group_state, repair:current.geometry.boundary_deduplication})')
        assert state['slots'] == slots
        assert state['source_version'] == 'zod-physical-boundaries-v6'
        assert state['group'] == 'needs_repair'
        expect(page.locator('#zod-sample-info')).to_contain_text('Paired paint')
        if image == 'zod-000777':
            assert state['decision'] is None
        if image == 'zod-004719':
            assert state['repair']['suspect_pairs'] and not state['repair']['merged_pairs']
        else:
            assert state['repair']['merged_pairs']
        assert page.evaluate('current.geometry.lanes.every(l=>l.segments.length===1)')
        page.locator('#image-comparison').screenshot(path=str(OUT / (image + '-physical-boundaries-v6.png')))
        results.append(state)
    assert not errors, errors
    browser.close()
(OUT / 'boundary-v6-browser-checks.json').write_text(json.dumps({'cases': results, 'javascript_errors': errors,
                'production_mutations': False}, indent=2))
print(json.dumps({'cases_checked': len(results), 'javascript_errors': errors, 'production_mutations': False}))
