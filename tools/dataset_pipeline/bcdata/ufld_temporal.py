"""Offline UFLDv2 temporal ablation; never changes training or app models.

Run --help for video inference, cached JSONL traces, and a synthetic smoke demo.
"""
import argparse
from contextlib import nullcontext
import hashlib
import json
import math
from pathlib import Path
import sys
import time

import cv2
import numpy as np
from scipy.optimize import linear_sum_assignment

from .lane_temporal import LaneTracker, MODES, TrackingConfig, sample_lane


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def preprocess(frame):
    """Match NativeDataset: canonical full frame, bottom .6 crop, ImageNet RGB."""
    image = cv2.resize(frame, (1640, 590), interpolation=cv2.INTER_LINEAR)
    image = cv2.resize(image, (1600, int(320 / .6)), interpolation=cv2.INTER_LINEAR)[-320:]
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float32) / 255
    image = (image - np.array([.485, .456, .406], np.float32)) / np.array([.229, .224, .225], np.float32)
    return np.ascontiguousarray(image.transpose(2, 0, 1))


def video_frames(args):
    """Infer once per sampled frame; all four variants consume identical lanes."""
    import subprocess
    import torch
    from . import ufld_baseline as baseline
    saved = torch.load(args.checkpoint, map_location="cpu", weights_only=False, mmap=True)
    baseline.validate_warm_start_config(saved["config"])
    revision = subprocess.check_output(["git", "-C", str(args.vendor), "rev-parse", "HEAD"], text=True).strip()
    if revision != baseline.UPSTREAM:
        raise ValueError("Vendor revision does not match the trained UFLDv2 contract")
    # A dirty implementation can silently invalidate checkpoint compatibility.
    dirty = subprocess.check_output(["git", "-C", str(args.vendor), "diff", "--name-only", "HEAD", "--", "model"], text=True)
    if dirty.strip():
        raise ValueError("UFLDv2 vendor model files have uncommitted changes")
    model = baseline.upstream_model(args.vendor, pretrained=False)
    model.load_state_dict(saved["model"], strict=True)
    model.to(args.device).eval()
    capture = cv2.VideoCapture(str(args.video))
    if not capture.isOpened():
        raise ValueError("Cannot open input video")
    fps = capture.get(cv2.CAP_PROP_FPS)
    if not math.isfinite(fps) or fps <= 0:
        capture.release()
        raise ValueError("Video needs a valid frame rate")
    index, emitted, previous_time = 0, 0, -1.
    try:
        while emitted < args.max_frames:
            ok, frame = capture.read()
            if not ok:
                break
            frame_index = index
            index += 1
            if frame_index % args.stride:
                continue
            stamp = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000
            if not math.isfinite(stamp) or stamp <= previous_time:
                stamp = frame_index / fps
            if stamp <= previous_time:
                raise ValueError("Video timestamp did not increase")
            previous_time = stamp
            started = time.perf_counter()
            with torch.inference_mode():
                pred = model(torch.from_numpy(preprocess(frame)).unsqueeze(0).to(args.device))
                decoded = baseline.decode(pred, 0, args.decoder)
            if args.device == "cuda":
                torch.cuda.synchronize()
            duration = (time.perf_counter() - started) * 1000
            lanes = [{"points": (np.asarray(line) / [baseline.WIDTH, baseline.HEIGHT]).tolist()}
                     for line in decoded]
            yield {"time": stamp, "clip_id": args.video.stem, "frame_index": frame_index,
                   "lanes": lanes, "inference_ms": duration}, frame
            emitted += 1
    finally:
        capture.release()


