import copy
import json
from pathlib import Path

import cv2
from filelock import FileLock, Timeout
import numpy as np
import pytest

from bcdata import repair_flagged_proposals as repair
from bcdata.bootstrap import atomic_json
from bcdata.config import load_settings
from bcdata.geometry import HOOD_CROP_POLICY
from bcdata.teachers.base import TeacherOutput, file_sha256


def fixture(tmp_path, count=2):
    settings = load_settings(overrides={"data_root": str(tmp_path / "data")})
    run = settings.root / "experiments/flagged_teacher/run-fixture"
    (run / "teacher").mkdir(parents=True)
    checkpoint = run / "teacher/best.pt"
    checkpoint.write_bytes(b"a pinned fixture checkpoint, not unpickled by tests")
    atomic_json(run / "teacher/teacher.json", {"checkpoint": str(checkpoint),
                "checkpoint_sha256": file_sha256(checkpoint), "name": "adapted", "version": "v1"})
    items = []
    for index in range(count):
        copy_id = f"copy-fixture-{index}"
        image = run / "copies/images" / (copy_id + ".png")
        image.parent.mkdir(parents=True, exist_ok=True)
        rgb = np.zeros((100, 160, 3), np.uint8)
        rgb[:, :, 0] = np.arange(160)
        image.write_bytes(cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))[1].tobytes())
        item = {"copy_id": copy_id, "original_image_id": f"fixture-{index}",
                "original_sha256": "a" * 64, "copy_sha256": file_sha256(image),
                "image_path": str(image.relative_to(run)), "width": 160, "height": 100}
        items.append(item)
        atomic_json(run / "proposals" / (copy_id + ".json"), {
            "copy_id": copy_id, "original_image_id": item["original_image_id"],
            "input_sha256": item["copy_sha256"], "original_sha256": item["original_sha256"],
            "teacher_provenance": {"checkpoint_sha256": file_sha256(checkpoint), "version": "v1"},
            "geometry": {"width": 160, "height": 100, "horizon_y": 42, "valid_bottom": 52, "lanes": []},
            "quality": {"eligible_for_experiment": False}, "extra_original_metadata": {"kept": True}})
    atomic_json(run / "copies.json", {"items": items})
    (settings.root / "metadata.sqlite").write_bytes(b"human decisions must remain unchanged")
    return settings, run, items


class FakeTeacher:
    def __init__(self, fail_id=None, on_predict=None):
        self.calls = []
        self.unloaded = False
        self.fail_id = fail_id
        self.on_predict = on_predict

    def predict(self, rgb, item):
        self.calls.append(item["copy_id"])
        if self.on_predict:
            self.on_predict(item, len(self.calls))
        if item["copy_id"] == self.fail_id:
            raise ValueError("fixture inference error")
        paint = np.zeros((100, 160), np.float32)
        paint[43:90, 59:63] = .97
        hood = np.zeros_like(paint)
        hood[13:25] = .9  # Detached sky-class error must not erase the road.
        hood[92:] = .9
        if rgb[0, 0, 0] > rgb[0, -1, 0]:
            paint = np.ascontiguousarray(paint[:, ::-1])
        return TeacherOutput(lane=paint, extra={"my_car": hood, "movable": np.zeros_like(paint)})

    def unload(self):
        self.unloaded = True


def output(run, item):
    return run / "proposals_hood_repaired" / (item["copy_id"] + ".json")


