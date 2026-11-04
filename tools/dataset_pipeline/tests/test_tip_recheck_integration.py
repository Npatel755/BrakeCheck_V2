"""Distal-tip evidence is narrow QC evidence and needs its own human controls."""
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from bcdata import apply_trace_repairs as installer
from bcdata import repair_trace_backlog as evaluator
from bcdata.source_tip_evidence import VERSION
from bcdata.teachers.base import file_sha256
from tests.test_source_tip_evidence import fixture


def examine_fixture(tmp_path, monkeypatch, *, source="a2d2", reasons=None, retain=True,
                    blocked=False, complete=True):
    geometry, paint, evidence = fixture()
    original = tmp_path / "original.json"
    original.write_text(json.dumps(geometry), encoding="utf-8")
    source_path = tmp_path / "source.png"
    source_path.write_bytes(b"mocked human source pixels")
    output = tmp_path / "geometry"
    output.mkdir()
    record = {
        "image": {"image_id": "fixture-tip-1", "height": geometry["height"],
                  "width": geometry["width"], "source": source, "split": "train"},
        "geometry": {"path": str(original)},
        "source": {"path": str(source_path), "params": json.dumps({"human": True})},
        "check": {"geometry_sha256": file_sha256(original), "source_sha256": file_sha256(source_path)},
        "data_root": str(tmp_path), "teacher_path": None, "human": None,
        "group": {"state": "blocked" if blocked else "awaiting_audit"},
    }
    if reasons is None:
        reasons = ["transverse_source_promoted_to_boundary"]
    monkeypatch.setattr(evaluator, "load_paint", lambda source: (paint, paint, paint))
    monkeypatch.setattr(evaluator, "reconstruct", lambda original, paint, **kw: (deepcopy(original), None))
    monkeypatch.setattr(evaluator, "refine", lambda original, paint: (deepcopy(original), None))
    monkeypatch.setattr(evaluator, "normalize_reference", lambda candidate, paint: (candidate, None))
    monkeypatch.setattr(evaluator, "reconstruct_order", lambda original, paint, **kw: (deepcopy(original), None))
    monkeypatch.setattr(evaluator, "retains_source_slots", lambda *args: retain)
    monkeypatch.setattr(evaluator, "retains_slot_traces", lambda *args: retain)
    monkeypatch.setattr(evaluator, "retains_physical_traces", lambda *args: retain)
    monkeypatch.setattr(evaluator, "local_settings", lambda root: object())
    monkeypatch.setattr(evaluator, "reasons_for", lambda *args: (list(reasons), {"score": .9, "tier": "high", "issues": []}))
    if not complete:
        monkeypatch.setattr(evaluator, "measure", lambda *args: {**evidence, "eligible_for_fresh_audit": False})
    return evaluator.examine((record, str(output))), output


def test_tip_evidence_only_removes_the_supported_transverse_reason(tmp_path, monkeypatch):
    result, output = examine_fixture(tmp_path, monkeypatch)
    assert result.get("error") is None
    assert result["eligible"]
    assert result["tip_evidence"]["version"] == VERSION
    assert result["tip_evidence"]["geometry_changed"] is False
    assert result["remaining_reasons"] == []
    assert result["refinement"] is None
    assert len(list(output.glob("*.json"))) == 1


@pytest.mark.parametrize("overrides", [
    {"reasons": ["transverse_source_promoted_to_boundary", "fatal_source_identity_error"]},
    {"source": "comma10k"},
    {"retain": False},
    {"complete": False},
    {"blocked": True},
])
def test_tip_evidence_cannot_bypass_other_source_and_qc_gates(tmp_path, monkeypatch, overrides):
    result, output = examine_fixture(tmp_path, monkeypatch, **overrides)
    assert result.get("error") is None
    assert not result["eligible"]
    assert not list(output.glob("*.json"))
    if overrides.get("source") == "comma10k" or overrides.get("complete") is False:
        assert result["tip_evidence"] is None
        assert "transverse_source_promoted_to_boundary" in result["remaining_reasons"]
    if overrides.get("reasons"):
        assert result["remaining_reasons"] == ["fatal_source_identity_error"]


