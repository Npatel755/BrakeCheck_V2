"""Temporal failure modes: stale evidence, association, resets and motion."""
import json

import cv2
import numpy as np
import pytest

from bcdata.lane_temporal import LaneTracker, TrackingConfig, estimate_alignment, sample_lane
from bcdata.ufld_temporal import arguments, compare, preprocess, trace_frames


def lane(x=.3, start=.42, stop=.96, confidence=1.):
    rows = np.linspace(start, stop, 55)
    return {"points": np.column_stack([np.full(len(rows), x), rows]).tolist(), "confidence": confidence}


def mean_x(output):
    return np.mean([p[0] for segment in output[0]["segments"] for p in segment])


@pytest.mark.parametrize("mode", ("ema", "kalman", "flow_kalman"))
def test_predictions_expire_without_renewing_measurement_time(mode):
    tracker = LaneTracker(mode)
    assert tracker.update([lane()], 0) == []
    measured = tracker.update([lane()], .03)
    assert len(measured) == 1
    for stamp in (.06, .15, .27):
        predicted = tracker.update([], stamp)
        assert predicted[0]["track_id"] == measured[0]["track_id"]
        assert predicted[0]["status"] == "predicted"
        assert predicted[0]["last_observed_at"] == .03
        assert all(t == .03 for _, t in predicted[0]["row_observed_at"])
    assert tracker.update([], .281) == []


def test_partial_detections_do_not_refresh_missing_far_rows():
    tracker = LaneTracker()
    tracker.update([lane()], 0)
    tracker.update([lane()], .03)
    for stamp in (.1, .2, .3, .4):
        result = tracker.update([lane(stop=.70)], stamp)
    assert len(result) == 1
    assert max(y for segment in result[0]["segments"] for _, y in segment) <= .70 + 1e-9


def test_reordering_detections_keeps_physical_track_ids():
    tracker = LaneTracker()
    tracker.update([lane(.3), lane(.7)], 0)
    first = tracker.update([lane(.7), lane(.3)], .03)
    second = tracker.update([lane(.3), lane(.7)], .06)
    assert {round(mean_x([x]), 2): x["track_id"] for x in first} == {
        round(mean_x([x]), 2): x["track_id"] for x in second}


def test_outlier_does_not_drag_confirmed_lane_or_create_confirmed_ghost():
    tracker = LaneTracker()
    tracker.update([lane()], 0)
    original = tracker.update([lane()], .03)[0]["track_id"]
    result = tracker.update([lane(.5)], .06)
    assert len(result) == 1 and result[0]["track_id"] == original
    assert mean_x(result) == pytest.approx(.3)
    assert result[0]["status"] == "predicted"
    result = tracker.update([lane()], .09)
    assert len(result) == 1 and result[0]["track_id"] == original


@pytest.mark.parametrize("event", ("backward", "duplicate", "gap", "scene", "cut"))
def test_discontinuities_cannot_reuse_a_confirmed_lane(event):
    tracker = LaneTracker()
    tracker.update([lane()], 1, scene_id="a")
    old = tracker.update([lane()], 1.03, scene_id="a")[0]["track_id"]
    stamp = {"backward": .5, "duplicate": 1.03, "gap": 3}.get(event, 1.06)
    scene = "b" if event == "scene" else "a"
    assert tracker.update([lane()], stamp, scene_id=scene, scene_cut=event == "cut") == []
    fresh = tracker.update([lane()], stamp + .03, scene_id=scene)
    assert fresh[0]["track_id"] != old


def test_supported_spans_and_curvature_are_preserved():
    tracker = LaneTracker(config=TrackingConfig(confirmations=1))
    rows = np.concatenate([np.linspace(.42, .6, 12), np.linspace(.8, .96, 12)])
    points = np.column_stack([.25 + .2 * (rows - .6) ** 2, rows])
    output = tracker.update([points], 0)
    assert len(output[0]["segments"]) == 2
    assert max(y for _, y in output[0]["segments"][0]) <= .6 + 1e-9
    assert min(y for _, y in output[0]["segments"][1]) >= .8 - 1e-9
    values = [x for segment in output[0]["segments"] for x, _ in segment]
    assert max(values) - min(values) > .015


def test_raw_keeps_original_decoder_points_without_confirmation_or_resampling():
    points = [[.2, .43], [.4, .96]]
    assert LaneTracker("raw").update([points], 0)[0]["segments"] == [points]


def test_velocity_covariance_is_finite_and_filter_reduces_static_noise():
    rng = np.random.default_rng(4)
    tracker = LaneTracker()
    observed, filtered = [], []
    for i in range(100):
        measured = .3 + rng.normal(0, .005)
        result = tracker.update([lane(measured)], i / 30)
        if i > 10:
            observed.append(measured)
            filtered.append(mean_x(result))
        for track in tracker.tracks:
            eigenvalues = np.linalg.eigvalsh(track.covariance)
            assert np.isfinite(eigenvalues).all() and eigenvalues.min() >= -1e-12
    assert np.std(filtered) < .65 * np.std(observed)
    assert abs(np.mean(filtered) - .3) < .002


def test_gradual_lane_change_tracks_motion_instead_of_locking_old_position():
    tracker = LaneTracker()
    original = None
    for i in range(25):
        result = tracker.update([lane(.3 + i * .002)], i / 30)
        if result:
            original = original or result[0]["track_id"]
            assert result[0]["track_id"] == original
    assert mean_x(result) > .335