def test_repair_only_selected_empty_geometry_and_preserves_all_original_bytes(tmp_path):
    settings, run, items = fixture(tmp_path, count=3)
    path = run / "proposals" / (items[1]["copy_id"] + ".json")
    nonempty = json.loads(path.read_text())
    nonempty["geometry"]["lanes"] = [{"slot": 2, "points": [[60, 80], [65, 45]]}]
    atomic_json(path, nonempty)
    path = run / "proposals" / (items[2]["copy_id"] + ".json")
    unclamped = json.loads(path.read_text())
    unclamped["geometry"]["valid_bottom"] = 99
    atomic_json(path, unclamped)
    before = {path: path.read_bytes() for path in settings.root.rglob("*") if path.is_file()}
    original_settings = copy.deepcopy(settings.cfg)
    teacher = FakeTeacher()

    def factory(config, checkpoint):
        assert config.get("teachers.device") == "cpu"
        assert config.get("teachers.amp") is False
        assert config.get("teachers.comma_segnet.input_width") == 1024
        assert config.get("teachers.comma_segnet.input_height") == 512
        return teacher

    state = repair.repair_run(settings, run, teacher_factory=factory)
    assert state["phase"] == "complete"
    assert state["completed"] == state["total"] == 1
    assert state["failures"] == 0 and state["current_ufld_snapshot_unchanged"]
    assert state["device"] == "cpu" and state["precision"] == "float32"
    assert state["torch_threads"] == 2 and state["opencv_threads"] == 1
    assert len(teacher.calls) == 2 and teacher.unloaded
    result = json.loads(output(run, items[0]).read_text())
    assert result["geometry"]["valid_bottom"] > 52
    assert result["geometry"]["lanes"] and result["quality"]["eligible_for_experiment"]
    assert result["geometry"]["hood_crop_policy"] == HOOD_CROP_POLICY
    assert result["repair_provenance"]["policy"] == HOOD_CROP_POLICY
    assert result["repair_provenance"]["original_proposal_sha256"] == file_sha256(run / "proposals" / (items[0]["copy_id"] + ".json"))
    assert result["quality"]["human_approved"] is False
    assert result["extra_original_metadata"] == {"kept": True}
    assert not output(run, items[1]).exists() and not output(run, items[2]).exists()
    assert all(path.read_bytes() == value for path, value in before.items())
    assert settings.cfg == original_settings
    assert not list(settings.root.rglob("large-models.lock"))


def test_successful_resume_does_not_load_or_predict_again(tmp_path):
    settings, run, items = fixture(tmp_path)
    state = repair.repair_run(settings, run, teacher_factory=lambda *_: FakeTeacher(), limit=1)
    assert state["phase"] == "limited" and state["completed"] == 1 and state["total"] == 2
    teacher = FakeTeacher()
    state = repair.repair_run(settings, run, teacher_factory=lambda *_: teacher)
    assert state["completed"] == 2 and state["resumed"] == 1 and state["repaired"] == 1
    original = [output(run, item).read_bytes() for item in items]
    state = repair.repair_run(settings, run, teacher_factory=lambda *_: pytest.fail("Resume loaded a model"))
    assert state["completed"] == state["resumed"] == 2 and state["repaired"] == 0
    assert [output(run, item).read_bytes() for item in items] == original


@pytest.mark.parametrize("change", ["input", "parent", "policy", "checkpoint_stamp"])
def test_resume_validates_pins_and_never_overwrites_existing_repair(tmp_path, change):
    settings, run, items = fixture(tmp_path, count=1)
    repair.repair_run(settings, run, teacher_factory=lambda *_: FakeTeacher())
    repaired_path = output(run, items[0])
    if change == "input":
        (run / items[0]["image_path"]).write_bytes(b"changed image")
    elif change == "parent":
        parent = run / "proposals" / (items[0]["copy_id"] + ".json")
        parent.write_text(parent.read_text() + "\n")
    elif change == "policy":
        settings.cfg["geometry"]["gap_rows"] = 9
    else:
        value = json.loads(repaired_path.read_text())
        value["repair_provenance"]["checkpoint_sha256"] = "b" * 64
        atomic_json(repaired_path, value)
    preserved = repaired_path.read_bytes()
    state = repair.repair_run(settings, run, teacher_factory=lambda *_: pytest.fail("Invalid resume must not re-infer"))
    assert state["phase"] == "complete_with_failures" and state["failures"] == 1
    assert state["completed"] == 0 and repaired_path.read_bytes() == preserved


