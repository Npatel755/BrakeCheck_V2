"""Disposable experiment copies: authentication, isolation and stale-decision guards."""
from pathlib import Path
import hashlib
import json
import sqlite3

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from bcdata.experiment_review import create_experiment_router
from bcdata.proposal_policy import proposal_approval_policy


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def experiment(tmp_path):
    run = tmp_path / "experiments/flagged_teacher/run-20261020-test"
    run.mkdir(parents=True)
    write_json(run / "experiment.json", {"phase": "teacher_training", "base_counts": {"train": 13086}})
    original = tmp_path / "metadata.sqlite"
    con = sqlite3.connect(original)
    con.execute("create table original_reviews(image_id text, action text)")
    con.execute("insert into original_reviews values('original-1','corrected')")
    con.commit(); con.close()
    copies = []
    for index, split in enumerate(["train", "val", "test"]):
        copy_id = f"copy-{index}"
        image_path = run / f"copies/images/{copy_id}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 32), (25, 40, 30)).save(image_path)
        geometry = {"width": 64, "height": 32, "lanes": [{"slot": 2, "points": [[10, 30], [25, 15]]}]}
        write_json(run / f"copies/original_annotations/{copy_id}.json", geometry)
        write_json(run / f"proposals/{copy_id}.json", {
            "geometry": geometry, "teacher_provenance": {"name": "fixture-teacher"},
            "quality": {"eligible_for_experiment": True, "reasons": []}})
        copies.append({"copy_id": copy_id, "original_image_id": f"original-{index}",
                       "split": split, "source": "a2d2", "session_id": f"drive-{index}",
                       "width": 64, "height": 32, "image_path": f"copies/images/{copy_id}.png",
                       "original_geometry_path": f"copies/original_annotations/{copy_id}.json"})
    write_json(run / "copies.json", {"schema": "flagged_model_copies:v1", "items": copies})
    app = FastAPI()

    def require_session(request: Request):
        if request.headers.get("x-fixture-session") != "paired":
            raise HTTPException(401, "Pair first")
        return {"reviewer": "Authenticated Nick", "owner": "test-session"}

    app.include_router(create_experiment_router(tmp_path, require_session))
    with TestClient(app) as client:
        yield {"client": client, "run": run, "root": tmp_path,
               "api": "/api/experiments/flagged/" + run.name,
               "headers": {"x-fixture-session": "paired"}, "copies": copies,
               "original_hash": hashlib.sha256(original.read_bytes()).hexdigest()}


def get(experiment, suffix):
    return experiment["client"].get(experiment["api"] + suffix, headers=experiment["headers"])


def body(experiment, action="approve", revision=0, event="event-0001", copy_id="copy-0"):
    detail = get(experiment, f"/copies/{copy_id}").json()
    return {"copy_id": copy_id, "action": action, "event_id": event,
            "revision": revision, "proposal_sha256": detail["proposal_sha256"]}


def post(experiment, data):
    return experiment["client"].post(experiment["api"] + "/decision", json=data, headers=experiment["headers"])


def test_every_route_requires_existing_authenticated_session(experiment):
    client = experiment["client"]
    for method, path in [("GET", ""), ("GET", "/latest"), ("GET", "/" + experiment["run"].name + "/status"),
                         ("GET", "/" + experiment["run"].name + "/queue"),
                         ("GET", "/" + experiment["run"].name + "/copies/copy-0"),
                         ("GET", "/" + experiment["run"].name + "/copies/copy-0/image"),
                         ("POST", "/" + experiment["run"].name + "/decision")]:
        response = client.request(method, "/api/experiments/flagged" + path,
                                  json=body(experiment) if method == "POST" else None)
        assert response.status_code == 401
    assert not (experiment["run"] / "review.sqlite").exists()


def test_read_routes_do_not_create_review_db_or_change_originals(experiment):
    assert get(experiment, "/status").json()["counts"]["copies"] == 3
    result = get(experiment, "/queue").json()
    assert [item["copy_id"] for item in result["items"]] == ["copy-0"]
    assert get(experiment, "/copies/copy-0").json()["original_geometry"]["lanes"]
    image = get(experiment, "/copies/copy-0/image")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert not (experiment["run"] / "review.sqlite").exists()
    assert hashlib.sha256((experiment["root"] / "metadata.sqlite").read_bytes()).hexdigest() == experiment["original_hash"]


