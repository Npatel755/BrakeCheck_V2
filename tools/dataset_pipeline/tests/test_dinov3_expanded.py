import json
from pathlib import Path

import pytest

from bcdata.dinov3_training import MODEL, REVISION, acquire_gpu_slot, memory_guard


def test_memory_guard_requires_identical_measured_profile(tmp_path, monkeypatch):
    from bcdata.teachers import dinov3_seg
    checkpoint = tmp_path / "best.pt"
    checkpoint.write_bytes(b"decoder fixture")
    state = {"batch_size": 1, "precision": "bfloat16", "backbone_frozen": True,
             "backbone_parameters": 6716035072, "training_cohort_sha256": "frozen"}
    monkeypatch.setattr(dinov3_seg, "validate_warm_start", lambda *args: state)
    prior = {**state, "peak_vram_gib": 13.272, "backbone_revision": REVISION, "checkpoint": str(checkpoint)}
    evidence = tmp_path / "result.json"
    evidence.write_text(json.dumps(prior))
    guard = memory_guard(checkpoint)
    assert guard["required_free_vram_gib"] == 14.8 and guard["evidence_sha256"]
    assert guard["backbone"] == MODEL and guard["input"] == [896, 448]
    assert memory_guard(checkpoint, width=1024)["required_free_vram_gib"] == 18
    prior["training_cohort_sha256"] = "another experiment"
    evidence.write_text(json.dumps(prior))
    assert memory_guard(checkpoint)["required_free_vram_gib"] == 18
    assert memory_guard(None)["required_free_vram_gib"] == 18


def test_memory_wait_never_holds_shared_lock():
    class Lock:
        held = False
        acquisitions = 0
        def acquire(self, timeout):
            self.held = True
            self.acquisitions += 1
        def release(self):
            self.held = False
    lock = Lock()
    samples = iter([20, 14, 14, 20, 20])
    waits = []
    def wait(phase, details):
        assert not lock.held
        waits.append(phase)
    acquire_gpu_slot(lock, lambda: next(samples) * 2**30, 15.5, wait)
    assert lock.held and lock.acquisitions == 2 and waits == ["waiting_for_gpu_memory"]
    lock.release()


def test_decoder_warmstart_new_optimizer_steps_and_fixed_validation(settings, registry, licenses, tmp_path):
    torch = pytest.importorskip("torch")
    from bcdata.teachers.dinov3_seg import train
    from tests.test_pipeline import build
    build(settings, registry, licenses, n=6)
    for index, row in enumerate(registry.images()):
        registry.update_image(row["image_id"], status="curated", split="val" if index >= 4 else "train")
    settings.cfg["teachers"]["dinov3_seg"].update(input_width=128, input_height=64)
    parent = tmp_path / "parent" / "best.pt"
    train(settings, registry, parent, backbone_id="test:tiny", max_steps=1, batch_size=2, device="cpu")
    state = torch.load(parent, weights_only=True)
    state["trained_steps"] = 1000
    state["history"] = [{"step": 1000, "lane_iou": 0.99}]
    torch.save(state, parent)
    candidate = tmp_path / "candidate" / "best.pt"
    result = train(settings, registry, candidate, backbone_id="test:tiny", max_steps=2, batch_size=2,
                   device="cpu", init_from=parent, evaluate_initial=True,
                   val_sources=["a2d2", "comma10k"], selection_source="a2d2")
    last = torch.load(candidate.parent / "last.pt", weights_only=True)
    assert result["steps"] == 2 and result["warm_start_source_steps"] == 1000
    assert result["optimizer_restored"] is False and result["warm_start_step"] == 0
    assert last["history"][0]["step"] == 2 and last["scheduler"]["last_epoch"] == 2
    assert all(int(value["step"]) == 2 for value in last["optimizer"]["state"].values())
    assert result["selection_metric"] == "a2d2.lane_iou"
    assert result["initial_val_metrics"]["by_source"]["a2d2"]["n"] == 1
    assert result["initial_val_metrics"]["by_source"]["comma10k"]["n"] == 1
    baseline = json.loads((candidate.parent / "initial_validation.json").read_text())
    assert baseline["checkpoint_sha256"] == result["warm_start_sha256"]
    assert baseline["metrics"]["n"] == 2 and last["test_images_used"] == 0


def test_bad_warmstart_fails_before_allocating_backbone(settings, registry, tmp_path, monkeypatch):
    torch = pytest.importorskip("torch")
    from bcdata.teachers import dinov3_seg
    from bcdata.source_mask_training import CLASSES
    state = {"backbone": "test:tiny", "backbone_revision": "wrong revision", "input": [896, 448],
             "classes": CLASSES, "trained_steps": 1, "val_metrics": {"lane_iou": 0.1},
             "decoder": {"weight": torch.tensor([1.0])}}
    checkpoint = tmp_path / "bad.pt"
    torch.save(state, checkpoint)
    def forbidden(*args, **kwargs):
        raise AssertionError("Backbone must not be allocated")
    monkeypatch.setattr(dinov3_seg, "build_model", forbidden)
    with pytest.raises(ValueError, match="Warm-start.*mismatch"):
        dinov3_seg.train(settings, registry, tmp_path / "out.pt", backbone_id="test:tiny", init_from=checkpoint, device="cpu")
    with pytest.raises(FileNotFoundError):
        dinov3_seg.train(settings, registry, tmp_path / "out.pt", backbone_id="test:tiny", init_from=tmp_path / "missing.pt", device="cpu")
    with pytest.raises(ValueError, match="warm-start or exact-cohort resume"):
        dinov3_seg.train(settings, registry, tmp_path / "out.pt", init_from=checkpoint, resume_from=checkpoint, device="cpu")