def trace_frames(path, max_frames):
    with path.open(encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            if index >= max_frames:
                break
            record = json.loads(line)
            if not isinstance(record, dict) or "time" not in record or "lanes" not in record:
                raise ValueError(f"Invalid trace frame at line {index + 1}")
            frame = None
            if record.get("image"):
                image_path = Path(record["image"])
                if not image_path.is_absolute():
                    image_path = path.parent / image_path
                frame = cv2.imread(str(image_path))
                if frame is None:
                    raise ValueError("Missing trace image: " + str(image_path))
            yield record, frame


def demo_frames(count=150):
    """Deterministic labeled, textured replay. Not real-world validation."""
    rng = np.random.default_rng(71)
    height, width = 360, 640
    texture = rng.integers(15, 75, (height, width), dtype=np.uint8)
    rows = np.linspace(.42, .96, 40)
    for i in range(count):
        phase = i % 75
        offset = .018 * math.sin(i / 18)
        # Two clips, a gradual bend, a short occlusion, outlier and long loss.
        scene = i // 75
        reference = []
        if phase < 61:
            for lane_id, base in (("left", .31), ("right", .69)):
                x = base + offset + .12 * (rows - .7) ** 2
                reference.append({"id": lane_id, "points": np.column_stack([x, rows]).tolist()})
        frame = cv2.cvtColor(cv2.warpAffine(texture, np.array([[1., 0., offset * width], [0., 1., 0.]]),
                                           (width, height)), cv2.COLOR_GRAY2BGR)
        for lane in reference:
            pixels = np.round(np.asarray(lane["points"]) * [width - 1, height - 1]).astype(np.int32)
            cv2.polylines(frame, [pixels], False, (220, 220, 220), 3)
        detections = []
        if phase not in (25, 26, 27, 28):
            for lane in reference:
                points = np.asarray(lane["points"]).copy()
                points[:, 0] += rng.normal(0, .006) + rng.normal(0, .001, len(points))
                if phase == 43 and lane["id"] == "left":
                    points[:, 0] += .17
                detections.append({"points": points.tolist()})
        if i % 2:
            detections.reverse()  # detector ordering is not lane identity
        yield {"time": phase / 30, "clip_id": f"synthetic-{scene}", "frame_index": i,
               "lanes": detections, "reference": reference, "scene_cut": phase == 0}, frame


def _lane_x(lane, rows):
    x = np.full(len(rows), np.nan)
    for run in lane["segments"]:
        values = sample_lane(run, rows, .065)
        valid = np.isfinite(values)
        x[valid] = values[valid]
    return x


def _raster(lane, width=820, height=295):
    mask = np.zeros((height, width), np.uint8)
    for run in lane["segments"]:
        if len(run) >= 2:
            points = np.round(np.asarray(run) * [width - 1, height - 1]).astype(np.int32)
            cv2.polylines(mask, [points], False, 1, 15)
    return mask.astype(bool)


class Metrics:
    def __init__(self, rows, mode):
        self.rows, self.mode = rows, mode
        self.reference_ids = True
        self.frames = self.labeled = self.tp = self.fp = self.fn = 0
        self.predicted = self.predicted_rows = self.switches = 0
        self.empty_reference_outputs = self.alignment_accepted = 0
        self.latencies, self.errors, self.residual_changes = [], [], []
        self.previous = {}
        self.reacquisition_delays = []
        self.missing_since = {}

    def update(self, record, output, milliseconds, alignment):
        self.frames += 1
        self.latencies.append(milliseconds)
        self.predicted += sum(lane["status"] == "predicted" for lane in output)
        self.predicted_rows += sum(len(lane["predicted_rows"]) for lane in output)
        self.alignment_accepted += alignment.get("status") == "accepted"
        if "reference" not in record:
            return
        self.labeled += 1
        clip = record.get("clip_id", "clip")
        stamp = float(record["time"])
        reference = []
        identities = set()
        for i, r in enumerate(record["reference"]):
            sample_lane(r["points"], self.rows, .065)  # validate normalized reference geometry
            self.reference_ids &= "id" in r
            identity = r.get("id", i)
            if identity in identities:
                raise ValueError("Reference boundary IDs must be unique within each frame")
            identities.add(identity)
            reference.append({"segments": [r["points"]], "id": identity})
        p, r = [_raster(lane) for lane in output], [_raster(lane) for lane in reference]
        overlaps = np.zeros((len(p), len(r)))
        for i, a in enumerate(p):
            for j, b in enumerate(r):
                overlaps[i, j] = np.count_nonzero(a & b) / max(1, np.count_nonzero(a | b))
        matched = set()
        if overlaps.size:
            a, b = linear_sum_assignment(-overlaps)
            for i, j in zip(a, b):
                if overlaps[i, j] < .5:
                    continue
                self.tp += 1
                matched.add(j)
                key = (clip, reference[j]["id"])
                px, rx = _lane_x(output[i], self.rows), _lane_x(reference[j], self.rows)
                residual = px - rx
                valid = np.isfinite(residual)
                self.errors.extend(np.abs(residual[valid]).tolist())
                previous = self.previous.get(key)
                if previous is not None and stamp > previous[0] and previous[3] == self.frames - 1:
                    shared = valid & np.isfinite(previous[1])
                    self.residual_changes.extend((residual[shared] - previous[1][shared]).tolist())
                    if previous[2] is not None and output[i]["track_id"] != previous[2]:
                        self.switches += 1
                self.previous[key] = (stamp, residual, output[i]["track_id"], self.frames)
                if key in self.missing_since:
                    self.reacquisition_delays.append(stamp - self.missing_since.pop(key))
        self.fp += len(output) - len(matched)
        self.fn += len(reference) - len(matched)
        if not reference:
            self.empty_reference_outputs += len(output)
        for j, lane in enumerate(reference):
            if j not in matched:
                self.missing_since.setdefault((clip, lane["id"]), stamp)
        # A ground-truth lane leaving view is not a detector reacquisition event.
        active = {(clip, lane["id"]) for lane in reference}
        self.missing_since = {k: v for k, v in self.missing_since.items() if k in active}

    def report(self):
        accuracy = None
        if self.labeled:
            accuracy = {"tp": self.tp, "fp": self.fp, "fn": self.fn,
                        "lane_f1": 2 * self.tp / max(1, 2 * self.tp + self.fp + self.fn),
                        "mean_abs_x_error": float(np.mean(self.errors)) if self.errors else None,
                        "residual_change_rms": float(np.sqrt(np.mean(np.square(self.residual_changes))))
                        if self.residual_changes else None,
                        "consecutive_frame_id_switches": self.switches
                        if self.mode != "raw" and self.reference_ids else None,
                        "outputs_on_empty_reference_frames": self.empty_reference_outputs,
                        "mean_reacquisition_seconds": float(np.mean(self.reacquisition_delays))
                        if self.reacquisition_delays else None}
        return {"frames": self.frames, "labeled_frames": self.labeled, "accuracy": accuracy,
                "predicted_lane_outputs": self.predicted, "predicted_row_outputs": self.predicted_rows,
                "alignment_accepted_frames": self.alignment_accepted,
                "tracker_ms_median": float(np.median(self.latencies)),
                "tracker_ms_p95": float(np.percentile(self.latencies, 95))}


def draw_panel(frame, outputs, labels, stamp):
    panels = []
    for mode in MODES:
        panel = cv2.resize(frame, (640, 360)) if frame is not None else np.zeros((360, 640, 3), np.uint8)
        if labels is not None:
            for lane in labels:
                points = np.round(np.asarray(lane["points"]) * [639, 359]).astype(np.int32)
                cv2.polylines(panel, [points], False, (100, 100, 100), 5)
        for lane in outputs[mode]:
            color = (0, 200, 255) if lane["status"] == "predicted" else (90, 240, 100)
            predicted = lane["predicted_rows"]
            for run in lane["segments"]:
                points = np.round(np.asarray(run) * [639, 359]).astype(np.int32)
                for k in range(len(points) - 1):
                    predicted_part = (lane["status"] == "predicted"
                                      or any(abs(y - run[k][1]) < 1e-7 or abs(y - run[k + 1][1]) < 1e-7
                                             for y in predicted))
                    cv2.line(panel, tuple(points[k]), tuple(points[k + 1]),
                             (0, 200, 255) if predicted_part else color, 2)
        cv2.rectangle(panel, (0, 0), (640, 34), (20, 20, 20), -1)
        cv2.putText(panel, f"{mode}   capture {stamp:.3f}s", (12, 24), cv2.FONT_HERSHEY_SIMPLEX, .6, (240, 240, 240), 1)
        panels.append(panel)
    return np.vstack([np.hstack(panels[:2]), np.hstack(panels[2:])])


def compare(frames, output, config=None, *, render=True, metadata=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    trackers = {mode: LaneTracker(mode, config) for mode in MODES}
    metrics = {mode: Metrics(trackers[mode].rows, mode) for mode in MODES}
    writer = None
    count = 0
    inference_ms = []
    source_times = []
    previous_clip = object()
    previous_stamp = None
    previous_shape = None
    try:
        with (output / "comparison.jsonl").open("w", encoding="utf-8") as stream, \
                (output / "detections.jsonl").open("w", encoding="utf-8") as raw:
            for record, frame in frames:
                stamp = float(record["time"])
                clip = record.get("clip_id", "clip")
                clip_reset = clip != previous_clip or record.get("scene_cut", False)
                clip_reset |= previous_stamp is not None and (stamp <= previous_stamp
                              or stamp - previous_stamp > trackers["kalman"].config.reset_gap)
                shape = frame.shape if frame is not None else None
                clip_reset |= shape is not None and previous_shape is not None and shape != previous_shape
                previous_clip = clip
                previous_stamp, previous_shape = stamp, shape
                if clip_reset:
                    for metric in metrics.values():
                        metric.previous.clear()
                        metric.missing_since.clear()
                results = {}
                for mode, tracker in trackers.items():
                    started = time.perf_counter()
                    results[mode] = tracker.update(record["lanes"], stamp,
                                                   frame=frame,
                                                   scene_id=clip, scene_cut=clip_reset,
                                                   boxes=record.get("boxes", []))
                    elapsed = (time.perf_counter() - started) * 1000
                    metrics[mode].update(record, results[mode], elapsed, tracker.alignment)
                source = dict(record)
                if frame is not None:
                    # Cache the actual frame so the motion experiment can be
                    # reproduced without rerunning the neural detector.
                    directory = output / "frames"
                    directory.mkdir(exist_ok=True)
                    name = f"frames/{count:06d}.png"
                    if not cv2.imwrite(str(output / name), frame):
                        raise IOError("Could not cache replay frame")
                    source["image"] = name
                raw.write(json.dumps(source, allow_nan=False) + "\n")
                stream.write(json.dumps({"time": stamp, "clip_id": clip, "variants": results,
                                         "alignment": trackers["flow_kalman"].alignment}, allow_nan=False) + "\n")
                if record.get("inference_ms") is not None:
                    inference_ms.append(record["inference_ms"])
                source_times.append({"clip_id": clip, "time": stamp})
                if render:
                    panel = draw_panel(frame, results, record.get("reference"), stamp)
                    if writer is None:
                        writer = cv2.VideoWriter(str(output / "comparison.mp4"), cv2.VideoWriter_fourcc(*"mp4v"),
                                                 30., (1280, 720))
                        if not writer.isOpened():
                            raise IOError("Cannot create comparison video")
                    writer.write(panel)
                    if count in (0, 27, 44, 65):
                        cv2.imwrite(str(output / f"preview-{count:04d}.png"), panel)
                count += 1
        if not count:
            raise ValueError("Replay has no frames")
        report = {"schema": "ufld_temporal_comparison:v1", "frames": count,
                  "metadata": metadata or {}, "settings": trackers["kalman"].settings(),
                  "implementation": {"tracker_sha256": digest(Path(__file__).with_name("lane_temporal.py")),
                                     "runner_sha256": digest(Path(__file__)),
                                     "python": sys.version.split()[0], "opencv": cv2.__version__,
                                     "numpy": np.__version__},
                  "variants": {mode: metric.report() for mode, metric in metrics.items()},
                  "inference_ms_median": float(np.median(inference_ms)) if inference_ms else None,
                  "source_timestamps": source_times,
                  "interpretation": ["Experimental offline comparison; no model promotion or training changes.",
                                     "Position errors use normalized full-frame horizontal coordinates.",
                                     "F1 uses 820x295 raster lanes, 15px width, IoU >= 0.5; not official CULane.",
                                     "Residual-change RMS uses ground-truth-relative errors, not raw coordinate smoothness.",
                                     "ID switches require reference IDs and consecutive matched frames; raw is unavailable.",
                                     "Preview plays one sampled frame per 1/30s; use capture timestamps for source timing.",
                                     "CPU tracker timings exclude detector, rendering, caching and metrics; not iPhone latency.",
                                     "Predicted geometry is experimental and must not drive warnings or training labels."]}
        (output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
        return report
    except Exception as error:
        (output / "FAILED.txt").write_text(str(error), encoding="utf-8")
        raise
    finally:
        if writer is not None:
            writer.release()


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--demo", action="store_true", help="Synthetic smoke test, not driving validation")
    source.add_argument("--trace", type=Path, help="Cached JSONL with normalized lanes and capture timestamps")
    source.add_argument("--video", type=Path, help="Local driving video; requires a trusted local checkpoint")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--vendor", type=Path, default=Path(__file__).resolve().parents[1] / "vendor" / "ufld_v2")
    parser.add_argument("--decoder", choices=("stock", "short_span_v1", "short_span_geometry_v2", "short_span_geometry_v3"), default="stock")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--output", required=True, type=Path, help="New directory; existing artifacts are never overwritten")
    parser.add_argument("--max-frames", type=int, default=300)
    parser.add_argument("--stride", type=int, default=1)
    parser.add_argument("--prediction-age", type=float, default=.25)
    parser.add_argument("--no-render", action="store_true")
    args = parser.parse_args(argv)
    if args.max_frames < 1 or args.stride < 1:
        parser.error("max-frames and stride must be positive")
    if args.video and not args.checkpoint:
        parser.error("--video requires --checkpoint (trusted local weights only)")
    if args.checkpoint and not args.video:
        parser.error("--checkpoint is only used with --video")
    try:
        TrackingConfig(max_prediction_age=args.prediction_age)
    except ValueError as error:
        parser.error(str(error))
    return args


def main(argv=None):
    args = arguments(argv)
    metadata = {"source_kind": "synthetic" if args.demo else "video" if args.video else "cached_trace",
                "decoder": args.decoder if args.video else "predecoded",
                "real_driving_validation": False, "accuracy_requires_references": True}
    if args.demo:
        frames = demo_frames(min(args.max_frames, 150))
    elif args.trace:
        frames = trace_frames(args.trace.resolve(), args.max_frames)
        metadata.update(source=str(args.trace.resolve()), source_sha256=digest(args.trace))
    else:
        metadata.update(source=str(args.video.resolve()), source_sha256=digest(args.video),
                        checkpoint=str(args.checkpoint.resolve()), checkpoint_sha256=digest(args.checkpoint),
                        device=args.device, stride=args.stride)
        frames = video_frames(args)
    lock = nullcontext()
    if args.video and args.device == "cuda":
        from filelock import FileLock
        from .local import local_settings
        lock = FileLock(str(local_settings().path("logs", "large-models.lock")), timeout=0)
    with lock:
        report = compare(frames, args.output, TrackingConfig(max_prediction_age=args.prediction_age),
                         render=not args.no_render, metadata=metadata)
    print(json.dumps({"output": str(args.output.resolve()), "frames": report["frames"],
                      "variants": report["variants"]}, indent=2))


if __name__ == "__main__":
    main()
