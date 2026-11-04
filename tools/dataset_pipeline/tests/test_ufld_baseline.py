import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

import numpy as np
import pytest

from bcdata.export_ufld import cache_entry
from bcdata.review_export import export_reviewed
from bcdata.review_store import ReviewStore
from bcdata.ufld_baseline import (
    COL_ANCHORS,
    ROW_ANCHORS,
    UPSTREAM,
    NativeDataset,
    freeze_export,
    interpolate,
    lane_counts,
    load_frozen_export,
    targets,
    validate_warm_start_config,
    validate_warm_start_splits,
    warm_start_model,
)
from tests.test_review import build_review_fixture


def test_anchor_targets_preserve_slot_gaps_and_valid_negative_images():
    points = np.array(cache_entry([[], [(300, y) for y in range(300, 401, 10)], [], []]), np.float32)
    rows, cols = targets(points)
    assert (rows[:, 0] == -1).all() and (rows[:, 2:] == -1).all()
    assert set(rows[:, 1]) == {-1, int(300 / 1640 * 199)}
    assert (cols == -1).all()  # a vertical line has no unique column intersection
    points[1, 10, 0] = -99999
    result = interpolate(points, [345, 350, 355], 0)
    assert (result[:, 1] == -1).all()  # no interpolation across missing annotations
    rows, cols = targets(cache_entry([[], [], [], []]))
    assert (rows == -1).all() and (cols == -1).all()


def test_frozen_baseline_only_contains_approved_train_and_val(tmp_path):
    settings = build_review_fixture(tmp_path / "data")
    store = ReviewStore(settings)
    for index in range(3):
        store.decide(f"sample-{index}", "approved", "Nick", 0, str(uuid.uuid4()))
    export_reviewed(settings)
    output = tmp_path / "run"
    output.mkdir()
    root, records, marker = freeze_export(settings, output)
    assert {r["split"] for r in records} == {"train", "val"}
    assert len(json.loads((root / "culane_anno_cache.json").read_text())) == 2
    assert json.loads((root / "reserved_test_ids.json").read_text()) == ["sample-2"]
    assert not (root / "images/test").exists()
    assert len(NativeDataset(root, "train")) == len(NativeDataset(root, "val")) == 1
    with pytest.raises(ValueError, match="must not read test"):
        NativeDataset(root, "test")
    verified_root, verified_records, verified_marker = load_frozen_export(output)
    assert verified_root == root and verified_records == records and verified_marker == marker
    image = root / records[0]['image']
    image.write_bytes(image.read_bytes() + b'changed after preparation')
    with pytest.raises(ValueError, match="Frozen training file changed"):
        load_frozen_export(output)


def test_validation_lane_matching_counts_misses_and_false_positives():
    line = [[300, 300], [400, 500]]
    other = [[800, 300], [900, 500]]
    assert lane_counts([line], [line]) == (1, 0, 0)
    assert lane_counts([other], [line]) == (0, 1, 1)
    assert lane_counts([], []) == (0, 0, 0)


def warm_start_config():
    return {
        "architecture": "UFLDv2",
        "backbone": "ResNet34",
        "upstream_commit": UPSTREAM,
        "input": [1600, 320],
        "row_anchors": ROW_ANCHORS.tolist(),
        "col_anchors": COL_ANCHORS.tolist(),
    }


def test_warm_start_accepts_compatible_model_with_prior_run_metadata():
    config = warm_start_config()
    config.update(epochs=5, train_images=123, val_images=45, seed=0)
    assert validate_warm_start_config(config) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("architecture", "UFLD"),
        ("backbone", "ResNet18"),
        ("upstream_commit", "another-upstream-revision"),
        ("input", [320, 1600]),
        ("input", [1280, 320]),
        ("row_anchors", ROW_ANCHORS.tolist()[:-1]),
        ("row_anchors", list(reversed(ROW_ANCHORS.tolist()))),
        ("col_anchors", [float(x) + .001 for x in COL_ANCHORS]),
    ],
)
def test_warm_start_rejects_incompatible_model_geometry(field, value):
    config = warm_start_config()
    config[field] = value
    with pytest.raises(ValueError):
        validate_warm_start_config(config)


@pytest.mark.parametrize("field", list(warm_start_config()))
def test_warm_start_requires_explicit_model_compatibility_metadata(field):
    config = warm_start_config()
    del config[field]
    with pytest.raises(ValueError):
        validate_warm_start_config(config)


def test_warm_start_accepts_stable_splits_and_new_recording_sessions():
    parent = [
        {"image_id": "trained-image", "session_id": "trained-session", "split": "train"},
        {"image_id": "validation-image", "session_id": "validation-session", "split": "val"},
    ]
    updated = parent + [
        {"image_id": "new-train", "session_id": "new-train-session", "split": "train"},
        {"image_id": "new-val", "session_id": "new-val-session", "split": "val"},
        {"image_id": "reserved-test", "session_id": "test-session", "split": "test"},
    ]
    assert validate_warm_start_splits(parent, ["reserved-test"], updated) is None


