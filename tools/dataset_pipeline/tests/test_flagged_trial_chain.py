"""Disposable end-to-end trial staging; real immutable builder, mocked training."""
import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from bcdata.bootstrap import atomic_json
from bcdata.db import Registry
from bcdata import flagged_model_trial as trial
from bcdata import flagged_teacher_experiment as helper
from bcdata.local import local_settings
from bcdata.teachers.base import file_sha256
from bcdata.ufld_baseline import parse_arguments
from bcdata.ufld_experiment import load_experiment_export
_fixture_spec = importlib.util.spec_from_file_location("ufld_trial_chain_disposable_fixture", Path(__file__).with_name("test_ufld_experiment.py"))
_fixture_module = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture_module)
experiment_fixture = _fixture_module.experiment_fixture


@pytest.fixture
def trial_chain(experiment_fixture, monkeypatch):
    fixture = experiment_fixture
    settings = local_settings(fixture.temp / "original-registry")
    reg = Registry(settings.db_path)
    reg.conn.execute("""CREATE TABLE IF NOT EXISTS source_conversion_checks(
        image_id TEXT PRIMARY KEY,batch_id TEXT,group_key TEXT,state TEXT,
        geometry_id INTEGER,geometry_version TEXT,geometry_path TEXT,geometry_sha256 TEXT,
        source_id INTEGER,source_path TEXT,source_sha256 TEXT,diagnostics TEXT,created_at REAL)""")
    reg.conn.commit()
    descriptors = [fixture.extra(0), fixture.extra(1, split="val"), fixture.extra(2, split="test")]
    for record in fixture.inventory:
        descriptor = next((d for d in descriptors if d["record"]["image_id"] == record["image_id"]), None)
        path = descriptor["image_path"] if descriptor else fixture.dataset / record.get("image", "reserved-unused.jpg")
        reg.add_image(image_id=record["image_id"], source=record["source"], session_id=record["session_id"],
                      split=record["split"], source_ref="fixture:" + record["image_id"], path=str(path),
                      width=1640, height=590, sha256=record["source_sha256"], pool="commercial",
                      license_id="fixture-license", status="curated", review_status="none", locked=0)
        if descriptor:
            geometry_path = settings.root / "original-conversions" / (record["image_id"] + ".json")
            atomic_json(geometry_path, {**descriptor["geometry"], "width": 1640, "height": 590})
            source_path = settings.root / "original-masks" / (record["image_id"] + ".json")
            atomic_json(source_path, {"human": True})
            reg.add_annotation(record["image_id"], "geometry", "geometry", "fixture", str(geometry_path))
            reg.add_annotation(record["image_id"], "source_label", "fixture", "fixture", str(source_path))
            geometry = reg.annotation(record["image_id"], "geometry", "geometry")
            source = reg.annotation(record["image_id"], "source_label")
            with reg.tx() as con:
                con.execute("INSERT INTO source_conversion_checks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (record["image_id"], "batch", "group", "flagged", geometry["id"], "fixture",
                             str(geometry_path), file_sha256(geometry_path), source["id"], str(source_path),
                             file_sha256(source_path), '{}', max(geometry["created_at"], source["created_at"]) + 1))
    reg.close()
    atomic_json(fixture.base / "status.json", {"best_val_f1": .72})
    (fixture.base / "best.pt").write_bytes(b"fixture-ufld-parent-weights")
    parent = fixture.temp / "parent-teacher.pt"; parent.write_bytes(b"fixture-teacher-parent-weights")
    output = fixture.temp / "isolated-trial"
    state = trial.freeze_inventory(settings, output, fixture.base, parent)
    before = file_sha256(settings.db_path)
    stages = []

    def fake_prepare(settings, registry, base_run, directory, checkpoint, progress=None):
        assert registry.conn.execute("PRAGMA query_only").fetchone()[0] == 1
        atomic_json(directory / "experiment.json", {"fixture": True})
        stages.append("prepare")
        if progress: progress({"phase": "prepared"})

    def fake_train(directory, progress=None):
        record = copy.deepcopy(fixture.records[0])
        teacher = {**fixture.teacher, "checkpoint": str(parent), "checkpoint_sha256": file_sha256(parent),
                   "training_records": [{key: record[key] for key in ("image_id", "source", "session_id", "split", "source_sha256")}],
                   "test_images_used": 0}
        atomic_json(directory / "teacher.json", teacher)
        stages.append("train_teacher")
        if progress: progress({"phase": "complete_awaiting_review"})

    def fake_proposals(settings, directory, update):
        manifest = trial.read_json(directory / "copies.json")
        for item in manifest["items"]:
            geometry = trial.read_json(directory / item["original_geometry_path"])
            atomic_json(directory / "proposals" / (item["copy_id"] + ".json"), {
                "geometry": geometry, "quality": {"eligible_for_experiment": True, "reasons": []},
                "teacher_provenance": {"checkpoint_sha256": file_sha256(parent)},
                "input_sha256": item["copy_sha256"]})
        update(proposals_done=len(manifest["items"]), phase="proposals_complete")
        stages.append("predict")

    calls = []
    def fake_ufld(command, **kwargs):
        assert command[:3] == [trial.sys.executable, "-m", "bcdata.ufld_baseline"]
        args = parse_arguments(command[3:])
        assert args.experimental_data and args.status_report == output / "ufld_training.json"
        assert args.gpu_lock_timeout == -1 and args.batch_size == 4 and args.accumulation == 8
        target = args.prepared_run or args.resume_run
        _, records, _ = load_experiment_export(target)
        assert len([r for r in records if r["label_origin"] == "teacher_pseudo"]) == 1
        assert kwargs["env"]["BRAKECHECK_DATA_ROOT"] == str(settings.root)
        calls.append(args)
        atomic_json(target / "status.json", {"phase": "trained_awaiting_review", "best_val_f1": .75})
        return None

    monkeypatch.setattr(helper, "prepare_teacher", fake_prepare)
    monkeypatch.setattr(helper, "train_teacher", fake_train)
    monkeypatch.setattr(trial, "generate_proposals", fake_proposals)
    monkeypatch.setattr(trial.subprocess, "run", fake_ufld)
    return {"fixture": fixture, "settings": settings, "output": output, "before": before,
            "stages": stages, "calls": calls, "state": state}


