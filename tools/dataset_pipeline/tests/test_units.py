import json

import cv2
import numpy as np
import pytest

from bcdata.fusion import binary_iou, fuse_maps, fusion_score
from bcdata.frames import Frame, adaptive_select
from bcdata.licenses import LicenseError
from bcdata.quality import assess, hamming, near_duplicate_groups, phash, write_png
from bcdata.sources.base import ApprovalRequired, check_download_budget
from bcdata.sources.comma10k import mask_to_classes, session_of
from bcdata.splits import split_for
from tests.synth import scene

QCFG = {"min_width": 320, "min_height": 240, "invalid_std": 3.0, "blur_abs": 12.0, "blur_norm": 0.01,
        "black_p99": 14, "white_p1": 242}


def test_quality_rejects_only_unusable_frames():
    rgb, _, _ = scene()
    assert assess(rgb, QCFG).ok
    night = (rgb.astype(np.float32) * 0.15).astype(np.uint8)
    rep = assess(night, QCFG)
    assert rep.ok and rep.tags["lighting"] == "night"           # dark is a hard case, not a reject
    assert assess(np.full_like(rgb, 128), QCFG).reason == "uniform_frame"
    dark_noise = np.random.default_rng(0).integers(0, 10, rgb.shape).astype(np.uint8)
    assert assess(dark_noise, QCFG).reason == "black_frame"
    blurred = cv2.GaussianBlur(rgb, (0, 0), 25)
    assert assess(blurred, QCFG).reason == "severe_blur"
    assert assess(None, QCFG).reason == "undecodable"
    assert assess(rgb[:100, :100], QCFG).reason == "too_small"


def test_phash_groups_near_duplicates_only():
    a, _, _ = scene(seed=1)
    b = np.clip(a.astype(int) + 6, 0, 255).astype(np.uint8)      # same frame, slightly brighter
    c, _, _ = scene(seed=2, curve=45.0, lanes=(("solid", 100), ("solid", 1000)))
    d = np.random.default_rng(3).integers(0, 255, a.shape).astype(np.uint8)
    hashes = {"a": phash(a), "b": phash(b), "c": phash(c), "d": phash(d)}
    assert hamming(hashes["a"], hashes["b"]) <= 4
    groups = near_duplicate_groups(hashes, 6)
    assert groups == [["a", "b"]] or groups == [["b", "a"]]


def test_comma10k_names_and_colours():
    s, f = session_of("0001_a23b0de0bc12dcba_2018-06-24--00-29-19_17_79.png")
    assert s == "a23b0de0bc12dcba_2018-06-24--00-29-19" and f == 17 * 1200 + 79
    m = np.zeros((2, 3, 3), np.uint8)
    m[0, 0] = (0x40, 0x20, 0x20)
    m[0, 1] = (0xFF, 0x00, 0x00)
    m[0, 2] = (0xCC, 0x00, 0xFF)
    m[1, 0] = (0x10, 0x10, 0x10)
    assert mask_to_classes(m).tolist() == [[1, 2, 5], [0, 0, 0]]


def test_licence_pools(licenses):
    assert licenses.pool_for_source("comma10k") == "commercial"
    assert licenses.pool_for_source("zod") == "share_alike"
    assert licenses.pool_for_source("some_random_youtube_dump") == "quarantine"
    licenses.entries["comma10k"].status = "blocked"
    with pytest.raises(LicenseError):
        licenses.pool_for_source("comma10k")
    with pytest.raises(LicenseError):
        licenses.check_model("openai-clip")                         # unverified needs explicit override
    assert licenses.output_pool("commercial", ["sam3", "comma_segnet"]) == "commercial"
    assert licenses.output_pool("commercial", ["openai-clip"]) == "quarantine"


def test_download_gate(settings, tmp_path):
    with pytest.raises(ApprovalRequired):
        check_download_budget(settings, int(6e9), tmp_path, None)
    check_download_budget(settings, int(6e9), tmp_path, 6.5)       # approved
    check_download_budget(settings, int(1e9), tmp_path, None)      # under threshold


def test_splits_keep_sessions_together_and_eval_real_only():
    fr = {"train": 0.8, "val": 0.1, "test": 0.1}
    assert len({split_for("sessA", False, fr, 1) for _ in range(5)}) == 1
    tests = [s for s in (f"s{i}" for i in range(400)) if split_for(s, False, fr, 1) == "test"]
    assert 20 < len(tests) < 70
    assert all(split_for(s, True, fr, 1) != "test" for s in (f"s{i}" for i in range(400)))


def test_fusion_disagreement_and_score():
    a = np.zeros((50, 50), np.float32)
    a[:, 10:14] = 1
    b = a.copy()
    c = np.zeros_like(a)
    c[:, 30:34] = 1
    agree = fuse_maps({"x": a, "y": b}, {}, 0.5, 0.3, 0.7)
    dis = fuse_maps({"x": a, "y": c}, {}, 0.5, 0.3, 0.7)
    assert agree["metrics"]["mean_pairwise_iou"] == 1.0 and dis["metrics"]["mean_pairwise_iou"] == 0.0
    assert fusion_score(agree["metrics"], 2) > fusion_score(dis["metrics"], 2)
    assert fusion_score({"uncertain_fraction": 0.0, "mean_pairwise_iou": None}, 1) < 0.8   # one teacher is never "high"
    assert binary_iou(a > 0.5, a > 0.5) == 1.0


def test_adaptive_frame_selection(tmp_path):
    frames = []
    base, _, _ = scene(seed=0)
    for i in range(20):
        if i < 10:
            img = base                                                  # parked at a light
        else:                                                           # driving: the view keeps changing
            noise = np.random.default_rng(i).integers(0, 255, (36, 64, 3)).astype(np.uint8)
            img = cv2.resize(noise, (base.shape[1], base.shape[0]), interpolation=cv2.INTER_CUBIC)
        p = tmp_path / f"f_{i:06d}.png"
        write_png(p, img)
        frames.append(Frame(p, i, float(i)))
    kept = adaptive_select(frames, min_change=0.06, max_gap_s=5.0)
    t = [f.t for f in kept]
    assert t[0] == 0.0 and 5.0 in t                  # parked: one frame plus the max-gap keep-alive
    assert sum(1 for x in t if x >= 10) >= 8          # moving: nearly every frame


def test_db_never_overwrites_human_geometry(settings, registry, licenses):
    from bcdata.sources.base import register_image
    rgb, _, _ = scene()
    p = settings.path("raw", "comma10k", "x.png")
    write_png(p, rgb[:, :, ::-1])
    r = register_image(settings, registry, licenses, p, source="comma10k", session_id="s", source_ref="t")
    hp = settings.path("annotations", "reviewed", "x.json")
    hp.write_text(json.dumps({"lanes": []}))
    registry.add_annotation(r.image_id, "human", "reviewer_1", "human:b", str(hp))
    registry.add_review(r.image_id, "reviewer_1", "approved")
    registry.add_annotation(r.image_id, "geometry", "geometry", "v2", "nowhere.json")
    assert registry.annotation(r.image_id, "geometry") is None       # model output refused for a locked image
    assert registry.image(r.image_id)["locked"] == 1
    dup = register_image(settings, registry, licenses, p, source="comma10k", session_id="s", source_ref="t")
    assert dup.status == "duplicate"
