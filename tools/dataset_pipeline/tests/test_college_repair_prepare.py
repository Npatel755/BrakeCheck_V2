"""Load only complete, source-bound proposal evidence into the audit freezer."""
from copy import deepcopy
import json

import pytest

from bcdata import college_repair_prepare as loader
from bcdata.college_repair_review import COLLECTION
from tests.test_college_repair_review import original_fixture
from tests.test_real_source_review import raw_json, sha


def preparation_fixture(tmp_path, monkeypatch):
    source_root, held, store = original_fixture(tmp_path, held_count=4)
    _, _, _, manifest_sha, audits_sha = store.snapshot(fresh=True)
    ordinary_path = tmp_path / "ordinary.jsonl"
    ordinary_row = held[0]
    ordinary = [{"image_id": ordinary_row["image_id"],
        "geometry": json.loads((source_root / ordinary_row["geometry"]).read_bytes()),
        "provenance": {"source_sha256": ordinary_row["source_sha256"],
                       "native_sha256": ordinary_row["source_label_sha256"],
                       "source_geometry_sha256": ordinary_row["geometry_sha256"]},
        "candidate_status": "ready_for_audit", "diagnostics": {"blockers": []},
        "batch_key": "ordinary-fixture"}]
    proposal_directory = tmp_path / "experiments" / COLLECTION / "adapted-full"
    proposal_directory.mkdir(parents=True)
    index = {"schema": "college_paint_proposal_index:v1", "state": "complete", "failed": [],
             "total": 3, "done": 3, "candidate_only": True, "training_eligible": False,
             "source_manifest_sha256": manifest_sha, "source_audits_sha256": audits_sha,
             "model_receipt": {"model": "fixture", "checkpoint_sha256": "d" * 64}, "rows": []}
    proposals = {}
    for row in held[1:]:
        geometry = json.loads((source_root / row["geometry"]).read_bytes())
        evidence = {}
        for name, suffix in (("model_probability_maps", "npz"), ("comparison_preview", "jpg")):
            relative = f"evidence/{row['image_id']}.{suffix}"
            path = proposal_directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((name + row["image_id"]).encode())
            evidence[name] = {"path": relative, "sha256": sha(path.read_bytes())}
        geometry.update(source_receipt=deepcopy(row), evidence=evidence, candidate_only=True,
                        training_eligible=False, diagnostic_only=False,
                        repair_disposition="ready_for_audit", repair_qc_reasons=[])
        relative = f"proposals/{row['image_id']}.json"
        path = proposal_directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw_json(geometry))
        proposals[row["image_id"]] = geometry
        index["rows"].append({"image_id": row["image_id"], "proposal": relative,
                              "sha256": sha(path.read_bytes()), "temporal_block": 0,
                              "repair_disposition": "ready_for_audit", "repair_qc_reasons": []})
    captured = []

    def freeze(data_root, candidates, **kwargs):
        captured.append({"data_root": data_root, "candidates": deepcopy(candidates), **kwargs})
        return {"captured": len(candidates)}

    monkeypatch.setattr(loader, "stage_batch", freeze)
    result = {"source_root": source_root, "held": held, "ordinary": ordinary,
              "ordinary_path": ordinary_path, "proposal_directory": proposal_directory,
              "index": index, "proposals": proposals, "captured": captured}
    write_inputs(result)
    return result


def write_inputs(context):
    context["ordinary_path"].write_bytes(b"".join(raw_json(row) for row in context["ordinary"]))
    for entry in context["index"]["rows"]:
        if entry["image_id"] in context["proposals"]:
            path = context["proposal_directory"] / entry["proposal"]
            path.write_bytes(raw_json(context["proposals"][entry["image_id"]]))
            entry["sha256"] = sha(path.read_bytes())
    (context["proposal_directory"] / "proposal-index.json").write_bytes(raw_json(context["index"]))


def prepare(tmp_path, context):
    return loader.prepare(tmp_path, context["ordinary_path"], context["proposal_directory"])


def test_loading_complete_evidence_keeps_source_repairs_and_models_truthful(tmp_path, monkeypatch):
    context = preparation_fixture(tmp_path, monkeypatch)
    source_before = {path: path.read_bytes() for path in context["source_root"].rglob("*") if path.is_file()}
    assert prepare(tmp_path, context) == {"captured": 4}
    assert len(context["captured"]) == 1
    frozen = context["captured"][0]
    assert frozen["original_held_count"] == 4
    assert frozen["report"]["ordinary_photos"] == 1 and frozen["report"]["model_photos"] == 3
    assert frozen["report"]["audit_modes"] == {"ready": 4}
    assert frozen["candidates"][0]["candidate_origin"] == "source_repair"
    for candidate in frozen["candidates"][1:]:
        assert candidate["candidate_origin"] == "model_proposal"
        assert len(candidate["evidence_files"]) == 3
        assert all(asset["path"].startswith("adapted-full/") for asset in candidate["evidence_files"])
        assert candidate["provenance"]["human_decisions_created"] == 0
        assert candidate["provenance"]["model_receipt"] == context["index"]["model_receipt"]
    assert all(path.read_bytes() == raw for path, raw in source_before.items())