def test_real_inventory_copies_builder_chain_reaches_valid_initial_ufld_flags(trial_chain):
    f = trial_chain
    trial.run_pipeline(f["settings"], f["output"])
    assert f["stages"] == ["prepare", "train_teacher", "predict"]
    args = f["calls"][0]
    assert args.prepared_run == f["output"] / "ufld_run" and args.resume_run is None
    assert args.warm_start == f["fixture"].base / "best.pt" and args.evaluate_initial
    result = trial.read_json(f["output"] / "experiment.json")
    assert result["phase"] == "complete_awaiting_comparison_review"
    assert result["training_dataset"]["extra_train_images"] == 1
    assert result["comparison"]["extra_train_images"] == 1
    assert file_sha256(f["settings"].db_path) == f["before"]
    frozen, rows, _ = load_experiment_export(f["output"] / "ufld_run")
    extra = next(r for r in rows if r.get("label_origin") == "teacher_pseudo")
    assert extra["source_sha256"] == extra["copy_provenance"]["original_sha256"]
    assert extra["input_image_sha256"] == extra["copy_provenance"]["copy_sha256"]
    assert extra["source_sha256"] != extra["input_image_sha256"]


def test_completed_chain_resume_does_not_repeat_stages_or_training(trial_chain):
    f = trial_chain
    trial.run_pipeline(f["settings"], f["output"])
    before_stages, before_calls = list(f["stages"]), len(f["calls"])
    trial.run_pipeline(f["settings"], f["output"])
    assert f["stages"] == before_stages and len(f["calls"]) == before_calls
    assert file_sha256(f["settings"].db_path) == f["before"]


def test_interrupted_ufld_resume_uses_resume_flags_without_initial_evaluation(trial_chain):
    f = trial_chain
    trial.run_pipeline(f["settings"], f["output"])
    ufld = f["output"] / "ufld_run"
    (ufld / "last.pt").write_bytes(b"fake interrupted own checkpoint; never unpickled")
    atomic_json(ufld / "status.json", {"phase": "training"})
    trial.run_pipeline(f["settings"], f["output"])
    args = f["calls"][-1]
    assert args.resume_run == ufld and args.prepared_run is None
    assert args.warm_start is None and not args.evaluate_initial


def test_manifest_or_inventory_mutation_blocks_resume_before_helpers(trial_chain):
    f = trial_chain
    path = f["output"] / "inventory.json"
    with path.open("a") as stream: stream.write(" ")
    with pytest.raises(ValueError, match="Frozen inventory changed"):
        trial.run_pipeline(f["settings"], f["output"])
    assert not f["stages"] and not f["calls"]