@pytest.mark.parametrize("action", ["approve", "exclude", "skip"])
def test_decisions_are_isolated_and_session_owned(experiment, action):
    request = {**body(experiment, action=action), "reviewer": "Forged reviewer"}
    result = post(experiment, request)
    assert result.status_code == 200 and result.json()["original_reviews_unchanged"]
    with sqlite3.connect(experiment["run"] / "review.sqlite") as con:
        assert con.execute("select action,reviewer,revision from decisions").fetchone() == (action, "Authenticated Nick", 1)
        assert con.execute("select count(*) from decision_events").fetchone()[0] == 1
    assert get(experiment, "/queue").json()["items"] == []
    assert len(get(experiment, f"/queue?status={action}").json()["items"]) == 1
    assert hashlib.sha256((experiment["root"] / "metadata.sqlite").read_bytes()).hexdigest() == experiment["original_hash"]


def test_retry_is_idempotent_and_event_cannot_change_action(experiment):
    request = body(experiment)
    assert post(experiment, request).status_code == 200
    retry = post(experiment, request)
    assert retry.status_code == 200 and retry.json()["idempotent"] is True
    assert post(experiment, {**request, "action": "exclude"}).status_code == 409
    with sqlite3.connect(experiment["run"] / "review.sqlite") as con:
        assert con.execute("select count(*) from decision_events").fetchone()[0] == 1


def test_stale_revision_and_changed_proposal_are_rejected(experiment):
    request = body(experiment)
    assert post(experiment, request).status_code == 200
    assert post(experiment, {**request, "event_id": "event-0002"}).status_code == 409
    path = experiment["run"] / "proposals/copy-0.json"
    proposal = json.loads(path.read_text()); proposal["teacher_provenance"]["name"] = "new-teacher"
    write_json(path, proposal)
    assert post(experiment, {**request, "revision": 1, "event_id": "event-0003"}).status_code == 409


@pytest.mark.parametrize("reason", ["unsupported_curve", "low_paint_confidence", "short_observed_paint_span",
                                    "flip_prediction_disagreement", "ambiguous_lane_geometry"])
def test_human_can_accept_advisory_quality_without_changing_proposal_or_originals(experiment, reason):
    path = experiment["run"] / "proposals/copy-0.json"
    proposal = json.loads(path.read_text()); proposal["quality"] = {"eligible_for_experiment": False, "reasons": [reason]}
    write_json(path, proposal)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    detail = get(experiment, "/copies/copy-0").json()
    assert not detail["quality_passes"] and detail["approval_allowed"] and detail["approval_overrides_quality"]
    assert detail["approval_block_reason"] is None
    request = body(experiment)
    assert post(experiment, request).status_code == 200
    assert post(experiment, request).json()["idempotent"]
    assert get(experiment, "/copies/copy-0").json()["decision"]["action"] == "approve"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert hashlib.sha256((experiment["root"] / "metadata.sqlite").read_bytes()).hexdigest() == experiment["original_hash"]


@pytest.mark.parametrize("mutation", [
    {"geometry": {"width": 64, "height": 32, "lanes": []}},
    {"error": "Prediction failed"},
    {"geometry": {"width": 63, "height": 32, "lanes": [{"slot": 2, "points": [[10, 30], [25, 15]]}]}},
    {"geometry": {"lanes": [None]}},
    {"geometry": {"lanes": [{"slot": 2, "points": [[-1, 30], [25, 15]]}]}},
    {"geometry": {"lanes": [{"slot": 2, "points": [[64, 30], [25, 15]]}]}},
    {"geometry": {"lanes": [{"slot": 2, "points": [[10, 32], [25, 15]]}]}},
    {"geometry": {"lanes": [{"slot": 2, "points": [[10, 15], [25, 15]]}]}},
    {"geometry": {"lanes": [{"slot": 2, "points": [["ten", 30], [25, 15]]}]}},
    {"geometry": {"lanes": [{"slot": True, "points": [[10, 30], [25, 15]]}]}},
    {"geometry": {"lanes": [{"slot": 2, "physical": False, "points": [[10, 30], [25, 15]]}]}},
    {"geometry": {"lanes": [{"slot": 2, "kind": "road_edge", "points": [[10, 30], [25, 15]]}]}},
    {"geometry": {"lanes": [{"slot": 2, "kind": [], "points": [[10, 30], [25, 15]]}]}},
    {"geometry": {"lanes": [{"slot": 2, "points": [[10, 30], [25, 15]]}, {"slot": 2, "points": [[40, 30], [30, 15]]}]}},
    {"geometry": {"lanes": [{"slot": 2, "points": [[10, 30]]}]}},
    {"quality": {"eligible_for_experiment": False, "reasons": ["prediction_error"]}},
    {"quality": {"eligible_for_experiment": False, "reasons": ["unknown_new_error"]}},
    {"quality": {"eligible_for_experiment": False, "reasons": []}},
    {"quality": None},
])
def test_invalid_proposal_cannot_be_approved_even_when_automatic_qc_claims_pass(experiment, mutation):
    path = experiment["run"] / "proposals/copy-0.json"
    proposal = json.loads(path.read_text()); proposal.update(mutation)
    write_json(path, proposal)
    detail = get(experiment, "/copies/copy-0").json()
    assert not detail["approval_allowed"] and detail["approval_block_reason"]
    assert not detail["approval_overrides_quality"]
    assert post(experiment, body(experiment)).status_code == 422
    assert not (experiment["run"] / "review.sqlite").exists()
    assert post(experiment, body(experiment, action="exclude")).status_code == 200


