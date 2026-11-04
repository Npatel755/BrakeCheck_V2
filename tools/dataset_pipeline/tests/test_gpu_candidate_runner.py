"""Offline orchestration checks; no model download, GPU inference, or real reviews."""
from contextlib import nullcontext
from types import SimpleNamespace
import json
import sys
import uuid

import numpy as np
import pytest

from bcdata.db import Registry
from bcdata.gpu_candidates import Sam2LargeCandidate, Sam3Candidate, candidate_version, completed_candidate, filter_exclusion_instances
from bcdata.teachers.base import TeacherOutput, save_output
from bcdata.review_store import ReviewStore
from bcdata.lanes import current_geometry
from .test_review import build_review_fixture


class Batch(dict):
    def to(self, device):
        assert device == "cuda"
        return self


def test_sam3_reuses_image_features_and_predicts_independent_road(monkeypatch):
    import bcdata.gpu_candidates as module
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(inference_mode=nullcontext))
    monkeypatch.setattr(module, "amp_context", lambda *args: nullcontext())
    cfg = {"prompts": ["paint one", "paint two"], "road_prompts": ["road", "paved road"],
           "exclusion_prompts": ["vehicle"], "threshold": .35}
    teacher = Sam3Candidate(cfg)
    vision = object()
    calls = []

    class Model:
        def get_vision_features(self, pixel_values):
            calls.append("image")
            return vision

        def __call__(self, **kwargs):
            assert kwargs["vision_embeds"] is vision and "pixel_values" not in kwargs
            calls.append(kwargs["input_ids"])
            return kwargs["input_ids"]

    class Processor:
        def __call__(self, images=None, text=None, return_tensors=None):
            if images is not None:
                return Batch(pixel_values="pixels", original_sizes=np.array([[20, 30]]))
            return Batch(input_ids=text)

        def post_process_instance_segmentation(self, prompt, **kwargs):
            mask = np.zeros((1, 20, 30), np.float32)
            if prompt.startswith("paint"):
                mask[:, :, 8] = 1
            elif prompt == "vehicle":
                mask[:, 8:12, 10:15] = 1
            else:
                mask[:, 10:, :] = 1
            return [{"masks": mask, "scores": np.array([.8])}]

    teacher.model, teacher.processor = Model(), Processor()
    result = teacher.predict(np.zeros((20, 30, 3), np.uint8))
    assert calls.count("image") == 1 and len(calls) == 6
    assert result.meta["model_forwards"] == result.meta["prompt_forwards"] == 5
    assert result.meta["image_encoder_forwards"] == 1
    assert result.meta["source_guided"] is False and result.meta["reference_inputs"] == []
    assert len(result.meta["prompt_results"]) == 5
    assert result.lane[0, 8] > 0 and result.road[19, 29] > 0
    assert result.extra["occlusion"][9, 11] > 0
    with pytest.raises(ValueError, match="must not receive"):
        teacher.predict(np.zeros((20, 30, 3), np.uint8), reference=np.ones((20, 30)))


def test_empty_sam2_source_performs_no_model_forward(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(inference_mode=nullcontext))
    teacher = Sam2LargeCandidate({})
    result = teacher.predict(np.zeros((20, 30, 3), np.uint8), np.zeros((20, 30)))
    assert result.meta["source_guided"] is True
    assert result.meta["components"] == result.meta["model_forwards"] == result.meta["image_encoder_forwards"] == 0
    assert not result.lane.any()


def test_exclusion_filter_drops_full_image_but_keeps_individual_vehicle():
    masks = np.zeros((3, 20, 30), np.float32)
    masks[0] = 1
    masks[1, 8:12, 10:15] = 1
    masks[2, 2:6, 20:25] = 1
    scores = np.array([.99, .85, .75])
    kept, kept_scores, info = filter_exclusion_instances(masks, scores, .30)
    assert len(kept) == 2 and np.array_equal(kept, masks[1:])
    assert np.array_equal(kept_scores, scores[1:])
    assert info["instances_before_filter"] == 3 and info["dropped_oversized_instances"] == 1
    assert info["maximum_instance_area_fraction"] == 1
    assert not kept[:, 0, 0].any() and kept[0, 9, 11] == 1


