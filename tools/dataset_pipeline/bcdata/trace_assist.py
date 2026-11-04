"""Two-click tracing over real saved probability maps; no invented boundaries."""
import json
import time
import uuid

import cv2
import numpy as np

from .bootstrap import atomic_json
from .review_store import ReviewError
from .teachers.base import load_output

MODELS = {"comma_segnet_fresh": "comma_segnet", "sam3": "sam3"}


def trace_probability(probability, seeds, *, layer="lanes", side="right", occlusion=None):
    probability = np.asarray(probability, dtype=np.float32)
    if probability.ndim != 2 or not np.isfinite(probability).all():
        raise ValueError("Invalid model probability map")
    h, w = probability.shape
    seeds = np.asarray(seeds, dtype=float)
    if seeds.shape != (2, 2) or not np.isfinite(seeds).all() or any(
        not (0 <= x < w and 0 <= y < h) for x, y in seeds
    ):
        raise ValueError("Tap two points inside the photo")
    if layer not in ("lanes", "edges") or side not in ("left", "right"):
        raise ValueError("Invalid tracing target")
    near, far = sorted(seeds, key=lambda point: -point[1])
    span = near[1] - far[1]
    if span < 12:
        raise ValueError("Tap a near point and a farther point on the same boundary")
    radius = max(12, int(w * .045))
    step = max(2, h // 240)
    runs, current, confidence = [], [], []
    def finish():
        nonlocal current
        if current:
            runs.append(current)
            current = []
    for y in range(int(near[1]), int(far[1]) - 1, -step):
        guide = far[0] + (near[0] - far[0]) * (y - far[1]) / span
        low, high = max(1, int(guide) - radius), min(w - 2, int(guide) + radius)
        if high <= low:
            continue
        xs = np.arange(low, high + 1)
        values = probability[y, xs]
        if layer == "lanes":
            scores = values - .35 * np.abs(xs - guide) / radius
            index = int(scores.argmax())
            if values[index] < .30:
                continue
            start = end = index
            cutoff = max(.30, values[index] * .6)
            while start and values[start - 1] >= cutoff:
                start -= 1
            while end < len(xs) - 1 and values[end + 1] >= cutoff:
                end += 1
            x = float(np.average(xs[start:end + 1], weights=values[start:end + 1]))
            strength = float(values[index])
        else:
            gradient = probability[y, xs + 1] - probability[y, xs - 1]
            if side == "right":
                gradient = -gradient
            index = int((gradient - .04 * np.abs(xs - guide) / radius).argmax())
            if gradient[index] < .04:
                continue
            x, strength = float(xs[index]), float(gradient[index])
        if occlusion is not None and occlusion[y, int(round(x))] >= .5:
            finish()
            continue
        if current and (current[-1][1] - y > .12 * h or abs(x - current[-1][0]) > 4 * (current[-1][1] - y) + 8):
            finish()
        current.append([x, float(y)])
        confidence.append(strength)
    finish()
    eligible = [run for run in runs if len(run) >= 4 and run[0][1] - run[-1][1] >= max(12, .55 * span)
                and len(run) * step >= .20 * span]
    if not eligible:
        raise ValueError("No reliable model trace between those taps. Try a shorter span or draw this boundary manually.")
    best = max(eligible, key=lambda run: run[0][1] - run[-1][1])
    points = cv2.approxPolyDP(np.asarray(best, np.float32).reshape(-1, 1, 2), 1.5, False).reshape(-1, 2).tolist()
    return points, (best[0][1] - best[-1][1]) / span


def create_assist(store, image_id, revision, owner, seeds, layer, side):
    settings = store.settings
    with store.registry() as reg:
        row = store._check_image(reg, image_id)
        if store._revision(reg, image_id) != revision:
            raise ReviewError("This photo changed. Reload before tracing.", 409)
        results = []
        for name in MODELS:
            annotation = reg.annotation(image_id, "teacher", name)
            parent_path = settings.path("lane_geometry", "teachers", name, image_id + ".json")
            if annotation is None or not parent_path.is_file():
                continue
            parent = json.loads(parent_path.read_text(encoding="utf-8"))
            if parent.get("teacher_version") != annotation["model_version"]:
                continue
            output = load_output(annotation["path"])
            probability = output.lane if layer == "lanes" else output.road
            if probability is None or probability.shape != (row["height"], row["width"]):
                continue
            try:
                points, support = trace_probability(probability, seeds, layer=layer, side=side,
                    occlusion=output.extra.get("movable", output.extra.get("occlusion")))
                results.append((support, points, name, parent))
            except ValueError:
                continue
    if not results:
        raise ReviewError("No reliable cached model trace here yet. Try a shorter span or draw this boundary manually.", 422)
    support, points, name, parent = max(results, key=lambda result: result[0])
    assist_id = str(uuid.uuid4())
    record = {"assist_id": assist_id, "image_id": image_id, "revision": revision, "owner": owner,
              "model": name, "version": parent["teacher_version"], "license": MODELS[name],
              "layer": layer, "side": side, "points": points, "seeds": seeds,
              "created_at": time.time(), "method": "two_tap_cached_probability_trace_v1"}
    atomic_json(settings.path("annotations", "assisted_traces", image_id, assist_id + ".json"), record)
    return {key: value for key, value in record.items() if key != "owner"}


def validate_assists(settings, image_id, revision, owner, assist_ids):
    records, licenses = [], set()
    seen = set()
    for value in assist_ids:
        try:
            identifier = str(uuid.UUID(value))
        except (ValueError, TypeError, AttributeError):
            raise ReviewError("Invalid assisted-trace reference") from None
        if identifier in seen:
            continue
        seen.add(identifier)
        path = settings.path("annotations", "assisted_traces", image_id, identifier + ".json")
        if not path.is_file():
            raise ReviewError("Assisted trace is missing. Trace again before saving.")
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["image_id"] != image_id or record["revision"] != revision or record["owner"] != owner:
            raise ReviewError("Assisted trace belongs to another image, reviewer or revision", 409)
        parent = settings.path("lane_geometry", "teachers", record["model"], image_id + ".json")
        if not parent.exists() or json.loads(parent.read_text(encoding="utf-8")).get("teacher_version") != record["version"]:
            raise ReviewError("The tracing model changed. Trace again before saving.", 409)
        records.append({key: value for key, value in record.items() if key != "owner"})
        licenses.add(record["license"])
    return records, licenses
