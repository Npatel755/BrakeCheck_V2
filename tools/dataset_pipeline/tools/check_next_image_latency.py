"""Verify desktop and phone advance while statistics requests are held open."""
import asyncio
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_review import build_review_fixture


async def wait_state(page, expression, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await page.evaluate(expression):
            return
        await asyncio.sleep(.05)
    raise AssertionError('Browser condition timed out: ' + expression)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repair-examples', action='store_true')
    parser.add_argument('--missing-split', action='store_true')
    args = parser.parse_args()
    from playwright.async_api import async_playwright
    output = ROOT / 'test-results' / ('next-image-' + uuid.uuid4().hex[:8])
    settings = build_review_fixture(output / 'data')
    if args.missing_split:
        from bcdata.db import Registry
        registry = Registry(settings.db_path)
        for image in registry.images():
            registry.update_image(image['image_id'], split=None)
        registry.close()
    queue = 'repair_sample' if args.repair_examples else 'pending'
    if args.repair_examples:
        from PIL import Image
        import numpy as np
        from bcdata.review_store import ReviewStore
        from bcdata.conversion_diagnostics import prepare
        from bcdata.teachers.base import file_sha256
        store = ReviewStore(settings)
        with store.registry() as reg:
            for image in reg.images():
                image_id = image['image_id']
                path = settings.root / 'raw' / (image_id + '-mask.png')
                Image.fromarray(np.zeros((874, 1164), np.uint8)).save(path)
                reg.add_annotation(image_id, 'source_label', 'fixture', 'fixture-source', str(path),
                                   params={'human': True, 'format': 'class_map'})
                geometry = reg.annotation(image_id, 'geometry', 'geometry')
                source = reg.annotation(image_id, 'source_label')
                reg.conn.execute('INSERT INTO source_conversion_checks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                 (image_id, 'fixture', 'fixture:flagged', 'flagged', geometry['id'], geometry['model_version'],
                                  geometry['path'], file_sha256(geometry['path']), source['id'], source['path'],
                                  file_sha256(source['path']), json.dumps({'reasons': ['low_mask_coverage']}), time.time()))
            reg.conn.commit()
        prepare(settings, settings.root / 'reports' / 'diagnostic-fixture', apply=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    log = (output / 'server.log').open('wb')
    server = subprocess.Popen([sys.executable, str(ROOT / 'review.py'), 'serve', '--data-root', str(settings.root),
                               '--port', str(port)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                              creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
    results = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel='msedge', headless=True)
            for layout, viewport in [('desktop', {'width': 1440, 'height': 1000}),
                                     ('phone', {'width': 390, 'height': 844})]:
                context = await browser.new_context(viewport=viewport)
                page = await context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.add_init_script("""
                    new MutationObserver(() => {
                        if (document.getElementById('confirm-dialog')?.open)
                            document.getElementById('confirm-accept').click();
                    }).observe(document, {subtree:true,attributes:true,attributeFilter:['open']});
                """)
                base = f'http://127.0.0.1:{port}'
                for attempt in range(60):
                    try:
                        await page.goto(base)
                        break
                    except Exception:
                        await asyncio.sleep(.2)
                await wait_state(page, "policyReady && !busy && (imageReady || document.getElementById('stage-message').textContent.includes('No audit samples'))")
                await page.locator('#phone-queue' if layout == 'phone' else '#status-filter').select_option(queue)
                await wait_state(page, 'imageReady && !!current?.image_id')
                if args.missing_split:
                    assert await page.locator('#split-badge').text_content() == 'UNASSIGNED'
                if args.repair_examples:
                    assert 'does not approve related photos' in await page.locator('#provenance').text_content()
                previous = await page.evaluate('current.image_id')
                release = asyncio.Event()
                requested = asyncio.Event()
                finished = asyncio.Event()

                async def hold_stats(route):
                    requested.set()
                    await release.wait()
                    await route.continue_()
                    finished.set()

                await page.route('**/api/stats', hold_stats)
                try:
                    start = time.monotonic()
                    await page.locator('#phone-approve' if layout == 'phone' else '#approve').click()
                    await wait_state(page, 'imageReady && current?.image_id !== ' + json.dumps(previous), timeout=4)
                    elapsed = time.monotonic() - start
                    await asyncio.wait_for(requested.wait(), timeout=3)
                    assert not release.is_set(), 'Statistics unexpectedly released before navigation'
                    assert not errors, errors
                    results.append({'layout': layout, 'queue': queue, 'next_image_seconds': round(elapsed, 3),
                                    'statistics_still_pending': True, 'javascript_errors': errors})
                finally:
                    release.set()
                    if requested.is_set():
                        await asyncio.wait_for(finished.wait(), timeout=5)
                    await page.unroute('**/api/stats')
                    await asyncio.sleep(.1)
                    await context.close()
            await browser.close()
    finally:
        server.terminate()
        server.wait(timeout=15)
        log.close()
    (output / 'result.json').write_text(json.dumps(results, indent=2))
    print(json.dumps({'output': str(output), 'results': results}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