@pytest.mark.parametrize("bad_number", [float("nan"), float("inf"), -float("inf"), 10 ** 500, True])
def test_shared_policy_rejects_nonfinite_or_boolean_coordinates_without_importing_worker_dependencies(bad_number):
    proposal = {"geometry": {"width": 64, "height": 32, "lanes": [{"slot": 2, "points": [[bad_number, 30], [25, 15]]}]},
                "quality": {"eligible_for_experiment": True, "reasons": []}}
    policy = proposal_approval_policy(proposal)
    assert not policy["approval_allowed"] and policy["block_reason"]


def test_automatic_experiment_is_not_gated_by_optional_decisions(experiment):
    status = get(experiment, "/status").json()
    assert "do not wait for individual approvals" in status["policy"]
    assert status["counts"]["approved"] == 0
    assert get(experiment, "/copies/copy-0").json()["quality_passes"]
    assert not (experiment["run"] / "review.sqlite").exists()


def test_fragmented_boundary_slots_are_advisory_for_a_human_but_not_an_automatic_quality_pass(experiment):
    path = experiment["run"] / "proposals/copy-0.json"
    proposal = json.loads(path.read_text())
    proposal["geometry"]["lanes"] = [
        {"slot": 1, "points": [[10, 10, True], [12, 15, True]]},
        {"slot": 2, "points": [[14, 20, True], [18, 30, True]]}]
    write_json(path, proposal)
    original_bytes = path.read_bytes()
    detail = get(experiment, "/copies/copy-0").json()
    assert not detail["quality_passes"]
    assert detail["proposal"]["quality"]["eligible_for_experiment"] is True
    assert detail["training_geometry"]["reasons"] == ["probable_same_boundary_multiple_slots"]
    assert detail["approval_allowed"] and detail["approval_overrides_quality"]
    assert post(experiment, body(experiment)).status_code == 200
    assert path.read_bytes() == original_bytes


def test_entirely_inferred_points_cannot_be_human_approved_as_observed_paint(experiment):
    path = experiment["run"] / "proposals/copy-0.json"
    proposal = json.loads(path.read_text())
    for point in proposal["geometry"]["lanes"][0]["points"]:
        point.append(False)
    write_json(path, proposal)
    assert not get(experiment, "/copies/copy-0").json()["approval_allowed"]
    assert post(experiment, body(experiment)).status_code == 422