def test_generate_proposals_checkpoint_input_and_forward_contract_without_gpu(trial_chain, monkeypatch):
    """Execute actual proposal orchestration with a fake Torch/model, never CUDA."""
    import numpy as np
    from bcdata import annotation_training
    from bcdata import geometry as geometry_module
    from bcdata.teachers import comma_segnet
    f = trial_chain
    trial.run_pipeline(f["settings"], f["output"])
    directory = f["output"]
    # Existing proposals belong only to this disposable fixture generation.
    for path in (directory / "proposals").glob("*.json"):
        path.unlink()
    model_calls, predictions = [], []

    class FakeModel:
        def load_state_dict(self, values, strict):
            assert values == {"fixture": True} and strict is True
            model_calls.append("load_strict")
        def to(self, device):
            assert device == "cuda"; model_calls.append("fake_to"); return self
        def eval(self):
            model_calls.append("eval"); return self

    class FakeTeacher:
        def __init__(self, settings): self.settings = settings
        def predict(self, rgb, item):
            assert self.settings.get("teachers.comma_segnet.input_width") == 1024
            assert self.settings.get("teachers.comma_segnet.input_height") == 512
            assert self.device == "cuda" and isinstance(item["copy_sha256"], str)
            predictions.append(item["copy_id"])
            return SimpleNamespace(lane=np.full(rgb.shape[:2], .95, np.float32), extra={})
        def unload(self): model_calls.append("unload")

    def extract(paint, params, my_car, movable):
        assert params.threshold == .5 and my_car is None and movable is None
        return {"width": 1640, "height": 590, "lanes": [{"slot": 2, "physical": True,
            "points": [[400, 300], [600, 570]], "y_top": 300, "y_bottom": 570,
            "mean_confidence": .95, "flags": []}]}

    fake_torch = SimpleNamespace(set_num_threads=lambda count: model_calls.append(("threads", count)),
                                 cuda=SimpleNamespace(is_bf16_supported=lambda: False), __version__="fixture",
                                 load=lambda *args, **kwargs: {"model": {"fixture": True}})
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(annotation_training, "cached_model", lambda: (FakeModel(), {}))
    monkeypatch.setattr(comma_segnet, "CommaSegnetTeacher", FakeTeacher)
    monkeypatch.setattr(geometry_module, "extract_lanes", extract)
    # The chain fixture patched the name; load the original body from its module.
    spec = importlib.util.spec_from_file_location("bcdata._test_real_trial_proposals", Path(trial.__file__))
    actual = importlib.util.module_from_spec(spec); spec.loader.exec_module(actual)
    updates = []
    actual.generate_proposals(f["settings"], directory, lambda **value: updates.append(value))
    assert len(predictions) == 6  # three copies, each normal + horizontally flipped
    assert model_calls[0] == ("threads", 4) and model_calls[-1] == "unload"
    assert updates[-1]["proposals_done"] == 3 and updates[-1]["quality_pass"] == 3
    for item in trial.read_json(directory / "copies.json")["items"]:
        proposal = trial.read_json(directory / "proposals" / (item["copy_id"] + ".json"))
        assert proposal["quality"]["eligible_for_experiment"]
        assert proposal["input_sha256"] == item["copy_sha256"]
        assert proposal["original_sha256"] == item["original_sha256"]
        assert proposal["teacher_provenance"]["source_guided"] is False
        assert proposal["teacher_provenance"]["reference_inputs"] == []
        assert proposal["pipeline_provenance"]["precision"] == "float16"
        assert proposal["pipeline_provenance"]["torch"] == "fixture"
    # Exact provenance permits reuse rather than repeating any forward.
    predictions.clear()
    actual.generate_proposals(f["settings"], directory, lambda **value: None)
    assert not predictions
    assert file_sha256(f["settings"].db_path) == f["before"]


def test_uncommitted_teacher_preparation_is_archived_without_losing_bytes(trial_chain):
    f = trial_chain
    partial = f["output"] / "teacher/cohort/training_cohort.json"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"frozen partial preparation fixture bytes")
    trial.run_pipeline(f["settings"], f["output"])
    archives = list(f["output"].glob("teacher-attempt-*"))
    assert len(archives) == 1
    assert (archives[0] / "cohort/training_cohort.json").read_bytes() == b"frozen partial preparation fixture bytes"
    assert trial.read_json(f["output"] / "teacher/experiment.json") == {"fixture": True}
    assert f["stages"] == ["prepare", "train_teacher", "predict"]


def test_committed_teacher_preparation_is_never_archived(trial_chain):
    f = trial_chain
    teacher = f["output"] / "teacher"
    atomic_json(teacher / "experiment.json", {"committed": True})
    (teacher / "cohort").mkdir()
    assert trial.recover_partial_teacher(f["output"]) is None
    assert not list(f["output"].glob("teacher-attempt-*"))
    assert trial.read_json(teacher / "experiment.json") == {"committed": True}
