"""Exercise desktop + phone review against disposable data with real Chromium.

No reviews are written to the real BrakeCheck dataset.
"""
from pathlib import Path
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.test_review import build_review_fixture
from bcdata.review_server import lan_addresses


def wait_state(page, expression, timeout=20):
    # Use DevTools evaluation instead of wait_for_function's injected eval;
    # production CSP deliberately forbids unsafe-eval.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if page.evaluate(expression):
            return
        time.sleep(.15)
    raise AssertionError(f"Browser state timed out: {expression}")


def tap_point(page, x, y):
    canvas = page.locator("#canvas")
    canvas.scroll_into_view_if_needed()
    box = canvas.bounding_box()
    canvas.tap(position={"x": x / 1164 * box["width"], "y": y / 874 * box["height"]})


def auto_confirm(page):
    # Exercise the actual custom dialog; accept routine prompts in this harness.
    page.add_init_script("""
        new MutationObserver(() => {
            const dialog = document.getElementById('confirm-dialog');
            if (dialog?.open && window.manualConfirm !== true)
                document.getElementById('confirm-accept').click();
        }).observe(document, {subtree: true, attributes: true, attributeFilter: ['open']});
    """)


def main():
    from playwright.sync_api import sync_playwright
    run_dir = ROOT / "test-results" / ("browser-" + uuid.uuid4().hex[:8])
    run_dir.mkdir(parents=True)
    settings = build_review_fixture(run_dir / "data")
    model_edges = {"left": {"status": "unknown", "kind": "other", "points": []},
                   "right": {"status": "visible", "kind": "other", "points": [[1000, 650], [850, 530], [710, 425]]}}
    for image_id in ("sample-1", "sample-2"):
        candidate = settings.path("lane_geometry", "teachers", "sam3", image_id + ".json")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(json.dumps({**json.loads(settings.path("lane_geometry", image_id + ".json").read_text()),
                                        "teacher": "sam3", "teacher_version": "sam3:browser-fixture", "source_guided": False,
                                        "road_edges": model_edges, "model_meta": {"reference_inputs": [], "model_forwards": 8}}))
    settings.path("licenses").mkdir(exist_ok=True)
    settings.path("licenses", "sam3-LICENSE.txt").write_text("browser fixture license text")
    sample = ROOT.parent.parent / "BrakeCheck_Dataset/imported/BrakeCheck_Dataset/exports/semantic_segmentation/BrakeCheck_RoadLane/images/test/comma10k-029e896d45961892.png"
    if sample.exists():
        import shutil
        for index in range(3):
            shutil.copyfile(sample, settings.path("raw", f"sample-{index}.png"))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    log = (run_dir / "server.log").open("w", encoding="utf-8")
    process = subprocess.Popen([sys.executable, str(ROOT / "review.py"), "serve", "--data-root", str(settings.root), "--port", str(port)],
                               cwd=ROOT, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=1).close()
                break
            except OSError:
                if process.poll() is not None:
                    raise RuntimeError((run_dir / "server.log").read_text())
                time.sleep(.25)
        else:
            raise RuntimeError("Test review server did not start")
        errors = []
        with sync_playwright() as p:
            executable = next((Path(x) for x in [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe"] if Path(x).exists()), None)
            browser = p.chromium.launch(executable_path=str(executable) if executable else None, headless=True)
            desktop = browser.new_context(viewport={"width": 1440, "height": 1100})
            pc = desktop.new_page()
            pc.on("pageerror", lambda error: errors.append(str(error)))
            auto_confirm(pc)
            pc.goto(base)
            wait_state(pc, "imageReady && current.image_id === 'sample-2'")
            pc.locator("#review-target").select_option("both")
            pc.evaluate("""() => {
                window.manualConfirm = true;
                window.confirmResult = null;
                confirmAction('Remember this test confirmation?').then(v => window.confirmResult = v);
            }""")
            pc.locator("#confirm-dialog").wait_for(state="visible")
            pc.locator("#confirm-remember").check()
            pc.locator("#confirm-cancel").click()
            assert pc.evaluate("confirmResult === false && storageGet('bc-confirm-hidden:Remember this test confirmation?') === null")
            pc.evaluate("() => { confirmAction('Remember this test confirmation?').then(v => window.confirmResult = v); }")
            pc.locator("#confirm-dialog").wait_for(state="visible")
            pc.locator("#confirm-remember").check()
            pc.locator("#confirm-accept").click()
            assert pc.evaluate("confirmResult === true && storageGet('bc-confirm-hidden:Remember this test confirmation?') === '1'")
            pc.reload()
            wait_state(pc, "imageReady && current.image_id === 'sample-2'")
            assert pc.evaluate("confirmAction('Remember this test confirmation?')") is True
            assert pc.locator("#confirm-dialog").is_hidden()
            pc.evaluate("() => { window.manualConfirm = true; confirmAction('A different confirmation?').then(v => window.confirmResult = v); }")
            pc.locator("#confirm-dialog").wait_for(state="visible")
            pc.keyboard.press("Escape")
            assert pc.evaluate("confirmResult === false")
            pc.evaluate("toast('Test notification preference', true)")
            pc.locator("#toast button").click()
            pc.evaluate("toast('Test notification preference', true)")
            assert pc.locator("#toast").is_hidden()
            assert pc.locator("#save-status").inner_text() == "Test notification preference"
            pc.locator("#label-guide summary").click()
            pc.locator("#reset-messages").click()
            assert pc.evaluate("storageGet('bc-confirm-hidden:Remember this test confirmation?') === null && storageGet('bc-toast-hidden:Test notification preference') === null")
            pc.locator("#label-guide summary").click()
            pc.evaluate("window.manualConfirm = false")
            pc.screenshot(path=str(run_dir / "desktop.png"), full_page=True)
            host = pc.request.get(base + "/api/host").json()
            pc.get_by_role("button", name="Pair phone", exact=True).click()
            wait_state(pc, "document.getElementById('pair-qr').complete && document.getElementById('pair-qr').naturalWidth > 0")
            pc.screenshot(path=str(run_dir / "pairing.png"))
            pc.locator("#pair-dialog .close-dialog").click()
            address = lan_addresses()[0]
            phone_base = f"http://{address}:{port}"
            mobile = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, device_scale_factor=2)
            phone = mobile.new_page()
            phone.on("pageerror", lambda error: errors.append(str(error)))
            phone.on("dialog", lambda dialog: dialog.accept())
            auto_confirm(phone)
            phone.goto(phone_base + "/#pair=" + host["code"])
            phone.locator("#pair-screen").wait_for(state="visible")
            assert phone.locator("#pair-code").input_value() == host["code"]
            phone.locator("#reviewer-name").fill("Phone tester")
            phone.get_by_role("button", name="Connect phone").click()
            wait_state(phone, "imageReady && current.image_id === 'sample-1'")
            phone.locator("#review-target").select_option("both")
            assert phone.locator("#pair-phone").is_hidden()
            assert phone.request.get(phone_base + "/api/host").status == 403
            assert phone.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            phone.screenshot(path=str(run_dir / "phone-review.png"), full_page=True)
            phone.locator("#show-teacher").check()
            phone.locator("#model-options").wait_for(state="visible")
            assert phone.locator("#model-select").input_value() == "sam3"
            phone.get_by_role("button", name="Use model lanes for correction", exact=True).click()
            wait_state(phone, "editing && lanes.length === 1")
            before = phone.evaluate("lanes[0].points[0].slice()")
            phone.locator("#canvas").scroll_into_view_if_needed()
            box = phone.locator("#canvas").bounding_box()
            x = box["x"] + before[0] / 1164 * box["width"]
            y = box["y"] + before[1] / 874 * box["height"]
            phone.mouse.move(x, y)
            phone.mouse.down()
            phone.mouse.move(x - 12, y - 4, steps=5)
            phone.mouse.up()
            after = phone.evaluate("lanes[0].points[0].slice()")
            assert after != before, (before, after)
            phone.locator("#undo-edit").click()
            assert phone.evaluate("lanes[0].points[0].slice()") == before
            phone.locator("#redo-edit").click()
            assert phone.evaluate("lanes[0].points[0].slice()") == after
            phone.keyboard.press("Control+z")
            assert phone.evaluate("lanes[0].points[0].slice()") == before
            phone.keyboard.press("Control+Shift+z")
            assert phone.evaluate("lanes[0].points[0].slice()") == after
            phone.get_by_role("button", name="Use model road edges", exact=True).click()
            assert phone.evaluate("lanes[0].points[0].slice()") == after
            assert phone.evaluate("editingEdgeCandidate === 'sam3' && roadEdges.right.points.length === 3")
            phone.locator("#undo-edit").click()
            assert phone.evaluate("editingEdgeCandidate === null && roadEdges.right.status === 'not_reviewed'")
            phone.locator("#redo-edit").click()
            assert phone.evaluate("editingEdgeCandidate === 'sam3'")
            phone.locator("#undo-edit").click()
            phone.get_by_role("tab", name="Road edges", exact=True).click()
            assert phone.locator("#edge-status").input_value() == "not_reviewed"
            assert phone.evaluate("mode === 'move' && roadEdges.right.status === 'not_reviewed' && roadEdges.left.status === 'not_reviewed'")
            phone.locator("#edge-right").click()
            assert phone.locator("#edge-status").input_value() == "visible"
            assert phone.evaluate("mode === 'new' && roadEdges.right.points.length === 0 && roadEdges.left.status === 'not_reviewed'")
            phone.get_by_role("button", name="Save correction", exact=True).click()
            assert phone.request.get(phone_base + "/api/images/sample-1").json()["revision"] == 0
            assert phone.evaluate("current.image_id === 'sample-1' && editing && !pendingRequest")
            phone.locator("#edge-right").click()
            phone.locator("#edge-kind").select_option("curb")
            for point in ((1010, 650), (850, 540), (700, 430)):
                tap_point(phone, *point)
            initial_edge = phone.evaluate("copy(roadEdges.right.points)")
            tap_point(phone, 780, 490)  # Last inserted node is in the middle of the polyline.
            assert phone.evaluate("roadEdges.right.points.length") == 4
            phone.get_by_role("button", name="Undo last point", exact=True).click()
            assert phone.evaluate("copy(roadEdges.right.points)") == initial_edge
            phone.locator("#undo-edit").click()
            assert phone.evaluate("roadEdges.right.points.length") == 4
            phone.locator("#redo-edit").click()
            assert phone.evaluate("copy(roadEdges.right.points)") == initial_edge
            phone.locator("#finish-lane").click()
            phone.locator("#pan-mode").click()
            assert phone.evaluate("mode === 'pan'")
            phone.locator("#pan-mode").click()
            phone.locator("#edge-left").click()
            assert phone.locator("#edge-status").input_value() == "visible"
            phone.locator("#edge-status").select_option("not_reviewed")
            phone.get_by_role("tab", name="Painted lines", exact=True).click()
            phone.get_by_role("tab", name="Road edges", exact=True).click()
            assert phone.locator("#edge-status").input_value() == "not_reviewed"
            phone.locator("#edge-status").select_option("unknown")
            phone.locator("#edge-right").click()
            phone.locator("#edge-left").click()
            assert phone.locator("#edge-status").input_value() == "unknown"
            assert phone.evaluate("lanes[0].points[0].slice()") == after
            phone.locator("#no-lanes").click()
            assert phone.evaluate("lanes.length === 0 && roadEdges.right.points.length === 3")
            phone.locator("#undo-edit").click()
            assert phone.evaluate("lanes.length === 1 && roadEdges.right.points.length === 3")
            phone.locator("#notes").fill("Road edges reviewed separately; left is hidden.")
            phone.reload()
            wait_state(phone, "imageReady && current.image_id === 'sample-1' && !busy")
            phone.locator("#draft-banner").wait_for(state="visible")
            assert phone.evaluate("!editing")
            phone.get_by_role("button", name="Restore draft", exact=True).click()
            assert phone.evaluate("copy(roadEdges.right.points)") == initial_edge
            assert phone.evaluate("roadEdges.left.status") == "unknown"
            assert phone.evaluate("lanes[0].points[0].slice()") == after
            assert phone.locator("#notes").input_value().startswith("Road edges reviewed")
            assert phone.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            phone.screenshot(path=str(run_dir / "phone-correction.png"), full_page=True)

            def lose_saved_response(route):
                response = route.fetch()  # Commit on the PC, then simulate a lost Wi-Fi response.
                assert response.ok
                route.abort("failed")

            phone.route("**/api/reviews", lose_saved_response)
            phone.get_by_role("button", name="Save correction", exact=True).click()
            wait_state(phone, "Boolean(pendingRequest) && !busy")
            assert phone.locator("#skip").is_disabled()
            assert phone.locator("#edge-status").is_disabled()
            assert phone.locator("#notes").is_disabled()
            committed = phone.request.get(phone_base + "/api/images/sample-1").json()
            assert committed["review_status"] == "corrected"
            # Recovery must find a committed image even though it is no longer pending in the queue.
            phone.unroute("**/api/reviews", lose_saved_response)
            phone.reload()
            wait_state(phone, "imageReady && current.image_id === 'sample-1' && !busy")
            phone.get_by_role("button", name="Restore draft", exact=True).click()
            assert phone.evaluate("Boolean(pendingRequest)")
            phone.get_by_role("button", name="Save correction", exact=True).click()
            wait_state(phone, "imageReady && current.image_id === 'sample-0'")
            saved = phone.request.get(phone_base + "/api/images/sample-1").json()
            assert saved["revision"] == committed["revision"]  # Retry is idempotent.
            assert saved["review_status"] == "corrected"
            assert saved["geometry"]["reviewer"] == "Phone tester"
            assert saved["geometry"]["candidate_provenance"]["name"] == "sam3"
            assert saved["geometry"]["lanes"][0]["polyline_points"][0] == after
            assert saved["geometry"]["road_edges"]["right"]["points"] == initial_edge
            assert saved["geometry"]["road_edges"]["left"]["status"] == "unknown"
            phone.get_by_role("button", name="Exclude photo", exact=True).click()
            wait_state(phone, "current === null && !busy")
            rejected = phone.request.get(phone_base + "/api/images/sample-0").json()
            assert rejected["review_status"] == "rejected"
            phone.get_by_role("button", name="Undo last decision", exact=True).click()
            wait_state(phone, "imageReady && current.image_id === 'sample-0' && !busy")
            assert phone.request.get(phone_base + "/api/images/sample-0").json()["review_status"] == "none"
            phone.route("**/api/reviews", lose_saved_response)
            phone.get_by_role("button", name="Human approve", exact=True).click()
            wait_state(phone, "Boolean(pendingRequest) && !busy")
            approval = phone.request.get(phone_base + "/api/images/sample-0").json()
            assert approval["review_status"] == "approved"
            phone.unroute("**/api/reviews", lose_saved_response)
            phone.reload()
            wait_state(phone, "imageReady && current.image_id === 'sample-0' && !busy")
            phone.get_by_role("button", name="Restore draft", exact=True).click()
            assert phone.evaluate("!editing && pendingRequest.action === 'approved'")
            assert phone.get_by_role("button", name="Human approve", exact=True).is_visible()
            phone.get_by_role("button", name="Human approve", exact=True).click()
            wait_state(phone, "current === null && !busy")
            assert phone.request.get(phone_base + "/api/images/sample-0").json()["revision"] == approval["revision"]
            pc.on("dialog", lambda dialog: dialog.accept())
            pc.locator("#show-teacher").check()
            pc.get_by_role("button", name="Use model road edges", exact=True).click()
            assert pc.evaluate("editingCandidate === null && lanes.length === 1 && editingEdgeCandidate === 'sam3'")
            # Imported/model points can exceed image bounds. Repair both layers
            # explicitly, preserve undo, and verify the repaired save below.
            pc.evaluate("""() => {
                lanes[0].points[0][0] = canvas.width + 10;
                roadEdges.right.points[0] = [-20, canvas.height + 20];
                roadEdges.right.points[1][1] = canvas.height + 30;
                changed();
            }""")
            before_fix = pc.evaluate("({lanes: copy(lanes), edges: copy(roadEdges)})")
            pc.get_by_role("button", name="Auto-fix points", exact=True).click()
            assert pc.evaluate("lanes[0].points[0][0] === canvas.width - 1 && roadEdges.right.points.length === 2 && roadEdges.right.points[0][0] === 0 && roadEdges.right.points[0][1] === canvas.height - 1")
            fixed_points = pc.evaluate("({lanes: copy(lanes), edges: copy(roadEdges)})")
            pc.locator("#undo-edit").click()
            assert pc.evaluate("({lanes: copy(lanes), edges: copy(roadEdges)})") == before_fix
            # Leave invalid points restored: Save must now repair automatically.
            pc.get_by_role("button", name="Save correction", exact=True).click()
            wait_state(pc, "current === null && !busy")
            pc_saved = pc.request.get(base + "/api/images/sample-2").json()["geometry"]
            assert pc_saved["lanes"][0]["polyline_points"] == fixed_points["lanes"][0]["points"]
            assert pc_saved["road_edges"]["right"]["points"] == fixed_points["edges"]["right"]["points"]
            assert pc_saved["road_edge_candidate_provenance"]["name"] == "sam3"
            assert "candidate_provenance" not in pc_saved
            for _ in range(30):
                stats = pc.request.get(base + "/api/stats").json()
                if stats["export"]["state"] == "ready" and stats["export"]["revision"] == stats["revision"]:
                    break
                time.sleep(.4)
            assert stats["pending"] == 0 and stats["accepted"] == 3
            assert stats["export"]["validation"]["ok"] and stats["export"]["counts"] == {"train":1,"val":1,"test":1}, stats
            assert stats["export"]["road_edges"]["exported"] == 2
            assert stats["export"]["road_edges"]["training_ready"] is False
            edge_export = Path(stats["export"]["road_edges"]["out"])
            edge_records = [json.loads(line) for line in (edge_export / "annotations.jsonl").read_text().splitlines()]
            edge_record = next(r for r in edge_records if r["image_id"] == "sample-1")
            assert edge_record["road_edges"]["left"]["training_mask"] is False
            assert edge_record["road_edges"]["right"]["points"] == initial_edge
            adopted_record = next(r for r in edge_records if r["image_id"] == "sample-2")
            assert adopted_record["human_review"]["road_edge_candidate_provenance"]["name"] == "sam3"
            assert not errors, errors
            browser.close()
        report = {"passed": True, "checks": ["desktop image display", "LAN phone pairing", "no duplicate review assignment", "phone viewport has no horizontal overflow", "touch controls", "model comparison and adoption", "editable point drag", "undo edit and redo", "keyboard undo shortcuts", "touch road-edge tracing", "undo last inserted point", "road-edge visibility states", "pan mode", "no-painted-lines preserves road edges", "draft recovery across refresh", "lost-save response recovery across refresh", "idempotent network retry", "saved correction and model provenance", "exclude photo with confirmation", "undo saved decision", "human approval", "separate reviewed-only painted and road-edge exports", "unknown edge ignored in training targets", "no browser JavaScript errors"], "screenshots": str(run_dir), "real_dataset_reviews_modified": False}
        (run_dir / "verification.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()


if __name__ == "__main__":
    main()
