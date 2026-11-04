"""Future V4 routing keeps human labels and model comparisons independent."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import numpy as np
import pytest

from bcdata import lanes
from bcdata.db import Registry
from bcdata.review_store import ReviewStore
from tests.test_review import build_review_fixture


@pytest.fixture
def routed_pipeline(tmp_path, monkeypatch):
    settings = build_review_fixture(tmp_path)
    registry = Registry(settings.db_path)
    source_path = settings.root / "source-mask.bin"
    source_path.write_bytes(b"disposable source mask fingerprint")
    registry.add_annotation("sample-0", "source_label", "comma10k", "fixture-source", str(source_path),
                            params={"human": True})
    registry.add_annotation("sample-0", "fused", "fusion", "fixture-fusion", "fixture-fusion.json",
                            license_ids=["comma10k"])
    geometry = json.loads(settings.path("lane_geometry", "sample-0.json").read_text())
    maps = {name: np.zeros((874, 1164), dtype=np.float32) for name in ("lane", "my_car", "movable")}
    calls = []
    monkeypatch.setattr(lanes, "extract_lanes", lambda *_: {**deepcopy(geometry), "route_steps": ["extract"]})
    def legacy(value, *_):
        out = deepcopy(value)
        out["route_steps"].append("v2")
        calls.append("v2")
        return out
    def v4(value, lane, my_car, movable):
        assert lane is maps["lane"] and my_car is maps["my_car"] and movable is maps["movable"]
        out = deepcopy(value)
        out["route_steps"].append("v4")
        calls.append("v4")
        return out
    monkeypatch.setattr(lanes, "repair_geometry", legacy)
    monkeypatch.setattr(lanes, "repair_geometry_v4", v4)
    monkeypatch.setattr(lanes, "risk_diagnostics", lambda _: {"risky": False})
    yield settings, registry, maps, calls
    registry.close()


@pytest.mark.parametrize("source,mode,expected", [
    ("comma10k", "source_label", ["extract", "v2", "v4"]),
    ("comma10k", "teacher_fusion", ["extract"]),
    ("other_dataset", "source_label", ["extract"]),
    ("other_dataset", "teacher_fusion", ["extract"]),
])
def test_v4_only_processes_trusted_comma_source_masks(routed_pipeline, monkeypatch, source, mode, expected):
    settings, registry, maps, calls = routed_pipeline
    registry.update_image("sample-0", source=source)
    record = {"version": "fixture-fusion", "mode": mode, "teacher_versions": {}}
    monkeypatch.setattr(lanes, "load_fused", lambda *_: (record, maps))
    result = lanes.run(settings, registry, [registry.image("sample-0")])
    written = json.loads(settings.path("lane_geometry", "sample-0.json").read_text())
    assert result["written"] == 1 and written["route_steps"] == expected
    assert calls.count("v4") == int(source == "comma10k" and mode == "source_label")
    assert written["mode"] == mode
    assert lanes.GEOMETRY_CODE_VERSION.startswith("geo-4:")
    assert lanes.SOURCE_BOUNDARIES_VERSION in lanes.GEOMETRY_CODE_VERSION
    assert registry.annotation("sample-0", "human") is None


def test_existing_source_candidate_is_created_before_v4_and_remains_comparison_only(routed_pipeline, monkeypatch):
    settings, registry, maps, calls = routed_pipeline
    record = {"version": "fixture-fusion", "mode": "source_label", "teacher_versions": {}}
    monkeypatch.setattr(lanes, "load_fused", lambda *_: (record, maps))
    monkeypatch.setattr(lanes, "risk_diagnostics", lambda value: {"risky": "v3" not in value["route_steps"]})
    def v3(value, *_):
        out = deepcopy(value)
        out["route_steps"].append("v3")
        out["source_converter"] = {"warnings": []}
        calls.append("v3")
        return out
    def candidate(value, *_):
        assert value["route_steps"] == ["extract", "v3"]
        calls.append("candidate")
        return {**deepcopy(value), "teacher": "source_refined", "comparison_fixture": True}
    monkeypatch.setattr(lanes, "repair_geometry_v3", v3)
    monkeypatch.setattr(lanes, "source_candidate", candidate)
    review_status = registry.image("sample-0")["review_status"]
    result = lanes.run(settings, registry, [registry.image("sample-0")])
    assert result["written"] == 1 and calls == ["v3", "candidate", "v4"]
    saved = json.loads(settings.path("lane_geometry", "sample-0.json").read_text())
    proposal_path = settings.path("lane_geometry", "teachers", "source_refined", "sample-0.json")
    proposal = json.loads(proposal_path.read_text())
    assert saved["route_steps"] == ["extract", "v3", "v4"]
    assert proposal["route_steps"] == ["extract", "v3"] and proposal["comparison_fixture"]
    assert registry.annotation("sample-0", "human") is None
    assert registry.image("sample-0")["review_status"] == review_status


def test_model_teacher_comparison_geometry_does_not_receive_v4(routed_pipeline, monkeypatch):
    settings, registry, maps, calls = routed_pipeline
    teacher_path = settings.root / "teacher-output.bin"
    teacher_path.write_bytes(b"disposable teacher output")
    registry.add_annotation("sample-0", "teacher", "fixture_model", "fixture-model-v1", str(teacher_path))
    record = {"version": "fixture-fusion", "mode": "source_label",
              "teacher_versions": {"source_labels": "source-v1", "fixture_model": "fixture-model-v1"}}
    monkeypatch.setattr(lanes, "load_fused", lambda *_: (record, maps))
    monkeypatch.setattr(lanes, "load_output", lambda _: SimpleNamespace(lane=maps["lane"]))
    lanes.run(settings, registry, [registry.image("sample-0")], per_teacher=True)
    active = json.loads(settings.path("lane_geometry", "sample-0.json").read_text())
    comparison = json.loads(settings.path("lane_geometry", "teachers", "fixture_model", "sample-0.json").read_text())
    assert active["route_steps"] == ["extract", "v2", "v4"]
    assert comparison["route_steps"] == ["extract"]
    assert comparison["teacher"] == "fixture_model" and calls == ["v2", "v4"]
    assert registry.annotation("sample-0", "human") is None


def test_locked_human_review_is_preserved_even_when_forced(routed_pipeline, monkeypatch):
    settings, registry, _, calls = routed_pipeline
    store = ReviewStore(settings)
    store.decide("sample-0", "approved", "Nick", 0, str(uuid.uuid4()))
    human_path = Path(registry.annotation("sample-0", "human")["path"])
    human_bytes = human_path.read_bytes()
    auto_path = settings.path("lane_geometry", "sample-0.json")
    auto_bytes = auto_path.read_bytes()
    with store.registry() as reg:
        events = [dict(row) for row in reg.conn.execute("SELECT * FROM review_events")]
    def no_loading(*_):
        raise AssertionError("Locked human labels must skip fusion and all automatic converters")
    monkeypatch.setattr(lanes, "load_fused", no_loading)
    result = lanes.run(settings, registry, [registry.image("sample-0")], force=True)
    assert result["locked"] == 1 and result["written"] == 0 and calls == []
    assert human_path.read_bytes() == human_bytes and auto_path.read_bytes() == auto_bytes
    with store.registry() as reg:
        assert [dict(row) for row in reg.conn.execute("SELECT * FROM review_events")] == events
    assert lanes.current_geometry(registry, "sample-0")[0] == "human"
