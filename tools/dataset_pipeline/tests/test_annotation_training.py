"""Candidate training checks use disposable original masks and a tiny CPU net."""
import copy
import json

import cv2
import numpy as np
import pytest

torch = pytest.importorskip("torch")

from bcdata.annotation_training import (IGNORE, MaskDataset, confusion_update, evaluate, fit_check,
                                       make_loader, map_target, metrics_from_confusion,
                                       segmentation_loss, train_candidate, validate_cohort)


def test_shared_taxonomy_and_unknown_pixels_are_not_background():
    assert map_target(np.arange(6, dtype=np.uint8)[None]).tolist() == [[255, 0, 1, 2, 3, 4]]
    with pytest.raises(ValueError, match="0..5"):
        map_target(np.array([[6]], np.uint8))
    logits = torch.randn(1, 5, 1, 3, requires_grad=True)
    target = torch.tensor([[[1, IGNORE, 0]]])
    loss = segmentation_loss(logits, target)
    loss.backward()
    assert torch.equal(logits.grad[:, :, :, 1], torch.zeros_like(logits.grad[:, :, :, 1]))
    empty = torch.randn(1, 5, 2, 2, requires_grad=True)
    segmentation_loss(empty, torch.full((1, 2, 2), IGNORE)).backward()
    assert torch.equal(empty.grad, torch.zeros_like(empty))


def test_metrics_ignore_unknown_and_separate_paint_from_road():
    matrix = np.zeros((5, 5), np.int64)
    confusion_update(matrix, np.array([[1, 0, 1, 1]]), np.array([[1, 1, 0, IGNORE]]))
    result = metrics_from_confusion(matrix)
    assert result["paint_iou"] == pytest.approx(1 / 3)
    assert result["paint_precision"] == result["paint_recall"] == .5
    assert result["road_iou"] == 1.
    assert result["valid_pixels"] == 3


def test_cohort_rejects_test_and_recording_session_leakage():
    def row(identifier, split, session):
        return {"image_id": identifier, "source": "a2d2", "split": split, "session_id": session}
    manifest = {"test_images_used": 0, "cohorts": {"train": [row("one", "train", "drive-a")],
                                                 "val": [row("two", "val", "drive-b")]}}
    assert len(validate_cohort(manifest)["train"]) == 1
    contaminated = copy.deepcopy(manifest)
    contaminated["cohorts"]["val"][0]["session_id"] = "drive-a"
    with pytest.raises(ValueError, match="leakage"):
        validate_cohort(contaminated)
    contaminated = copy.deepcopy(manifest)
    contaminated["cohorts"]["val"][0]["split"] = "test"
    with pytest.raises(ValueError, match="split"):
        validate_cohort(contaminated)


@pytest.fixture
def records(tmp_path):
    result = []
    for index in range(4):
        image = np.zeros((16, 32, 3), np.uint8)
        image[:, 16:] = 150
        classes = np.ones((16, 32), np.uint8)
        classes[:, 15:17] = 2
        classes[:2] = 0
        image_path, mask_path = tmp_path / f"image-{index}.png", tmp_path / f"mask-{index}.png"
        cv2.imwrite(str(image_path), image); cv2.imwrite(str(mask_path), classes)
        result.append({"image_id": str(index), "source": "a2d2" if index % 2 == 0 else "comma10k",
                       "path": str(image_path), "source_annotation": {"path": str(mask_path),
                       "params": json.dumps({"format": "class_map", "human": True})}})
    return result


def test_original_mask_dataset_preserves_input_contract(records):
    dataset = MaskDataset(records, 32, 16)
    image, target, source = dataset[0]
    assert image.max() == 150 and image.shape == (3, 16, 32)
    assert (target[:2] == IGNORE).all()
    assert (target[2:, 15:17] == 1).all()
    assert source == "a2d2"


def test_dataset_refuses_changed_frozen_source_bytes(records):
    from pathlib import Path
    from bcdata.teachers.base import file_sha256
    path = Path(records[0]["source_annotation"]["path"])
    records[0]["assets"] = {"source": {"path": str(path), "sha256": file_sha256(path)}}
    dataset = MaskDataset(records, 32, 16)
    dataset[0]
    changed = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    changed[4, 4] = 3
    cv2.imwrite(str(path), changed)
    with pytest.raises(RuntimeError, match="SHA mismatch"):
        dataset[0]


def test_real_fit_step_restores_all_pretrained_parameters_and_buffers(records):
    torch.set_num_threads(1)
    model = torch.nn.Sequential(torch.nn.Conv2d(3, 8, 1), torch.nn.BatchNorm2d(8),
                                torch.nn.ReLU(), torch.nn.Conv2d(8, 5, 1))
    original = {key: value.clone() for key, value in model.state_dict().items()}
    result = fit_check(model, MaskDataset(records, 32, 16), 4, "cpu")
    assert result["real_optimizer_step"] is True and result["batch_size"] == 4
    assert all(torch.equal(original[key], value) for key, value in model.state_dict().items())
    assert all(parameter.grad is None for parameter in model.parameters())


def test_tiny_cpu_training_saves_only_candidate_and_preserves_source_files(records, tmp_path):
    torch.set_num_threads(1); torch.manual_seed(42)
    originals = {row["path"]: __import__("pathlib").Path(row["path"]).read_bytes() for row in records}
    originals.update({row["source_annotation"]["path"]: __import__("pathlib").Path(row["source_annotation"]["path"]).read_bytes() for row in records})
    model = torch.nn.Sequential(torch.nn.Conv2d(3, 8, 1), torch.nn.ReLU(), torch.nn.Conv2d(8, 5, 1))
    before = model[0].weight.detach().clone()
    train = make_loader(records[:2], 32, 16, 2, training=True)
    validation = make_loader(records[2:], 32, 16, 2)
    progress = []
    result = train_candidate(model, train, validation, tmp_path / "candidate", {"epochs": 2,
                             "learning_rate": 1e-3, "patience": 3, "cohort_sha256": "frozen-cohort"},
                             device="cpu", progress=progress.append)
    assert result["candidate_only"] and not result["auto_promote"] and result["test_images_used"] == 0
    assert result["trained_steps"] == 2 and result["same_frozen_validation"]
    assert result["cohort_sha256"] == "frozen-cohort"
    for name in ("baseline", "candidate"):
        assert result[name]["a2d2"]["images"] == result[name]["comma10k"]["images"] == 1
        assert "diagnostic_only" in result[name]["comma10k"]["interpretation"]
    assert not torch.equal(before, model[0].weight)
    saved = torch.load(tmp_path / "candidate/best.pt", weights_only=True)
    assert saved["trained_steps"] > 0 and saved["candidate_only"]
    assert "optimizer" in saved and "scheduler" in saved
    assert all(__import__("pathlib").Path(path).read_bytes() == data for path, data in originals.items())
    assert not list(tmp_path.rglob("*.sqlite"))
    assert not list(tmp_path.rglob("*review*"))
