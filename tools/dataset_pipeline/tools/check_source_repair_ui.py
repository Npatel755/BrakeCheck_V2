"""Check fresh repair-audit guidance on desktop/phone with disposable data."""
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
from bcdata.source_repair_audit import requeue_repairs
from tests.test_source_repair_audit import blocked_fixture
from tools.check_review_ui import auto_confirm, wait_state
from playwright.sync_api import sync_playwright


def main():
    run = ROOT / "test-results" / ("source-repair-" + uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    settings, store, _, repairs = blocked_fixture(run / "data")
    report = requeue_repairs(settings, repairs, sample_target=2, minimum_per_group=2, apply=True)
    settings.path('reports','ufld_queue.json').write_text(json.dumps({
        'state':'waiting','train_images':123,'val_images':23,'epochs':20}))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with (run / "server.log").open("w") as log:
        process = subprocess.Popen([sys.executable, str(ROOT / "review.py"), "serve", "--data-root", str(settings.root), "--port", str(port)],
                                   stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            for _ in range(80):
                try:
                    urllib.request.urlopen(base + "/api/health", timeout=1).close()
                    break
                except OSError:
                    time.sleep(.25)
            with sync_playwright() as playwright:
                executable = next((Path(path) for path in [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"] if Path(path).exists()), None)
                browser = playwright.chromium.launch(executable_path=str(executable) if executable else None)
                for mobile in (False, True):
                    context = browser.new_context(viewport={"width": 390 if mobile else 1600, "height": 900}, is_mobile=mobile, has_touch=mobile)
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    auto_confirm(page)
                    page.goto(base)
                    wait_state(page, "imageReady && !busy")
                    assert page.locator("#source-audit-summary").is_visible()
                    assert "Revised source conversions" in page.locator("#source-audit-heading").inner_text()
                    assert "fresh sample check" in page.locator("#source-audit-hint").inner_text()
                    assert "Some groups are held" not in page.locator("#source-audit-counts").inner_text()
                    assert "Next UFLDv2 benchmark" in page.locator('#milestones').inner_text()
                    assert "123 training images" in page.locator('#milestones').inner_text()
                    page.locator("#review-source-sample").click()
                    wait_state(page, "imageReady && !busy && $('status-filter').value === 'audit_sample'")
                    assert "fresh audit sample" in page.locator("#provenance").inner_text()
                    assert page.evaluate("current.image_id") in report["sample_ids"]
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(run / ("phone.png" if mobile else "desktop.png")), full_page=True)
                    if mobile:
                        # Decisions exist only in this disposable fixture. Verify
                        # the new risk cohort becomes eligible only after both.
                        for remaining in (1, 0):
                            page.locator("#approve").click()
                            wait_state(page, f"!busy && Number($('pending').textContent.replaceAll(',', '')) === {remaining}")
                        assert "3 source-validated without individual review" in page.locator("#source-audit-counts").inner_text()
                        assert "Revised source conversions" not in page.locator("#source-audit-heading").inner_text()
                    assert not errors, errors
                    context.close()
                browser.close()
            print(json.dumps({"result": "passed", "artifact": str(run), "checks": ["desktop/phone repair guidance", "fresh sample filter", "historical blocked warning hidden", "actual sample decisions gate release", "queued benchmark status"]}))
        finally:
            process.terminate()
            process.wait(timeout=15)


if __name__ == "__main__":
    main()
