"""Read-only desktop/phone check of the live dataset collection browser."""
import argparse
import json
from pathlib import Path
import time

from playwright.sync_api import expect, sync_playwright


def wait_for_assets(page, selector, *, image=False):
    """Check loaded assets without evaluating predicate strings under the app CSP."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        nodes = page.locator(selector)
        if nodes.count() == 3:
            if image:
                ready = all(nodes.nth(index).evaluate("node => node.complete && node.naturalWidth === 1280")
                            for index in range(3))
            else:
                ready = all(int(nodes.nth(index).get_attribute("width") or "0") > 1000
                            for index in range(3))
            if ready:
                return
        page.wait_for_timeout(100)
    raise AssertionError(f"Assets did not load: {selector}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path(__file__).resolve().parents[3] / "BrakeCheck_Dataset/reports/studio_datasets_ui"
    output.mkdir(parents=True, exist_ok=True)
    edge = next((p for p in [Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
                            Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe")] if p.is_file()), None)
    result = {"checked_at": time.time(), "read_only": True, "views": []}
    with sync_playwright() as tool:
        browser = tool.chromium.launch(executable_path=str(edge) if edge else None, headless=True)
        for mobile in (False, True):
            context = browser.new_context(viewport={"width": 390 if mobile else 1460, "height": 844 if mobile else 1000},
                                          is_mobile=mobile, has_touch=mobile)
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(args.url + "/datasets", wait_until="networkidle")
            page.locator("#content").wait_for(state="visible")
            payload = page.request.get(args.url + "/api/studio/datasets").json()
            expected = payload["base"]["train_images"]
            assert len(payload["variants"]) == 4
            assert all(item["base_train_images"] == expected for item in payload["variants"])
            assert page.locator('[role="tab"]').count() == 4
            assert page.locator("#base-train").inner_text() == f"{expected:,}"
            wait_for_assets(page, "#base-samples canvas")
            for variant in payload["variants"]:
                page.locator("#tab-" + variant["id"]).click()
                assert page.locator("#tab-" + variant["id"]).get_attribute("aria-selected") == "true"
                assert page.locator("#detail-title").inner_text() == variant["title"]
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), variant["id"]
            wait_for_assets(page, "#extra-preview img", image=True)
            if payload.get("simulation_import"):
                assert page.locator("#simulation-progress").is_visible()
            else:
                assert "not imported" in page.locator("#detail-counts").inner_text().lower()
            assert "TuSimple" in page.locator("#contents").inner_text()
            page.screenshot(path=str(output / ("phone.png" if mobile else "desktop.png")), full_page=True)
            page.locator("#tab-converted_plus_simulated").focus()
            page.keyboard.press("Home")
            assert page.locator("#tab-converted_only").get_attribute("aria-selected") == "true"
            page.keyboard.press("ArrowRight")
            assert page.locator("#tab-converted_plus_proposals").get_attribute("aria-selected") == "true"
            # Read-only response fixtures check labels for later experiment phases.
            future = json.loads(json.dumps(payload))
            future["active_experiment"] = {"phase": "training_ufld", "teacher_progress": {"epoch": 8},
                                           "training_progress": {"epoch": 3}}
            proposal = next(item for item in future["variants"] if item["id"] == "converted_plus_proposals")
            proposal.update(training_ready=True, extra_train_images=123,
                            total_train_images=expected + 123, state="ready", state_label="Training in progress")
            page.route("**/api/studio/datasets", lambda route: route.fulfill(json=future))
            page.get_by_role("button", name="Refresh status", exact=True).click()
            expect(page.locator("#progress")).to_contain_text("epoch 3")
            assert "epoch 8" not in page.locator("#progress").inner_text()
            assert "final selection pending" not in page.locator("#detail-counts").inner_text()
            future["active_experiment"]["phase"] = "complete_awaiting_comparison_review"
            page.get_by_role("button", name="Refresh status", exact=True).click()
            expect(page.locator("#progress")).to_contain_text("Training complete")
            assert "epoch" not in page.locator("#progress").inner_text()
            future["overnight_queue"] = {"state": "running", "provisional": True,
                "jobs": [{"id": f"collection_{index + 2}", "collection_id": variant["id"],
                          "state": "running" if index == 1 else "complete" if index == 0 else "waiting_proposals",
                          "progress": {"epoch": 2, "epochs": 20} if index == 1 else {},
                          "metrics": {"best_val_f1": .75} if index == 0 else {},
                          "train_images": expected + 123, "extra_images": 123}
                         for index, variant in enumerate(future["variants"][1:])]}
            for variant in future["variants"][1:]:
                variant.update(extra_train_images=0, total_train_images=expected, training_ready=False)
            page.get_by_role("button", name="Refresh status", exact=True).click()
            expect(page.locator("#overnight-panel")).to_be_visible()
            expect(page.locator("#overnight-jobs article")).to_have_count(3)
            expect(page.locator("#overnight-jobs")).to_contain_text("75.0%")
            expect(page.locator("#overnight-jobs")).to_contain_text("Epoch 2 / 20")
            assert page.locator("#base-train").inner_text() == f"{expected:,}"
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(output / ("phone-queue.png" if mobile else "desktop-queue.png")), full_page=True)
            assert not errors, errors
            result["views"].append({"mobile": mobile, "base_train_images": expected, "collections": 4,
                                     "base_photos": 3, "simulation_samples": 3,
                                     "future_phase_labels": "passed",
                                     "provisional_queue": "passed",
                                     "horizontal_overflow": False, "javascript_errors": errors})
            context.close()
        browser.close()
    (output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
