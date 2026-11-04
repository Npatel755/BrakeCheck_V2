"""V4 deployment gates use disposable fixtures, never production review data."""
from copy import deepcopy
import json
from pathlib import Path
import time
import uuid

import numpy as np
import pytest

from bcdata import repair_source_v4 as deployment
from bcdata.teachers.base import file_sha256
from tests.test_source_repair_audit import blocked_fixture, snapshot


def regression_evidence(tmp_path):
    evaluator = tmp_path / "disposable_evaluator.py"
    evaluator.write_text("# Disposable deployment evidence fixture\n")
    package = Path(deployment.__file__).resolve().parent
    code = package / "source_converter_v4.py"
    report = {
        "converter_version": deployment.VERSION,
        "converter_code_path": str(code), "converter_code_sha256": file_sha256(code),
        "dependency_sha256": {str((package / name).resolve()): file_sha256(package / name)
                              for name in deployment._DEPENDENCIES},
        "evaluator_code_path": str(evaluator), "evaluator_code_sha256": file_sha256(evaluator),
        "safety_checks": {"approved_sample_regressions": 0, "original_annotations_unchanged": True,
                          "converter_and_dependencies_stable": True, "source_fingerprints_verified": True,
                          "known_failures_resolved_or_flagged": True},
        "known_failure_checks": [{"image_id": "fixture-known-failure", "status": "passed"}],
        "unresolved_known_failures": [],
    }
    path = tmp_path / "regression.json"
    path.write_text(json.dumps(report))
    return path, report


@pytest.fixture
def staged_fixture(tmp_path, monkeypatch):
    settings, store, previous_samples, previous_repairs = blocked_fixture(tmp_path / "dataset")
    # The converter's algorithm has separate pixel-level tests. Here controlled
    # output isolates deployment ownership, provenance and review races.
    def repair(original, *_):
        value = deepcopy(original)
        value["source_converter"] = {"version": deployment.VERSION, "risk_family": "unchanged_clean",
                                     "warnings": [], "requires_fresh_human_audit": True}
        return value
    monkeypatch.setattr(deployment, "repair_geometry_v4", repair)
    monkeypatch.setattr(deployment, "load_boundary_maps", lambda _: {
        name: np.zeros((874, 1164), dtype=np.float32) for name in ("lane", "my_car", "movable")})
    monkeypatch.setattr(deployment, "check", lambda *_: (.9, []))
    evidence, _ = regression_evidence(tmp_path)
    return settings, store, previous_samples, previous_repairs, evidence


def test_staging_is_read_only_and_application_requires_three_fresh_samples(staged_fixture):
    settings, store, previous_samples, repairs, evidence = staged_fixture
    before = snapshot(store)
    immutable_bytes = {row["path"]: Path(row["path"]).read_bytes() for row in before["annotations"]}
    frozen = settings.path("models", "ufld_baseline", "running-fixture", "dataset", "manifest.json")
    frozen.parent.mkdir(parents=True)
    frozen.write_bytes(b'{"frozen_training_fixture":true}')
    first = deployment.prepare(settings, evidence)
    second = deployment.prepare(settings, evidence)
    assert first["plan_path"] != second["plan_path"]
    assert first["batch_id"] == second["batch_id"]
    assert snapshot(store) == before
    assert first["eligible"] == len(repairs) == 5
    assert first["counts"] == {"sample": 3, "awaiting_audit": 2}
    installed = deployment.apply_plan(settings, first["plan_path"])
    after = snapshot(store)
    assert after["events"] == before["events"] and after["images"] == before["images"]
    assert all(Path(path).read_bytes() == value for path, value in immutable_bytes.items())
    assert frozen.read_bytes() == b'{"frozen_training_fixture":true}'
    assert set(installed["sample_ids"]).isdisjoint(previous_samples)
    assert store.stats()["source_validated"] == 0
    with store.registry() as reg:
        groups = list(reg.conn.execute("SELECT * FROM source_validation_groups WHERE batch_id=?",
                                       (installed["batch_id"],)))
        assert len(groups) == 1 and groups[0]["state"] == "awaiting_audit"
        assert len(json.loads(groups[0]["sample_ids"])) == 3
        assert "family-unchanged_clean:day:lanes-1" in groups[0]["stratum"]
        assert not reg.conn.execute("SELECT 1 FROM annotations WHERE kind='human' AND image_id IN (" +
                                    ",".join("?" for _ in repairs) + ")",
                                    [item["image_id"] for item in repairs]).fetchone()