def test_resume_requires_maps_and_matching_geometry(tmp_path):
    settings = build_review_fixture(tmp_path)
    reg = Registry(settings.db_path)
    try:
        version = candidate_version("sam3", {"revision": "pinned", "pipeline_version": "v2"})
        output = TeacherOutput(lane=np.zeros((20, 30)), road=np.ones((20, 30)), extra={"occlusion": np.zeros((20, 30))},
                               meta={"source_guided": False, "model_forwards": 8})
        paths = save_output(settings, "sam3", version, "sample-0", output)
        reg.add_annotation("sample-0", "teacher", "sam3", version, paths["_meta"])
        geometry = settings.path("lane_geometry", "teachers", "sam3", "sample-0.json")
        geometry.parent.mkdir(parents=True, exist_ok=True)
        assert completed_candidate(settings, reg, "sample-0", "sam3", version) is None
        geometry.write_text(json.dumps({"teacher_version": "older", "lanes": []}))
        assert completed_candidate(settings, reg, "sample-0", "sam3", version) is None
        geometry.write_text(json.dumps({"teacher_version": version, "lanes": []}))
        assert completed_candidate(settings, reg, "sample-0", "sam3", version) is not None
        from pathlib import Path
        Path(paths["road"]).unlink()
        assert completed_candidate(settings, reg, "sample-0", "sam3", version) is None
    finally:
        reg.close()


def test_explicit_teacher_candidates_preserve_locked_human_review(tmp_path):
    settings = build_review_fixture(tmp_path)
    store = ReviewStore(settings)
    store.decide("sample-0", "approved", "Nick", 0, str(uuid.uuid4()))
    reg = Registry(settings.db_path)
    try:
        before_human = current_geometry(reg, "sample-0")
        before_image = dict(reg.image("sample-0"))
        before_qc = dict(reg.conn.execute("SELECT * FROM qc WHERE image_id='sample-0'").fetchone())
        before_events = [dict(row) for row in reg.conn.execute("SELECT * FROM review_events")]
        before_reviews = [dict(row) for row in reg.conn.execute("SELECT * FROM reviews")]
        before_source = reg.annotation("sample-0", "geometry", "geometry")["path"]
        version = candidate_version("sam3", {"revision": "pinned", "pipeline_version": "v2"})
        output = TeacherOutput(lane=np.zeros((20, 30)), road=np.ones((20, 30)),
                               extra={"occlusion": np.zeros((20, 30))}, meta={"source_guided": False})
        paths = save_output(settings, "sam3", version, "sample-0", output)
        # Existing teachers still cannot write into a locked human review.
        reg.add_annotation("sample-0", "teacher", "sam3", version, paths["_meta"])
        assert reg.annotation("sample-0", "teacher", "sam3", version) is None
        reg.add_annotation("sample-0", "teacher", "sam3", version, paths["_meta"], params={"candidate_only": True})
        assert reg.annotation("sample-0", "teacher", "sam3", version) is not None
        # The exception is strictly a teacher exception, never geometry/fusion.
        reg.add_annotation("sample-0", "geometry", "geometry", "bad-replacement", "missing.json", params={"candidate_only": True})
        reg.add_annotation("sample-0", "fused", "fusion", "bad-replacement", "missing.json", params={"candidate_only": True})
        assert reg.annotation("sample-0", "geometry", "geometry")["path"] == before_source
        assert reg.annotation("sample-0", "fused", "fusion", "bad-replacement") is None
        geometry = settings.path("lane_geometry", "teachers", "sam3", "sample-0.json")
        geometry.parent.mkdir(parents=True, exist_ok=True)
        geometry.write_text(json.dumps({"teacher_version": version, "lanes": []}))
        assert completed_candidate(settings, reg, "sample-0", "sam3", version) is not None
        assert current_geometry(reg, "sample-0") == before_human
        assert dict(reg.image("sample-0")) == before_image
        assert dict(reg.conn.execute("SELECT * FROM qc WHERE image_id='sample-0'").fetchone()) == before_qc
        assert [dict(row) for row in reg.conn.execute("SELECT * FROM review_events")] == before_events
        assert [dict(row) for row in reg.conn.execute("SELECT * FROM reviews")] == before_reviews
    finally:
        reg.close()