def test_tip_measurement_is_not_used_when_there_is_no_transverse_warning(tmp_path, monkeypatch):
    def unexpected(*args):
        pytest.fail("A tip exception must only run for its own QC reason")
    monkeypatch.setattr(evaluator, "measure_tip", unexpected)
    result, _ = examine_fixture(tmp_path, monkeypatch, reasons=[])
    assert result.get("error") is None
    assert result["eligible"] and result["tip_evidence"] is None


def controls(count=20, *, tip=False, approved=20):
    return [{"image_id": f"control-{index}", "eligible": True, "regression": False,
             "input": {"image": {"source": "a2d2", "review_status": "approved" if index < approved else "corrected"}},
             "human_comparison": {"candidate": {"tp": 2, "fp": 0, "fn": 0}},
             **({"tip_evidence": {"version": VERSION, "eligible": True}} if tip else {})}
            for index in range(count)]


def test_established_controls_cannot_certify_the_tip_route():
    old, new = controls(40), controls(19, tip=True)
    _, evidence = installer.supported_sources(old + new)
    routes = evidence["a2d2"]["route_evidence"]
    assert routes["native-trace"]["eligible_for_fresh_audits"]
    assert not routes[VERSION]["eligible_for_fresh_audits"]
    assert routes[VERSION]["qualifying_controls"] == 19


@pytest.mark.parametrize("problem", ["too_few", "too_few_approved", "low_match", "regression"])
def test_tip_route_requires_its_own_count_approval_match_and_regression_thresholds(problem):
    rows = controls(19 if problem == "too_few" else 20, tip=True,
                    approved=9 if problem == "too_few_approved" else 20)
    if problem == "low_match":
        for row in rows[:2]:
            row["human_comparison"]["candidate"]["fp"] = 1
    if problem == "regression":
        rows[0]["regression"] = True
    assert installer.supported_sources(rows)[0] == set()


def test_twenty_qualifying_tip_controls_can_certify_only_the_tip_route():
    rows = controls(20, tip=True, approved=10)
    supported, evidence = installer.supported_sources(rows)
    assert supported == {"a2d2"}
    assert set(evidence["a2d2"]["route_evidence"]) == {VERSION}
    assert evidence["a2d2"]["route_evidence"][VERSION]["eligible_for_fresh_audits"]


def test_independent_alternative_tip_controls_do_not_borrow_primary_evidence():
    rows = controls(20)
    for row in rows[:19]:
        row["alternative_controls"] = [{"eligible": True, "tip_evidence": {"version": VERSION, "eligible": True},
            "human_comparison": {"candidate": {"tp": 2, "fp": 0, "fn": 0}}, "regression": False}]
    _, evidence = installer.supported_sources(rows)
    routes = evidence["a2d2"]["route_evidence"]
    assert routes["native-trace"]["eligible_for_fresh_audits"]
    assert routes[VERSION]["qualifying_controls"] == 19
    assert not routes[VERSION]["eligible_for_fresh_audits"]


def test_tip_helper_is_sealed_with_the_evaluation_dependencies():
    paths = evaluator.dependencies()
    assert any(path.endswith("source_tip_evidence.py") for path in paths)


@pytest.mark.parametrize("value", [None, {}, [], [""], ["   "], [1], ["one", "one"], "one"])
def test_invalid_image_id_filters_fail_before_creating_a_generation(tmp_path, value):
    ids = tmp_path / "image-ids.json"
    ids.write_text(json.dumps(value), encoding="utf-8")
    output = tmp_path / "generation"
    with pytest.raises(ValueError, match="unique image IDs"):
        evaluator.prepare(SimpleNamespace(db_path="unused", cfg={}), output, image_ids_file=ids)
    assert not output.exists()