def test_staging_excludes_live_claims_nonheld_checks_and_all_human_history(staged_fixture):
    settings, store, _, repairs, evidence = staged_fixture
    busy, flagged, validated = [item["image_id"] for item in repairs[:3]]
    with store.registry() as reg:
        with reg.tx() as con:
            con.execute("INSERT INTO review_claims VALUES (?,?,?)", (busy, "phone-reviewer", time.time()+600))
            con.execute("UPDATE source_conversion_checks SET state='flagged' WHERE image_id=?", (flagged,))
            con.execute("UPDATE source_conversion_checks SET state='validated' WHERE image_id=?", (validated,))
    # A reopened old correction is pending again but must remain human-owned.
    store.requeue_empty_lanes()
    before = snapshot(store)
    report = deployment.prepare(settings, evidence)
    plan = json.loads(Path(report["plan_path"]).read_text())
    assert {item["image_id"] for item in plan["repairs"]} == {item["image_id"] for item in repairs[3:]}
    assert report["eligible"] == 2 and report["counts"] == {"sample": 2}
    assert snapshot(store) == before


def test_prepared_plan_rechecks_new_human_decisions_and_phone_claims(staged_fixture):
    settings, store, _, repairs, evidence = staged_fixture
    plan = deployment.prepare(settings, evidence)
    busy, reviewed = [item["image_id"] for item in repairs[:2]]
    with store.registry() as reg:
        with reg.tx() as con:
            con.execute("INSERT INTO review_claims VALUES (?,?,?)", (busy, "phone-reviewer", time.time()+600))
    store.decide(reviewed, "approved", "Nick", store.detail(reviewed)["revision"], str(uuid.uuid4()))
    before = snapshot(store)
    result = deployment.apply_plan(settings, plan["plan_path"])
    assert {item["image_id"]: item["reason"] for item in result["skipped"]} == {
        busy: "active_reviewer", reviewed: "human_reviewed"}
    assert result["eligible"] == 3 and len(result["sample_ids"]) == 3
    after = snapshot(store)
    assert before["images"] == after["images"] and before["events"] == after["events"]
    assert store.stats()["source_validated"] == 0


def test_reapplying_same_plan_creates_no_duplicate_annotations_history_or_reviews(staged_fixture):
    settings, store, _, _, evidence = staged_fixture
    prepared = deployment.prepare(settings, evidence)
    first = deployment.apply_plan(settings, prepared["plan_path"])
    before = snapshot(store)
    with store.registry() as reg:
        history_before = [dict(row) for row in reg.conn.execute("SELECT * FROM source_conversion_repair_history")]
    second = deployment.apply_plan(settings, prepared["plan_path"])
    assert first["eligible"] == 5 and second["eligible"] == 0
    assert second["applied_ids"] == [] and second["sample_ids"] == []
    assert second["geometry_changed"] == 0 and second["risk_families"] == {}
    assert snapshot(store) == before
    with store.registry() as reg:
        assert [dict(row) for row in reg.conn.execute("SELECT * FROM source_conversion_repair_history")] == history_before


def test_unresolved_mechanism_warnings_remain_manual_without_audit_release(staged_fixture, monkeypatch):
    settings, store, _, _, evidence = staged_fixture
    base_repair = deployment.repair_geometry_v4
    def uncertain(*args):
        value = base_repair(*args)
        value["source_converter"].update(risk_family="crossing_uncertain", warnings=["ambiguous_crossing"])
        return value
    monkeypatch.setattr(deployment, "repair_geometry_v4", uncertain)
    report = deployment.prepare(settings, evidence)
    assert report["counts"] == {"flagged": 5} and report["groups"] == 0
    installed = deployment.apply_plan(settings, report["plan_path"])
    assert installed["sample_ids"] == [] and installed["eligible"] == 5
    assert store.stats()["source_validated"] == 0
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM source_conversion_checks WHERE state='flagged'") == 5
        assert reg.count("SELECT count(*) FROM source_validation_groups WHERE state='passed'") == 0


