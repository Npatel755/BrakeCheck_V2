import json
import uuid

import numpy as np
import pytest

from bcdata.bootstrap import atomic_json
from bcdata.dinov3_proposals import binary_iou
from bcdata.review_store import ReviewStore, ReviewError
from tests.test_review import build_review_fixture


def test_comparison_ignores_unknown_and_reports_empty_union_as_missing():
    probability = np.array([[.9, .9], [.8, .1]])
    target = np.array([[False, True], [False, False]])
    valid = np.array([[False, True], [True, True]])
    assert binary_iou(probability, target, valid) == .5
    assert binary_iou(np.zeros((2, 2)), np.zeros((2, 2), bool), valid) is None


def test_dino_candidates_are_comparison_only_and_adoption_preserves_provenance(tmp_path):
    settings = build_review_fixture(tmp_path)
    store = ReviewStore(settings)
    store.decide("sample-0", "approved", "Nick", 0, str(uuid.uuid4()))
    original = store.detail("sample-0")["geometry"]
    path = settings.path("lane_geometry", "teachers", "dinov3_seg", "sample-0.json")
    candidate = {**original, "teacher_version": "dinov3_seg:fixture", "source_guided": False,
                 "model_meta": {"checkpoint_sha256": "fixture-checksum", "backbone_revision": "frozen-revision"}}
    atomic_json(path, candidate)
    with store.registry() as reg:
        reg.add_annotation("sample-0", "teacher", "dinov3_seg", "dinov3_seg:fixture", "fixture-meta.json",
                           license_ids=["dinov3", "dinov3_seg"], params={"candidate_only": True})
    assert store.detail("sample-0")["geometry"] == original
    assert "dinov3_seg" in [o["name"] for o in store.detail("sample-0")["teacher_options"]]
    arguments = dict(lanes=[{"slot": 2, "points": [[450, 650], [550, 410]]}], candidate="dinov3_seg")
    with pytest.raises(ReviewError, match="changed"):
        store.decide("sample-0", "corrected", "Nick", 1, str(uuid.uuid4()), candidate_version="stale", **arguments)
    store.decide("sample-0", "corrected", "Nick", 1, str(uuid.uuid4()), candidate_version="dinov3_seg:fixture", **arguments)
    saved = store.detail("sample-0")["geometry"]
    assert saved["model_licenses"] == ["dinov3", "dinov3_seg"]
    assert saved["candidate_provenance"]["checkpoint_sha256"] == "fixture-checksum"