@pytest.mark.parametrize("evaluation_split", ["val", "test"])
def test_warm_start_rejects_trained_image_moving_to_evaluation(evaluation_split):
    parent = [{"image_id": "trained-image", "session_id": "old-session", "split": "train"}]
    updated = [
        {"image_id": "trained-image", "session_id": "renamed-session", "split": evaluation_split}
    ]
    with pytest.raises(ValueError):
        validate_warm_start_splits(parent, [], updated)


@pytest.mark.parametrize("evaluation_split", ["val", "test"])
def test_warm_start_rejects_new_evaluation_frame_from_trained_session(evaluation_split):
    parent = [{"image_id": "trained-image", "session_id": "same-recording", "split": "train"}]
    updated = [
        {"image_id": "different-frame", "session_id": "same-recording", "split": evaluation_split}
    ]
    with pytest.raises(ValueError):
        validate_warm_start_splits(parent, [], updated)


@pytest.mark.parametrize("fitting_split", ["train", "val"])
def test_warm_start_preserves_parent_reserved_test_images(fitting_split):
    updated = [
        {"image_id": "reserved-test", "session_id": "test-session", "split": fitting_split}
    ]
    with pytest.raises(ValueError):
        validate_warm_start_splits([], ["reserved-test"], updated)


@pytest.fixture
def warm_start_checkpoint(tmp_path, monkeypatch):
    snapshot = tmp_path / "previous-run" / "dataset"
    snapshot.mkdir(parents=True)
    records = [{"image_id": "trained-image", "session_id": "trained-session", "split": "train"}]
    manifest = snapshot / "manifest.json"
    manifest.write_text(json.dumps(records))
    (snapshot / "reserved_test_ids.json").write_text(json.dumps(["reserved-test"]))
    checkpoint = snapshot.parent / "last.pt"
    checkpoint.write_bytes(b"disposable checkpoint bytes; fake torch loader below")
    config = warm_start_config()
    config["manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    config["initialization"] = {"backbone": "ImageNet weights", "lane_heads": "random"}

    class Checkpoint(dict):
        def __getitem__(self, key):
            if key == "optimizer":
                raise AssertionError("A new expanded-data run must not restore the old optimizer")
            return super().__getitem__(key)

    weights = {"head.weight": "fake tensor representing learned lane weights"}
    saved = Checkpoint(config=config, model=weights, optimizer="must stay unused", epoch=4, step=540)
    load_calls = []

    def load(path, **kwargs):
        load_calls.append((path, kwargs))
        return saved

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(load=load))
    model_calls = []
    model = SimpleNamespace(load_state_dict=lambda state, **kwargs: model_calls.append((state, kwargs)))
    return checkpoint, saved, records, model, load_calls, model_calls


def test_warm_start_loads_only_strict_model_weights_and_records_checkpoint_provenance(warm_start_checkpoint):
    checkpoint, saved, records, model, load_calls, model_calls = warm_start_checkpoint
    result = warm_start_model(model, checkpoint, records)
    assert model_calls == [(saved["model"], {"strict": True})]
    assert load_calls == [(checkpoint.resolve(), {"map_location": "cpu", "weights_only": False, "mmap": True})]
    assert result["previous_initialization"] == saved["config"]["initialization"]
    assert result["warm_start"] == {
        "checkpoint": str(checkpoint.resolve()),
        "sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "completed_epochs": 5,
        "optimizer_steps": 540,
        "manifest_sha256": saved["config"]["manifest_sha256"],
        "optimizer_restored": False,
        "schedule": "reset for reviewed dataset",
    }


def test_warm_start_rejects_changed_parent_manifest_before_loading_weights(warm_start_checkpoint):
    checkpoint, saved, records, model, load_calls, model_calls = warm_start_checkpoint
    manifest = checkpoint.parent / "dataset" / "manifest.json"
    manifest.write_text(json.dumps(records + [{"image_id": "tampered", "session_id": "tampered", "split": "train"}]))
    with pytest.raises(ValueError, match="parent manifest changed"):
        warm_start_model(model, checkpoint, records)
    assert model_calls == []


def test_warm_start_rejects_evaluation_leakage_before_loading_weights(warm_start_checkpoint):
    checkpoint, saved, records, model, load_calls, model_calls = warm_start_checkpoint
    leaked_records = [{"image_id": "another-frame", "session_id": "trained-session", "split": "val"}]
    with pytest.raises(ValueError, match="entered evaluation"):
        warm_start_model(model, checkpoint, leaked_records)
    assert model_calls == []