def test_staging_detects_geometry_changes_even_if_converter_mutates_its_input(staged_fixture, monkeypatch):
    settings, store, _, _, evidence = staged_fixture
    def in_place(original, *_):
        original["lanes"][0]["points"][0][0] += 2
        original["source_converter"] = {"risk_family": "boundary_join", "warnings": []}
        return original
    monkeypatch.setattr(deployment, "repair_geometry_v4", in_place)
    before = snapshot(store)
    report = deployment.prepare(settings, evidence)
    assert report["geometry_changed"] == 5
    plan = json.loads(Path(report["plan_path"]).read_text())
    assert {item["diagnostics"]["risk"] for item in plan["repairs"]} == {"shape_changed"}
    assert snapshot(store) == before


@pytest.mark.parametrize("tamper", ["draft", "report", "dependency", "evaluator"])
def test_apply_rejects_stale_sealed_files_before_any_database_mutation(staged_fixture, tmp_path, tamper):
    settings, store, _, _, evidence = staged_fixture
    if tamper == "dependency":
        report = json.loads(evidence.read_text())
        extra = tmp_path / "fixture_dependency.py"
        extra.write_text("before")
        report["dependency_sha256"][str(extra)] = file_sha256(extra)
        evidence.write_text(json.dumps(report))
    report = deployment.prepare(settings, evidence)
    plan = json.loads(Path(report["plan_path"]).read_text())
    before = snapshot(store)
    if tamper == "draft":
        target = Path(plan["repairs"][0]["geometry_path"])
        target.write_text(target.read_text()+" ")
    elif tamper == "report":
        evidence.write_text(evidence.read_text()+" ")
    elif tamper == "dependency":
        extra.write_text("after")
    else:
        Path(plan["converter_evidence"]["evaluator_code_path"]).write_text("changed evaluator")
    with pytest.raises(ValueError, match="changed"):
        deployment.apply_plan(settings, report["plan_path"])
    assert snapshot(store) == before


def test_source_fingerprint_drift_is_reported_and_cannot_be_applied(staged_fixture):
    settings, store, _, repairs, evidence = staged_fixture
    Path(repairs[0]["previous_check"]["source_path"]).write_bytes(b"changed disposable source")
    before = snapshot(store)
    report = deployment.prepare(settings, evidence)
    assert report["errors"] == [{"image_id": repairs[0]["image_id"], "error": "Source changed after audit"}]
    with pytest.raises(ValueError, match="plan has errors"):
        deployment.apply_plan(settings, report["plan_path"])
    assert snapshot(store) == before


@pytest.mark.parametrize("field,value", [
    ("approved_sample_regressions", False), ("approved_sample_regressions", 1),
    ("original_annotations_unchanged", 1), ("converter_and_dependencies_stable", False),
    ("source_fingerprints_verified", False), ("known_failures_resolved_or_flagged", False),
])
def test_regression_report_requires_explicit_typed_safety_evidence(tmp_path, field, value):
    path, report = regression_evidence(tmp_path)
    report["safety_checks"][field] = value
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="[Rr]egression"):
        deployment.verify_regression_report(path)


@pytest.mark.parametrize("problem", ["empty_dependencies", "missing_dependency", "unflagged_failure"])
def test_incomplete_evidence_cannot_install_or_release_source_groups(tmp_path, problem):
    path, report = regression_evidence(tmp_path)
    if problem == "empty_dependencies":
        report["dependency_sha256"] = {}
    elif problem == "missing_dependency":
        report["dependency_sha256"].pop(next(iter(report["dependency_sha256"])))
    else:
        report["unresolved_known_failures"] = [{"image_id": "fixture", "flagged": False,
                                               "warnings": ["ambiguous_crossing"]}]
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        deployment.verify_regression_report(path)


def test_known_defect_can_only_be_partial_when_a_specific_warning_flags_it(tmp_path):
    path, report = regression_evidence(tmp_path)
    problem = {"image_id": "fixture-known-failure", "status": "flagged", "flagged": True,
               "warnings": ["ambiguous_crossing"]}
    report["known_failure_checks"] = [problem]
    report["unresolved_known_failures"] = [problem]
    path.write_text(json.dumps(report))
    assert deployment.verify_regression_report(path) == report
    report["unresolved_known_failures"][0]["warnings"] = []
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="unresolved known failures"):
        deployment.verify_regression_report(path)