def test_inference_failure_is_reported_and_never_written_as_negative(tmp_path):
    settings, run, items = fixture(tmp_path)
    teacher = FakeTeacher(fail_id=items[0]["copy_id"])
    state = repair.repair_run(settings, run, teacher_factory=lambda *_: teacher)
    assert state["completed"] == 1 and state["failures"] == 1 and teacher.unloaded
    assert not output(run, items[0]).exists() and output(run, items[1]).exists()
    assert "fixture inference error" in state["failure_examples"][0]["error"]


def test_input_changed_during_inference_cannot_be_published(tmp_path):
    settings, run, items = fixture(tmp_path, count=1)

    def mutate(item, calls):
        if calls == 2:
            (run / item["image_path"]).write_bytes(b"changed while predicting")

    state = repair.repair_run(settings, run, teacher_factory=lambda *_: FakeTeacher(on_predict=mutate))
    assert state["completed"] == 0 and state["failures"] == 1
    assert not output(run, items[0]).exists()


def test_checkpoint_change_fails_preflight_without_model_load(tmp_path):
    settings, run, _ = fixture(tmp_path)
    (run / "teacher/best.pt").write_bytes(b"changed checkpoint")
    with pytest.raises(ValueError, match="checkpoint changed"):
        repair.repair_run(settings, run, teacher_factory=lambda *_: pytest.fail("Changed checkpoint loaded"))
    assert json.loads((run / "proposal_repair.json").read_text())["phase"] == "failed"
    assert not (run / "proposals_hood_repaired").exists()


def test_manifest_change_during_inference_fails_run_not_negative(tmp_path):
    settings, run, items = fixture(tmp_path, count=1)

    def mutate(item, calls):
        if calls == 2:
            path = run / "copies.json"
            value = json.loads(path.read_text())
            value["changed"] = True
            atomic_json(path, value)

    with pytest.raises(repair.FatalRepairError, match="inventory changed"):
        repair.repair_run(settings, run, teacher_factory=lambda *_: FakeTeacher(on_predict=mutate))
    assert not output(run, items[0]).exists()
    assert json.loads((run / "proposal_repair.json").read_text())["phase"] == "failed"


def test_worker_uses_own_lock_and_strict_experiment_scope(tmp_path):
    settings, run, _ = fixture(tmp_path)
    with FileLock(str(run / "proposal_repair.lock")):
        with pytest.raises(Timeout):
            repair.repair_run(settings, run, teacher_factory=lambda *_: FakeTeacher())
    with pytest.raises(ValueError, match="directly under"):
        repair.repair_run(settings, settings.root, teacher_factory=lambda *_: FakeTeacher())


def test_bad_model_initialization_fails_once_not_thousands_of_negative_outputs(tmp_path):
    settings, run, _ = fixture(tmp_path)
    calls = []

    def factory(*_):
        calls.append(True)
        raise RuntimeError("cached model unavailable")

    with pytest.raises(repair.FatalRepairError, match="initialize pinned CPU"):
        repair.repair_run(settings, run, teacher_factory=factory)
    assert len(calls) == 1
    assert not (run / "proposals_hood_repaired").exists()


def test_empty_selection_never_loads_a_teacher_and_invalid_geometry_is_not_selected(tmp_path):
    settings, run, _ = fixture(tmp_path, count=0)
    state = repair.repair_run(settings, run, teacher_factory=lambda *_: pytest.fail("No candidate model load"))
    assert state["phase"] == "complete" and state["completed"] == state["total"] == 0
    for value in ({}, {"geometry": {"lanes": [], "horizon_y": 42, "valid_bottom": float("nan")}},
                  {"geometry": {"lanes": [], "horizon_y": "42", "valid_bottom": 52}}):
        assert not repair.needs_repair(value)