def test_optical_flow_aligns_texture_and_rejects_occluded_or_blank_evidence():
    rng = np.random.default_rng(5)
    frame = rng.integers(0, 255, (240, 400), dtype=np.uint8)
    moved = cv2.warpAffine(frame, np.array([[1., 0., 5.], [0., 1., 0.]]), (400, 240))
    output = LaneTracker("raw").update([lane()], 0)
    affine, report = estimate_alignment(frame, moved, output)
    assert report["status"] == "accepted"
    assert affine[0, 2] == pytest.approx(5 / 399, abs=.001)
    assert affine[1, 2] == pytest.approx(0, abs=.001)
    assert estimate_alignment(frame, moved, output, [(0, 0, 1, 1)])[0] is None
    assert estimate_alignment(np.zeros_like(frame), moved, output)[0] is None


def test_flow_prediction_does_not_renew_real_evidence_or_double_count_motion():
    rng = np.random.default_rng(11)
    texture = rng.integers(0, 255, (240, 400), dtype=np.uint8)
    tracker = LaneTracker("flow_kalman")
    for i in range(3):
        frame = cv2.warpAffine(texture, np.array([[1., 0., float(i * 2)], [0., 1., 0.]]), (400, 240))
        output = tracker.update([lane(.3 + i * 2 / 399)] if i < 2 else [], i / 30, frame=frame)
    assert output[0]["last_observed_at"] == pytest.approx(1 / 30)
    assert output[0]["status"] == "predicted"
    assert mean_x(output) == pytest.approx(.3 + 4 / 399, abs=.003)


@pytest.mark.parametrize("change", ("brightness_cut", "resolution"))
def test_camera_changes_reset_flow_tracks(change):
    tracker = LaneTracker("flow_kalman")
    frame = np.zeros((240, 400), np.uint8)
    tracker.update([lane()], 0, frame=frame)
    tracker.update([lane()], .03, frame=frame)
    changed = np.full_like(frame, 255) if change == "brightness_cut" else np.zeros((480, 800), np.uint8)
    assert tracker.update([lane()], .06, frame=changed) == []


@pytest.mark.parametrize("points", ([[[float("nan"), .5], [.3, .6]]], [[[2, .5], [.3, .6]]]))
def test_invalid_geometry_fails_closed(points):
    with pytest.raises(ValueError):
        LaneTracker().update(points, 0)


def test_replay_reports_missing_references_without_claiming_accuracy(tmp_path):
    records = [({"time": i / 30, "lanes": [lane()]}, None) for i in range(5)]
    report = compare(records, tmp_path / "replay", render=False)
    assert report["variants"]["kalman"]["accuracy"] is None
    assert report["inference_ms_median"] is None
    assert len(list(trace_frames(tmp_path / "replay/detections.jsonl", 10))) == 5
    with pytest.raises(FileExistsError):
        compare(records, tmp_path / "replay", render=False)
    assert not (tmp_path / "replay/FAILED.txt").exists()


def test_reference_empty_frames_count_stale_predictions_as_false_positives(tmp_path):
    records = [({"time": i / 30, "lanes": [lane()] if i < 2 else [],
                 "reference": [{"id": "left", **lane()}] if i < 2 else []}, None) for i in range(4)]
    report = compare(records, tmp_path / "scored", render=False)
    assert report["variants"]["raw"]["accuracy"]["outputs_on_empty_reference_frames"] == 0
    assert report["variants"]["kalman"]["accuracy"]["outputs_on_empty_reference_frames"] == 2
    assert json.loads((tmp_path / "scored/report.json").read_text())["frames"] == 4


def test_replay_clock_reset_cannot_create_negative_reacquisition_delays(tmp_path):
    records = [({"time": stamp, "lanes": [lane()] if seen else [],
                 "reference": [{"id": "left", **lane()}]}, None)
               for stamp, seen in ((1., True), (1.03, True), (1.06, False), (0., True), (.03, True))]
    report = compare(records, tmp_path / "clock-reset", render=False)
    for variant in report["variants"].values():
        delay = variant["accuracy"]["mean_reacquisition_seconds"]
        assert delay is None or delay >= 0


def test_preprocessing_matches_training_native_dataset(tmp_path):
    torch = pytest.importorskip("torch")
    from bcdata.ufld_baseline import NativeDataset
    frame = np.random.default_rng(3).integers(0, 255, (590, 1640, 3), dtype=np.uint8)
    cv2.imwrite(str(tmp_path / "frame.png"), frame)
    (tmp_path / "manifest.json").write_text(json.dumps([{"image": "frame.png", "split": "val"}]))
    (tmp_path / "culane_anno_cache.json").write_text(json.dumps({"frame.png": [[[-1e5, -1e5]] * 2] * 4}))
    expected = NativeDataset(tmp_path, "val")[0]["images"].numpy()
    np.testing.assert_array_equal(preprocess(frame), expected)


def test_cli_requires_explicit_local_checkpoint_and_valid_expiry():
    with pytest.raises(SystemExit):
        arguments(["--video", "clip.mp4", "--output", "out"])
    with pytest.raises(SystemExit):
        arguments(["--demo", "--prediction-age", "nan", "--output", "out"])