def test_bounded_backlog_keeps_every_human_control_and_seals_the_requested_ids(tmp_path, monkeypatch):
    database = tmp_path / "fixture.sqlite"
    con = sqlite3.connect(database)
    con.execute("CREATE TABLE images (image_id TEXT, status TEXT, locked INTEGER, review_status TEXT)")
    con.execute("CREATE TABLE source_conversion_checks (image_id TEXT, state TEXT, diagnostics TEXT)")
    definitions = [
        ("backlog-selected", "pending", 0, "train", "awaiting_audit"),
        ("backlog-other", "pending", 0, "train", "awaiting_audit"),
        ("control-approved-val", "approved", 1, "val", "approved"),
        ("control-corrected-test", "corrected", 1, "test", "approved"),
        ("backlog-blocked", "pending", 0, "train", "blocked"),
        ("backlog-protected", "pending", 0, "train", "awaiting_audit"),
    ]
    snapshots = {}
    for image_id, review_status, locked, split, group_state in definitions:
        con.execute("INSERT INTO images VALUES (?,'curated',?,?)", (image_id, locked, review_status))
        con.execute("INSERT INTO source_conversion_checks VALUES (?,'flagged',?)", (image_id, json.dumps({"reasons": []})))
        snapshots[image_id] = {"image": {"image_id": image_id, "source": "a2d2", "split": split,
                                       "review_status": review_status},
                               "human": {"path": "existing-human.json"} if locked else None,
                               "group": {"state": group_state}}
    con.commit()
    con.close()
    original_database = database.read_bytes()
    ids = tmp_path / "image-ids.json"
    requested = ["missing-id", "backlog-selected", "backlog-blocked", "backlog-protected"]
    ids.write_text(json.dumps(requested), encoding="utf-8")
    monkeypatch.setattr(evaluator, "snapshot", lambda con, check, settings: deepcopy(snapshots[check["image_id"]]))
    monkeypatch.setattr(evaluator, "protected", lambda con, image_id: image_id == "backlog-protected")
    visited = []

    def examine(payload):
        record, _ = payload
        visited.append(record["image"]["image_id"])
        return {"image_id": record["image"]["image_id"], "input": record, "eligible": True,
                "regression": False, "human_comparison": {"candidate": {"tp": 2, "fp": 0, "fn": 0}}}

    class InlineExecutor:
        def __init__(self, max_workers):
            assert max_workers == 4

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def map(self, function, records, chunksize):
            assert chunksize == 4
            return map(function, records)

    monkeypatch.setattr(evaluator, "examine", examine)
    monkeypatch.setattr(evaluator, "ProcessPoolExecutor", InlineExecutor)
    generation = tmp_path / "generation"
    report = evaluator.prepare(SimpleNamespace(db_path=database, cfg={}), generation,
                               workers=4, image_ids_file=ids)
    selection = json.loads((generation / "selection.json").read_text())
    assert [record["image"]["image_id"] for record in selection["backlog"]] == ["backlog-selected"]
    assert {record["image"]["image_id"] for record in selection["controls"]} == {
        "control-approved-val", "control-corrected-test"}
    assert visited[-1] == "backlog-selected" and set(visited[:-1]) == {
        "control-approved-val", "control-corrected-test"}
    assert report["processed"] == 1 and report["controls_processed"] == 2
    assert report["human_decisions_created"] == report["active_geometry_changes"] == 0
    sealed = report["selection_filter"]
    assert sealed == selection["selection_filter"]
    assert sealed["sha256"] == file_sha256(ids)
    assert sealed["image_ids"] == sorted(requested)
    assert sealed["unavailable_image_ids"] == ["backlog-blocked", "backlog-protected", "missing-id"]
    assert report["selection_sha256"] == file_sha256(generation / "selection.json")
    assert report["controls_sha256"] == file_sha256(generation / "controls.jsonl")
    assert report["backlog_sha256"] == file_sha256(generation / "backlog.jsonl")
    assert database.read_bytes() == original_database