@pytest.mark.parametrize("tamper", ["proposal", "model_probability_maps", "comparison_preview"])
def test_changed_proposal_or_supporting_evidence_is_rejected(tmp_path, monkeypatch, tamper):
    context = preparation_fixture(tmp_path, monkeypatch)
    entry = context["index"]["rows"][0]
    relative = entry["proposal"] if tamper == "proposal" else context["proposals"][entry["image_id"]]["evidence"][tamper]["path"]
    path = context["proposal_directory"] / relative
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="changed"):
        prepare(tmp_path, context)
    assert not context["captured"]


@pytest.mark.parametrize("bad", ["running", "failed", "unfinished", "row_count"])
def test_incomplete_or_inconsistent_proposal_index_is_rejected(tmp_path, monkeypatch, bad):
    context = preparation_fixture(tmp_path, monkeypatch)
    if bad == "running":
        context["index"]["state"] = "running"
    elif bad == "failed":
        context["index"]["failed"] = [{"reason": "fixture failure"}]
    elif bad == "unfinished":
        context["index"]["done"] = 2
    else:
        context["index"].update(total=4, done=4)
    write_inputs(context)
    with pytest.raises(ValueError):
        prepare(tmp_path, context)
    assert not context["captured"]


@pytest.mark.parametrize("bad", ["source_manifest", "source_audits", "ordinary_native", "model_source_receipt"])
def test_stale_original_source_bindings_are_rejected(tmp_path, monkeypatch, bad):
    context = preparation_fixture(tmp_path, monkeypatch)
    if bad in {"source_manifest", "source_audits"}:
        context["index"][bad + "_sha256"] = "a" * 64
    elif bad == "ordinary_native":
        context["ordinary"][0]["provenance"]["native_sha256"] = "a" * 64
    else:
        proposal = context["proposals"][context["index"]["rows"][0]["image_id"]]
        proposal["source_receipt"]["geometry_sha256"] = "a" * 64
    write_inputs(context)
    with pytest.raises(ValueError):
        prepare(tmp_path, context)
    assert not context["captured"]


@pytest.mark.parametrize("control", ["index_entry", "proposal_geometry"])
def test_diagnostic_controls_never_enter_a_fresh_audit_batch(tmp_path, monkeypatch, control):
    context = preparation_fixture(tmp_path, monkeypatch)
    entry = context["index"]["rows"][0]
    if control == "index_entry":
        entry["diagnostic_only"] = True
    else:
        context["proposals"][entry["image_id"]]["diagnostic_only"] = True
    write_inputs(context)
    with pytest.raises(ValueError):
        prepare(tmp_path, context)
    assert not context["captured"]


def test_duplicate_ordinary_photos_are_rejected_before_freezing(tmp_path, monkeypatch):
    context = preparation_fixture(tmp_path, monkeypatch)
    context["ordinary"].append(deepcopy(context["ordinary"][0]))
    write_inputs(context)
    with pytest.raises(ValueError, match="Duplicate"):
        prepare(tmp_path, context)
    assert not context["captured"]


def test_duplicate_model_photos_are_rejected_before_freezing(tmp_path, monkeypatch):
    context = preparation_fixture(tmp_path, monkeypatch)
    context["index"]["rows"][1] = deepcopy(context["index"]["rows"][0])
    write_inputs(context)
    with pytest.raises(ValueError, match="duplicates"):
        prepare(tmp_path, context)
    assert not context["captured"]


def test_every_remaining_held_photo_requires_a_disposition(tmp_path, monkeypatch):
    context = preparation_fixture(tmp_path, monkeypatch)
    context["ordinary"] = []
    write_inputs(context)
    with pytest.raises(ValueError, match="Every remaining held photo"):
        prepare(tmp_path, context)
    assert not context["captured"]


def test_ready_index_with_no_observed_training_span_is_demoted_to_problem_triage(tmp_path, monkeypatch):
    context = preparation_fixture(tmp_path, monkeypatch)
    entry = context["index"]["rows"][0]
    context["proposals"][entry["image_id"]]["lanes"] = [{"slot": 2, "points": [[20, 10], [19, 11]]}]
    write_inputs(context)
    prepare(tmp_path, context)
    candidate = next(candidate for candidate in context["captured"][0]["candidates"] if candidate["image_id"] == entry["image_id"])
    assert candidate["audit_mode"] == "needs_repair"
    assert "no_observed_training_span" in candidate["blockers"]


def test_existing_model_blockers_cannot_be_erased_by_a_ready_index_label(tmp_path, monkeypatch):
    context = preparation_fixture(tmp_path, monkeypatch)
    entry = context["index"]["rows"][0]
    entry["repair_qc_reasons"] = ["ambiguous_visible_paint"]
    write_inputs(context)
    try:
        prepare(tmp_path, context)
    except ValueError:
        assert not context["captured"]
    else:
        candidate = next(candidate for candidate in context["captured"][0]["candidates"] if candidate["image_id"] == entry["image_id"])
        assert candidate["audit_mode"] == "needs_repair"
        assert "ambiguous_visible_paint" in candidate["blockers"]
