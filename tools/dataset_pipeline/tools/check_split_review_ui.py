"""Desktop/phone regression: references cannot contaminate the editable canvas."""
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
from bcdata.db import Registry
from playwright.sync_api import sync_playwright


def main():
    import numpy as np
    from PIL import Image
    run = ROOT / "test-results" / ("split-" + uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    settings = build_review_fixture(run / "data")
    reg = Registry(settings.db_path)
    for index in range(3):
        image_id = f"sample-{index}"
        pixels = np.zeros((874, 1164), np.uint8)
        pixels[410:660, 500:510] = 255
        probability = settings.root / (image_id + "-paint.png")
        Image.fromarray(pixels).save(probability)
        metadata = settings.root / (image_id + "-fused.json")
        metadata.write_text(json.dumps({"version": "fixture", "maps": {"lane": str(probability)}}))
        reg.add_annotation(image_id, "fused", "fusion", "fixture", str(metadata))
        geometry = json.loads(settings.path("lane_geometry", image_id + ".json").read_text())
        candidate = settings.path("lane_geometry", "teachers", "sam3", image_id + ".json")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(json.dumps({**geometry, "teacher_version": "sam3:fixture", "source_guided": False}))
    reg.close()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with (run / "server.log").open("w") as log:
        server = subprocess.Popen([sys.executable, str(ROOT / "review.py"), "serve", "--data-root", str(settings.root), "--port", str(port)],
                                  stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            for _ in range(80):
                try:
                    urllib.request.urlopen(base + "/api/health", timeout=1).close(); break
                except OSError:
                    time.sleep(.25)
            with sync_playwright() as p:
                executable = next((Path(x) for x in [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"] if Path(x).exists()), None)
                browser = p.chromium.launch(executable_path=str(executable) if executable else None)
                for mobile in (False, True):
                    context = browser.new_context(viewport={"width": 390 if mobile else 1600, "height": 900},
                                                  is_mobile=mobile, has_touch=mobile)
                    page = context.new_page(); errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    auto_confirm(page)
                    page.goto(base)
                    wait_state(page, "imageReady && mask !== null")
                    assert page.locator("#show-mask").is_checked()
                    assert page.locator("#show-teacher").is_checked()
                    left, right = page.locator("#reference-canvas").bounding_box(), page.locator("#canvas").bounding_box()
                    assert left["y"] < right["y"] if mobile else left["x"] < right["x"] and abs(left["y"] - right["y"]) < 3
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    before = page.evaluate("({right:canvas.toDataURL(),left:$('reference-canvas').toDataURL()})")
                    page.locator("#show-mask").uncheck(); page.locator("#show-teacher").uncheck()
                    after = page.evaluate("({right:canvas.toDataURL(),left:$('reference-canvas').toDataURL()})")
                    assert before["right"] == after["right"] and before["left"] != after["left"]
                    page.locator("#show-labels").uncheck()
                    assert page.evaluate("canvas.toDataURL()") != after["right"]
                    page.locator("#show-labels").check()
                    page.locator("#show-mask").check(); page.locator("#show-teacher").check()
                    page.locator("#correct").click()
                    original = page.evaluate("JSON.stringify(lanes)")
                    page.locator("#reference-canvas").click(position={"x": 50, "y": 50})
                    assert page.evaluate("JSON.stringify(lanes)") == original
                    page.locator("#canvas").scroll_into_view_if_needed()
                    box = page.locator("#canvas").bounding_box()
                    point = page.evaluate("lanes[0].points[0].slice(0,2)")
                    x, y = box["x"] + point[0] / 1164 * box["width"], box["y"] + point[1] / 874 * box["height"]
                    page.mouse.move(x, y); page.mouse.down(); page.mouse.move(x + 12, y - 5, steps=5); page.mouse.up()
                    assert page.evaluate("lanes[0].points[0].slice(0,2)") != point
                    page.locator("#undo-edit").click()
                    assert page.evaluate("lanes[0].points[0].slice(0,2)") == point
                    page.locator("#zoom-in").click()
                    assert page.evaluate("$('canvas-wrap').style.width === $('reference-wrap').style.width")
                    page.evaluate("$('stage').scrollLeft = 30")
                    wait_state(page, "Math.abs($('stage').scrollLeft - $('reference-stage').scrollLeft) <= 1")
                    page.locator("#toggle-reference").click()
                    assert page.locator("#reference-pane").is_hidden()
                    page.locator("#toggle-reference").click()
                    assert page.locator("#reference-pane").is_visible()
                    assert not errors, errors
                    page.screenshot(path=str(run / ("phone.png" if mobile else "desktop.png")), full_page=True)
                    context.close()
                browser.close()
            print(json.dumps({"result": "passed", "artifact": str(run), "checks": ["desktop side by side", "phone stacked", "source/model isolated from painted lines", "reference read only", "point drag/undo", "shared zoom/scroll", "focus editing"]}))
        finally:
            server.terminate(); server.wait(timeout=15)


if __name__ == "__main__":
    main()