def install_repaired_proposal(experiment):
    original_path = experiment["run"] / "proposals/copy-0.json"
    original = json.loads(original_path.read_text())
    original.update(input_sha256="b" * 64,
                    teacher_provenance={"name": "fixture-teacher", "checkpoint_sha256": "a" * 64})
    write_json(original_path, original)
    original_bytes = original_path.read_bytes()
    repaired = json.loads(original_bytes)
    repaired["geometry"]["hood_crop_policy"] = "bottom-connected-broad-band-v1"
    repaired["geometry"]["lanes"][0]["points"][0][0] = 12
    repaired["repair_provenance"] = {
        "policy": "bottom-connected-broad-band-v1",
        "original_proposal_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "input_sha256": "b" * 64, "checkpoint_sha256": "a" * 64}
    path = experiment["run"] / "proposals_hood_repaired/copy-0.json"
    write_json(path, repaired)
    return original_path, original_bytes, path, repaired


def test_repaired_proposal_is_preferred_without_overwriting_original_or_accepting_old_click(experiment):
    original, original_bytes, path, repaired = install_repaired_proposal(experiment)
    detail = get(experiment, "/copies/copy-0").json()
    assert detail["proposal"] == repaired
    assert detail["original_geometry"]["lanes"][0]["points"][0][0] == 10
    assert detail["proposal_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    old_click = {**body(experiment), "proposal_sha256": hashlib.sha256(original_bytes).hexdigest()}
    assert post(experiment, old_click).status_code == 409
    assert post(experiment, body(experiment)).status_code == 200
    assert original.read_bytes() == original_bytes
    assert hashlib.sha256((experiment["root"] / "metadata.sqlite").read_bytes()).hexdigest() == experiment["original_hash"]


@pytest.mark.parametrize("field", ["original_proposal_sha256", "input_sha256", "checkpoint_sha256", "policy"])
def test_stale_or_different_model_repairs_are_not_silently_used(experiment, field):
    original, original_bytes, path, repaired = install_repaired_proposal(experiment)
    repaired["repair_provenance"][field] = "different"
    write_json(path, repaired)
    assert get(experiment, "/copies/copy-0").status_code == 409
    assert original.read_bytes() == original_bytes
    assert not (experiment["run"] / "review.sqlite").exists()


def test_training_is_default_and_holdouts_remain_comparison_only(experiment):
    assert get(experiment, "/queue").json()["items"][0]["split"] == "train"
    assert get(experiment, "/queue?split=val").json()["items"][0]["split"] == "val"
    assert get(experiment, "/queue?split=test").json()["items"][0]["split"] == "test"
    assert get(experiment, "/queue?split=all&limit=12").json()["items"][-1]["split"] == "test"
    assert get(experiment, "/queue?split=all&after=copy-0").json()["items"][0]["copy_id"] == "copy-1"


def test_teacher_training_phase_works_before_copies_exist(experiment):
    (experiment["run"] / "copies.json").unlink()
    result = get(experiment, "/status")
    assert result.status_code == 200 and result.json()["metadata"]["phase"] == "teacher_training"
    assert result.json()["counts"]["copies"] == 0
    assert get(experiment, "/queue").json()["preparing_copies"]


@pytest.mark.parametrize("phase", ["building_training_snapshot", "training_ufld", "complete_awaiting_comparison_review"])
def test_frozen_snapshot_decisions_only_apply_to_future_rebuild(experiment, phase):
    write_json(experiment["run"] / "experiment.json", {"phase": phase})
    write_json(experiment["run"] / "ufld_training.json", {"epoch": 2, "epochs": 20})
    status = get(experiment, "/status").json()
    assert status["snapshot_frozen"] and status["decisions_apply_to"] == "future_rebuild_only"
    assert status["progress"]["ufld_training"]["epoch"] == 2
    assert post(experiment, body(experiment, action="exclude")).json()["decisions_apply_to"] == "future_rebuild_only"


def test_optional_progress_cannot_read_external_training_run(experiment):
    outside = experiment["root"] / "outside-run"
    write_json(outside / "status.json", {"secret": "not experiment data"})
    write_json(experiment["run"] / "experiment.json", {"phase": "training_ufld", "ufld_run": str(outside)})
    assert "ufld_run" not in get(experiment, "/status").json()["progress"]


@pytest.mark.parametrize("field", ["image_path", "original_geometry_path"])
def test_manifest_paths_cannot_escape_experiment(experiment, field):
    outside = experiment["root"] / "outside.json"
    write_json(outside, {"lanes": []})
    manifest = {"schema": "flagged_model_copies:v1", "items": experiment["copies"]}
    manifest["items"][0][field] = str(outside)
    write_json(experiment["run"] / "copies.json", manifest)
    route = "/copies/copy-0/image" if field == "image_path" else "/copies/copy-0"
    assert get(experiment, route).status_code == 400


def test_invalid_identifier_and_filter_are_rejected(experiment):
    assert get(experiment, "/copies/copy..outside").status_code == 400
    assert get(experiment, "/queue?status=original_approved").status_code == 400
    assert get(experiment, "/queue?split=other").status_code == 400
    assert post(experiment, {**body(experiment), "action": "correct_original"}).status_code == 400


def test_partial_proposal_and_pending_preparation_do_not_become_empty_negative(experiment):
    path = experiment["run"] / "proposals/copy-0.json"
    path.write_text('{"geometry":')
    assert get(experiment, "/copies/copy-0").status_code == 409
    assert not (experiment["run"] / "review.sqlite").exists()


@pytest.fixture
def experiment_browser(experiment):
    playwright = pytest.importorskip("playwright.sync_api")
    browser_path = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    if not browser_path.exists():
        pytest.skip("Optional local Edge phone verification")
    ui = Path(__file__).resolve().parents[1] / "review_ui"
    errors = []
    with playwright.sync_playwright() as tool:
        browser = tool.chromium.launch(executable_path=str(browser_path), headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            from urllib.parse import urlsplit
            request = route.request
            parsed = urlsplit(request.url)
            if parsed.path.startswith("/api/"):
                response = experiment["client"].request(request.method, parsed.path + ("?" + parsed.query if parsed.query else ""),
                                                        content=request.post_data, headers={**experiment["headers"], "content-type": "application/json"})
                route.fulfill(status=response.status_code, body=response.content,
                              content_type=response.headers.get("content-type", "application/json"))
            else:
                files = {"/experiment": ("experiment.html", "text/html"),
                         "/static/experiment.js": ("experiment.js", "application/javascript"),
                         "/static/experiment.css": ("experiment.css", "text/css"),
                         "/static/studio_nav.css": ("studio_nav.css", "text/css")}
                if parsed.path in files:
                    name, kind = files[parsed.path]
                    route.fulfill(body=(ui / name).read_bytes(), content_type=kind,
                                  headers={"Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'"})
                else:
                    route.fulfill(status=404, body="Missing fixture resource")

        page.route("**/*", serve)
        try:
            yield page, errors
        finally:
            browser.close()


def test_phone_page_renders_both_views_and_saves_isolated_decisions(experiment, experiment_browser):
        page, errors = experiment_browser
        page.goto("http://experiment.test/experiment")
        page.locator("#review").wait_for(state="visible")
        assert page.locator("canvas").count() == 2
        assert page.locator("#split").input_value() == "train"
        assert page.locator("#copy-title").inner_text() == "original-0"
        assert page.locator("#approve").is_enabled()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator("#skip").click()
        page.locator("#empty").wait_for(state="visible")
        page.select_option("#filter", "skip")
        page.locator("#review").wait_for(state="visible")
        page.locator("#approve").click()
        page.locator("#empty").wait_for(state="visible")
        page.select_option("#filter", "approve")
        page.locator("#review").wait_for(state="visible")
        assert "approve" in page.locator("#copy-meta").inner_text()
        assert not errors
        with sqlite3.connect(experiment["run"] / "review.sqlite") as con:
            assert con.execute("select action,revision from decisions").fetchone() == ("approve", 2)
        assert hashlib.sha256((experiment["root"] / "metadata.sqlite").read_bytes()).hexdigest() == experiment["original_hash"]


@pytest.mark.parametrize("case, can_approve", [("soft_hold", True), ("empty", False), ("error", False), ("malformed", False)])
def test_phone_approve_button_explains_soft_holds_and_blocks_invalid_predictions(experiment, experiment_browser, case, can_approve):
    path = experiment["run"] / "proposals/copy-0.json"
    proposal = json.loads(path.read_text())
    proposal["quality"] = {"eligible_for_experiment": False, "reasons": ["low_paint_confidence", "flip_prediction_disagreement"]}
    if case == "empty":
        proposal["geometry"]["lanes"] = []
    elif case == "error":
        proposal["error"] = "Fixture prediction failed"
    elif case == "malformed":
        proposal["geometry"]["lanes"] = [None]
    write_json(path, proposal)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    page, errors = experiment_browser
    page.goto("http://experiment.test/experiment")
    page.locator("#review").wait_for(state="visible")
    assert page.locator("canvas").count() == 2
    assert page.locator("#approve").is_enabled() is can_approve
    assert "approval-help" in page.locator("#approve").get_attribute("aria-describedby")
    assert "do not wait for individual approvals" in page.locator("#policy").inner_text()
    if can_approve:
        assert "advisory quality hold" in page.locator("#approval-help").inner_text()
        page.locator("#approve").click()
    else:
        detail = get(experiment, "/copies/copy-0").json()
        assert detail["approval_block_reason"] in page.locator("#approval-help").inner_text()
        assert "does not block the automatic experiment" in page.locator("#approval-help").inner_text()
        assert page.locator("#exclude").is_enabled()
        page.locator("#exclude").click()
    page.locator("#empty").wait_for(state="visible")
    assert not errors
    with sqlite3.connect(experiment["run"] / "review.sqlite") as con:
        assert con.execute("select action from decisions").fetchone()[0] == ("approve" if can_approve else "exclude")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert hashlib.sha256((experiment["root"] / "metadata.sqlite").read_bytes()).hexdigest() == experiment["original_hash"]
