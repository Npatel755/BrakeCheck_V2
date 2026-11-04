"""Real browser batch checks; writes only disposable fixture reviews."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.test_review import build_review_fixture
from tools.check_review_ui import wait_state, auto_confirm
from playwright.sync_api import sync_playwright


def main():
    run = ROOT / "test-results" / ("batch-" + uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    settings = build_review_fixture(run / "data")
    for index in range(3):
        image_id = f"sample-{index}"
        source = settings.path("lane_geometry", image_id + ".json")
        candidate = {**json.loads(source.read_text()), "teacher": "dinov3_seg", "teacher_version": "dinov3_seg:browser-fixture",
                     "source_guided": False, "model_meta": {"checkpoint_sha256": "browser-checksum"}}
        target = settings.path("lane_geometry", "teachers", "dinov3_seg", image_id + ".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(candidate))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with (run / "server.log").open("w") as log:
        server = subprocess.Popen([sys.executable, str(ROOT / "review.py"), "serve", "--data-root", str(settings.root), "--port", str(port)], stdout=log, stderr=log,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            for _ in range(80):
                try:
                    urllib.request.urlopen(base + "/api/health", timeout=1).close()
                    break
                except OSError:
                    time.sleep(.25)
            with sync_playwright() as p:
                executable = next((Path(x) for x in [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"] if Path(x).exists()), None)
                browser = p.chromium.launch(executable_path=str(executable) if executable else None)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                auto_confirm(page)
                page.goto(base)
                wait_state(page, "imageReady && current.image_id === 'sample-2'")
                assert page.locator("#review-target").input_value() == "lanes"
                page.locator("#batch-review").click()
                wait_state(page, "document.querySelectorAll('#batch-grid input:not(:disabled)').length === 2")
                assert page.locator("#batch-grid input:checked").count() == 0
                page.locator("#batch-grid button").first.click()
                assert page.locator("#batch-preview").is_visible()
                page.locator("#batch-preview-close").click()
                page.locator("#batch-select").click()
                page.locator("#batch-approve").click()
                wait_state(page, "document.querySelector('#batch-status').textContent.includes('2 photos saved')")
                assert page.evaluate("async () => (await (await fetch('/api/stats')).json()).accepted") == 2
                page.locator("#batch-undo").click()
                wait_state(page, "document.querySelector('#batch-status').textContent.includes('Last batch undone')")
                assert page.evaluate("async () => (await (await fetch('/api/stats')).json()).accepted") == 0
                page.locator("#batch-load").click()
                wait_state(page, "document.querySelectorAll('#batch-grid input:not(:disabled)').length === 2")
                page.locator("#batch-select").click()
                # Lose a successful save response; reload and retry its exact UUID.
                lost = []
                def lose_response(route):
                    if not lost:
                        route.fetch()
                        lost.append(True)
                        route.abort()
                    else:
                        route.continue_()
                page.route("**/api/reviews", lose_response)
                page.locator("#batch-approve").click()
                wait_state(page, "document.querySelector('#batch-status').textContent.includes('saved requests will not be duplicated') && !document.querySelector('#batch-approve').disabled")
                page.unroute("**/api/reviews", lose_response)
                page.reload()
                wait_state(page, "document.querySelector('#batch-dialog').open && document.querySelector('#batch-approve').textContent.includes('Retry')")
                page.locator("#batch-approve").click()
                wait_state(page, "document.querySelector('#batch-status').textContent.includes('2 photos saved')")
                assert page.evaluate("async () => (await (await fetch('/api/stats')).json()).accepted") == 2
                from bcdata.review_store import ReviewStore
                with ReviewStore(settings).registry() as reg:
                    assert reg.count("SELECT count(*) FROM review_events WHERE action='approved'") == 4
                page.locator("#batch-undo").click()
                wait_state(page, "document.querySelector('#batch-status').textContent.includes('Last batch undone')")
                page.locator("#batch-model").select_option("dinov3_seg")
                wait_state(page, "document.querySelectorAll('#batch-grid input:not(:disabled)').length === 2")
                page.locator("#batch-select").click()
                page.locator("#batch-approve").click()
                wait_state(page, "document.querySelector('#batch-status').textContent.includes('2 photos saved')")
                for index in range(2):
                    geometry = ReviewStore(settings).detail(f"sample-{index}")["geometry"]
                    assert geometry["candidate_provenance"]["checkpoint_sha256"] == "browser-checksum"
                    assert geometry["model_licenses"] == ["dinov3", "dinov3_seg"]
                assert not errors, errors
                page.screenshot(path=str(run / "batch.png"), full_page=True)
                browser.close()
            print(json.dumps({"result": "passed", "checks": ["load", "explicit selection", "preview", "save", "undo", "lost response retry without duplicate decisions", "DINO batch adoption and license provenance"], "artifact": str(run)}))
        finally:
            server.terminate()
            server.wait(timeout=15)


if __name__ == "__main__":
    main()
