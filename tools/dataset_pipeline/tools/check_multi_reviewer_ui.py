"""Exercise real concurrent browser saves against disposable data only."""
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

import requests
import uvicorn
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_review import build_review_fixture
from tests.test_zod_mirror import fixture
from bcdata.review_server import create_app

OUT = Path(__file__).resolve().parents[3] / 'reports/zod-import-20261023'
with tempfile.TemporaryDirectory(prefix='brakecheck-collaboration-') as temporary:
    data_root = Path(temporary)
    settings = build_review_fixture(data_root)
    _, store = fixture(data_root, count=8)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    app = create_app(settings, port=port, start_worker=False)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started
        with sync_playwright() as tool:
            browser = tool.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless=True)
            context = browser.new_context(viewport={'width': 1400, 'height': 950}, accept_downloads=True)
            # Reproduce duplicated tabs copying their original sessionStorage.
            context.add_init_script("sessionStorage.setItem('bc-review-client', 'cloned-client-0000001')")
            first = context.new_page()
            errors = []
            first.on('pageerror', lambda error: errors.append(str(error)))
            first.goto(base + '/?queue=zod_audit')
            first.wait_for_function('() => imageReady && !busy && current?.zod_audit')
            first_id = first.evaluate('current.image_id')
            first_client = first.evaluate('reviewClient')
            second = context.new_page()
            second.goto(base + '/?queue=zod_audit')
            second.wait_for_function('() => imageReady && !busy && current?.zod_audit')
            assert second.evaluate('reviewClient') != first_client
            assert second.evaluate('current.image_id') != first_id
            first.evaluate('beginEdit()')
            first.wait_for_function('() => editing')
            first.evaluate('lanes[0].points[0][0] += 8; changed();')
            first.locator('#zod-complete').check()
            expected_draft = first.evaluate('JSON.stringify(lanes[0].points)')
            # Simulate a sleeping device whose reservation expired, followed
            # by a legitimate completed review from another session.
            db = store._db()
            db.execute('UPDATE zod_review_claims SET expires=0 WHERE image_id=?', (first_id,))
            db.commit()
            db.close()
            headers = {'X-Review-Client': 'third-reviewer-browser-0001'}
            item = requests.get(base + '/api/images/' + first_id, headers=headers).json()
            saved = requests.post(base + '/api/reviews', headers=headers, json={
                'image_id': first_id, 'revision': item['revision'], 'fingerprint': item['fingerprint'],
                'action': 'approved', 'event_id': 'disposable-third-review-0001', 'reviewer': 'Other reviewer'})
            saved.raise_for_status()
            first.locator('#save-correction').click()
            expect(first.locator('#review-conflict')).to_be_visible()
            expect(first.locator('#save-correction')).to_be_disabled()
            expect(first.locator('#review-conflict-message')).to_contain_text('Another reviewer saved')
            assert first.evaluate('JSON.stringify(lanes[0].points)') == expected_draft
            assert first.evaluate('JSON.parse(storageGet(draftKey())).state.lanes[0].points.length') > 1
            assert store.detail(first_id)['decision']['reviewer'] == 'Other reviewer'
            with first.expect_download() as download:
                first.locator('#conflict-download').click()
            assert download.value.suggested_filename.endswith('-my-draft.json')
            first.screenshot(path=str(OUT / 'multi-reviewer-conflict.png'), full_page=True)
            first.locator('#conflict-next').click()
            first.wait_for_function('(old) => !busy && imageReady && current && current.image_id !== old', arg=first_id)
            assert first.evaluate('current.image_id') != second.evaluate('current.image_id')
            assert store.detail(first_id)['decision']['reviewer'] == 'Other reviewer'
            assert not errors, errors
            # A real paired session has different privileges/identity than the
            # local host. Old shared phone=1 links must still allow desktop edits.
            paired = requests.post(base + '/api/pair', json={'code': app.state.pairing['pin'], 'reviewer': 'Paired collaborator'})
            paired.raise_for_status()
            remote = browser.new_context(viewport={'width': 1400, 'height': 950}, extra_http_headers={'cf-ray': 'disposable-proxy-test'})
            remote.add_cookies([{'name': 'bc_review', 'value': paired.cookies['bc_review'], 'url': base, 'httpOnly': True, 'sameSite': 'Strict'}])
            partner = remote.new_page()
            partner.on('pageerror', lambda error: errors.append(str(error)))
            partner.goto(base + '/?phone=1&queue=zod_audit&image=' + first_id)
            partner.wait_for_function('() => imageReady && !busy && current?.zod_audit')
            assert partner.evaluate('viewer.local') is False
            assert partner.evaluate('viewer.reviewer') == 'Paired collaborator'
            assert not partner.locator('body').evaluate("el => el.classList.contains('phone-review')")
            partner.locator('#correct').click()
            partner.wait_for_function('() => editing')
            expect(partner.locator('#editor')).to_be_visible()
            assert partner.locator('#canvas').evaluate("el => getComputedStyle(el).pointerEvents") != 'none'
            # Exercise a real pointer drag, rather than injecting edited labels.
            handle = partner.evaluate('({x:lanes[0].points[0][0], y:lanes[0].points[0][1], before:JSON.stringify(lanes[0].points), width:canvas.width, height:canvas.height})')
            partner.locator('#canvas').scroll_into_view_if_needed()
            box = partner.locator('#canvas').bounding_box()
            x = box['x'] + handle['x'] * box['width'] / handle['width']
            y = box['y'] + handle['y'] * box['height'] / handle['height']
            partner.mouse.move(x, y)
            partner.mouse.down()
            partner.mouse.move(x + 12, y, steps=5)
            partner.mouse.up()
            assert partner.evaluate('JSON.stringify(lanes[0].points)') != handle['before']
            partner.locator('#zod-complete').check()
            with partner.expect_response(lambda response: response.url.endswith('/api/reviews') and response.request.method == 'POST') as response:
                partner.locator('#save-correction').click()
            assert response.value.status == 200
            partner.wait_for_function('() => !busy && !editing')
            assert store.detail(first_id)['decision']['reviewer'] == 'Paired collaborator'
            partner.reload()
            partner.wait_for_function('() => !busy && imageReady && current')
            expect(partner.locator('#undo')).to_be_visible()
            partner.locator('#undo').click()
            partner.wait_for_function('(id) => !busy && imageReady && current?.image_id === id && lastDecision === null', arg=first_id)
            assert store.detail(first_id)['decision']['reviewer'] == 'Other reviewer'
            partner.evaluate("async()=>await post('/api/images/'+current.image_id+'/claim',{revision:current.revision,release:true})")
            phone_context = browser.new_context(viewport={'width':390,'height':950}, is_mobile=True, has_touch=True)
            phone = phone_context.new_page()
            phone.goto(base + '/?queue=zod_audit&image=' + first_id)
            phone.wait_for_function('() => !busy && imageReady && current')
            phone.locator('#phone-approve').click()
            phone.wait_for_function('() => !busy && lastDecision !== null')
            expect(phone.locator('#phone-undo')).to_be_visible()
            phone.locator('#phone-undo').click()
            phone.wait_for_function('(id) => !busy && imageReady && current?.image_id === id && lastDecision === null', arg=first_id)
            assert store.detail(first_id)['decision']['reviewer'] == 'Other reviewer'
            phone_context.close()
            partner.screenshot(path=str(OUT / 'paired-computer-review.png'), full_page=True)
            assert not errors, errors
            browser.close()
        result = {'disposable_dataset_only': True, 'duplicated_tabs_get_distinct_clients': True,
                  'simultaneous_reviewers_get_distinct_photos': True, 'stale_save_rejected': True,
                  'other_review_preserved': True, 'draft_preserved_and_downloadable': True,
                  'conflict_recovery_to_next_photo': True, 'paired_collaborator_computer_old_shared_link': True,
                  'paired_collaborator_pointer_edit_and_real_save': True, 'javascript_errors': errors}
        result.update(desktop_undo_after_reload=True, phone_undo_saved_decision=True)
        (OUT / 'multi-reviewer-browser-checks.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result))
    finally:
        server.should_exit = True
        thread.join(timeout=10)
