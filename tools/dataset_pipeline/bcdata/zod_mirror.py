"""Isolated ZOD mirror inventory and provisional polygon-to-trace conversion.

Original polygons, missing annotations, resolution identity and human edits are
kept distinct. No source here is admitted to the cleared training collection.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

import numpy as np

SOURCE = "zod_mirror"
CONVERTER = "zod-polygon-observed-curves-v2"
NATIVE_WIDTH, NATIVE_HEIGHT = 3848, 2168
WIDTH, HEIGHT = 1280, 720
CLASSES = {"lm_solid", "lm_dashed", "lm_botts_dot"}
ID = re.compile(r"zod-[0-9]{6}\Z")


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def connect(root):
    Path(root).mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(Path(root) / "audits.sqlite", timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS frames (
        image_id TEXT PRIMARY KEY, frame_id TEXT NOT NULL UNIQUE,
        native_json TEXT, native_sha TEXT, geometry_json TEXT NOT NULL,
        geometry_sha TEXT NOT NULL, qc_json TEXT NOT NULL,
        image_path TEXT, image_sha TEXT, image_ready INTEGER NOT NULL DEFAULT 0,
        correction_json TEXT, correction_sha TEXT, correction_reviewer TEXT,
        correction_time REAL, resolved_json TEXT NOT NULL DEFAULT '[]',
        revision INTEGER NOT NULL DEFAULT 0, action TEXT, reviewer TEXT,
        decision_fingerprint TEXT, decision_time REAL);
      CREATE INDEX IF NOT EXISTS frames_queue ON frames(image_ready,action,image_id);
      CREATE TABLE IF NOT EXISTS audit_events (
        event_id TEXT PRIMARY KEY, image_id TEXT NOT NULL, payload TEXT NOT NULL,
        created_at REAL NOT NULL);
    """)
    return db


def rings(item):
    geometry = item.get("geometry") or {}
    coordinates = geometry.get("coordinates", [])
    if geometry.get("type") == "Polygon":
        return coordinates[:1]
    if geometry.get("type") == "MultiPolygon":
        return [polygon[0] for polygon in coordinates if polygon]
    return []


def centerline(points, *, every_row=False):
    """Midpoints inside observed polygon row intersections; no extension."""
    polygon = np.asarray(points, dtype=float)
    if polygon.ndim != 2 or polygon.shape[1] != 2 or not 3 <= len(polygon) <= 20000 or not np.isfinite(polygon).all():
        raise ValueError("Invalid paint polygon")
    polygon = polygon / [NATIVE_WIDTH / WIDTH, NATIVE_HEIGHT / HEIGHT]
    if np.any(polygon < 0) or np.any(polygon[:, 0] >= WIDTH) or np.any(polygon[:, 1] >= HEIGHT):
        raise ValueError("Paint polygon exceeds provisional source dimensions")
    if not np.array_equal(polygon[0], polygon[-1]):
        polygon = np.concatenate([polygon, polygon[:1]])
    low, high = float(polygon[:, 1].min()), float(polygon[:, 1].max())
    if high - low < 1:
        return [], polygon.tolist()
    ys = (np.arange(math.ceil(low + .01), math.floor(high - .01) + 1) if every_row else
          np.linspace(low + .1, high - .1, min(220, max(2, int((high - low) / 3) + 1))))
    a, b = polygon[:-1], polygon[1:]
    if every_row:
        selected = (a[None, :, 1] > ys[:, None]) != (b[None, :, 1] > ys[:, None])
        delta = b[:, 1] - a[:, 1]
        fraction = np.divide(ys[:, None] - a[None, :, 1], delta[None, :],
                             out=np.zeros(selected.shape, dtype=float), where=delta[None, :] != 0)
        xs = a[None, :, 0] + fraction * (b - a)[None, :, 0]
        valid = selected.sum(axis=1) >= 2
        if not valid.any():
            return [], polygon.tolist()
        low_x = np.where(selected, xs, np.inf).min(axis=1)[valid]
        high_x = np.where(selected, xs, -np.inf).max(axis=1)[valid]
        if np.any(selected.sum(axis=1) > 2) or np.any(high_x - low_x > WIDTH * .10):
            return [], polygon.tolist()
        return np.column_stack([(low_x + high_x) / 2, ys[valid]]).tolist(), polygon.tolist()
    samples = []
    for y in ys:
        selected = (a[:, 1] > y) != (b[:, 1] > y)
        x = a[selected, 0] + (y - a[selected, 1]) * (b[selected, 0] - a[selected, 0]) / (b[selected, 1] - a[selected, 1])
        if len(x) < 2:
            continue
        x.sort()
        if len(x) > 2:
            # Multiple disconnected intervals must not be replaced by a line
            # through their empty middle. Keep only an unambiguous paint strip.
            return [], polygon.tolist()
        if x[1] - x[0] > WIDTH * .10:
            return [], polygon.tolist()
        samples.append([float(x.mean()), float(y)])
    return samples, polygon.tolist()


def convert_legacy(native, image_id):
    if not ID.fullmatch(image_id):
        raise ValueError("Invalid ZOD frame ID")
    warnings, sections, polygons = set(), [], []
    if not isinstance(native, list):
        native = []
        warnings.add("missing_or_invalid_native_lane_annotations")
    class_counts = Counter()
    for item in native:
        if not isinstance(item, dict):
            warnings.add("invalid_native_paint_shape")
            continue
        props = item.get("properties") or {}
        cls = props.get("class", "road_paint_other")
        class_counts[cls] += 1
        for ring in rings(item):
            try:
                trace, polygon = centerline(ring)
            except (ValueError, TypeError):
                warnings.add("invalid_or_out_of_bounds_native_polygon")
                continue
            polygons.append({"class": cls, "points": polygon, "annotation_uuid": props.get("annotation_uuid")})
            # Text, arrows, shaded regions and unclassified paint do not erase
            # valid lane annotations elsewhere in this same frame.
            if cls not in CLASSES:
                continue
            if len(trace) < 2:
                warnings.add("paint_polygon_needs_manual_trace")
                continue
            if max(p[0] for p in trace) - min(p[0] for p in trace) > 8 * (trace[-1][1] - trace[0][1]):
                warnings.add("near_transverse_paint_needs_review")
                continue
            sections.append({"points": trace, "class": cls, "coloured": props.get("coloured", False)})

    # Associate disjoint, geometrically compatible painted pieces. Their gaps
    # stay separate in segments, including dashed paint and vehicle occlusions.
    groups = []
    for section in sorted(sections, key=lambda s: s["points"][-1][1], reverse=True):
        matches = []
        for group in groups:
            previous = group["segments"][-1]
            gap = previous[0][1] - section["points"][-1][1]
            if (group["class"] != section["class"] or group["coloured"] != section["coloured"]
                    or gap < 0 or gap > HEIGHT * .30):
                continue
            span = previous[-1][1] - previous[0][1]
            slope = (previous[-1][0] - previous[0][0]) / max(span, 1)
            expected = previous[0][0] - slope * gap
            difference = abs(expected - section["points"][-1][0])
            if difference <= max(8, gap * .18):
                matches.append((difference, group))
        matches.sort(key=lambda pair: pair[0])
        if len(matches) > 1 and matches[1][0] - matches[0][0] < 4:
            warnings.add("ambiguous_dashed_piece_grouping")
            matches = []
        if matches:
            matches[0][1]["segments"].append(section["points"])
        else:
            groups.append({"class": section["class"], "coloured": section["coloured"], "segments": [section["points"]]})

    def position(group):
        near = group["segments"][0]
        span = near[-1][1] - near[0][1]
        slope = (near[-1][0] - near[0][0]) / max(span, 1)
        gap = HEIGHT * .9 - near[-1][1]
        if abs(gap) > HEIGHT * .25:
            warnings.add("lane_slot_requires_long_extrapolation")
        return near[-1][0] + slope * min(HEIGHT * .25, max(-HEIGHT * .25, gap))

    located = sorted([(position(group), group) for group in groups], key=lambda pair: pair[0])
    left = [pair for pair in located if pair[0] < WIDTH / 2]
    right = [pair for pair in located if pair[0] >= WIDTH / 2]
    chosen = [(2, left[-1])] if left else []
    if len(left) > 1:
        chosen.append((1, left[-2]))
    if right:
        chosen.append((3, right[0]))
    if len(right) > 1:
        chosen.append((4, right[1]))
    if len(groups) > 4:
        warnings.add("more_than_four_boundary_groups")
    for (x, _), (other, _) in zip(located, located[1:]):
        if abs(x - other) < WIDTH * .025:
            warnings.add("possible_double_stripe_or_duplicate_boundary")
    lanes = []
    for slot, (_, group) in sorted(chosen):
        segments = sorted(group["segments"], key=lambda points: points[0][1])
        points = [point for segment in segments for point in segment]
        if len(points) > 2048:
            warnings.add("boundary_exceeds_editor_point_limit")
            continue
        lanes.append({"slot": slot, "points": points, "segments": segments,
                      "physical": True, "kind": "lane_marking", "origin": "source_polygon_conversion",
                      "marking_class": group["class"]})
    if not lanes:
        warnings.add("no_converted_lane_requires_whole_photo_check")
    geometry = {"image_id": image_id, "width": WIDTH, "height": HEIGHT,
                "version": "zod-polygon-traces-v1", "lanes": lanes,
                "source_polygons": polygons, "source_class_counts": dict(class_counts),
                "coordinate_transform": {"native_width": NATIVE_WIDTH, "native_height": NATIVE_HEIGHT,
                                         "status": "provisional_official_sensor_dimensions_check_overlay"},
                "annotation_policy": "observed_polygon_sections_no_gap_fill_no_endpoint_extension"}
    return geometry, sorted(warnings)


def convert(native, image_id):
    from .zod_converter_v2 import convert_sections
    return convert_sections(native, image_id)


def insert_annotation(db, frame_id, raw):
    image_id = "zod-" + frame_id
    existing = db.execute("SELECT native_sha FROM frames WHERE image_id=?", (image_id,)).fetchone()
    expected = sha(raw)
    if existing:
        if existing[0] != expected:
            raise ValueError("Native annotations changed for " + image_id)
        return False
    try:
        native = json.loads(raw)
    except ValueError:
        native = None
    geometry, warnings = convert(native, image_id)
    geometry_json = encoded(geometry)
    db.execute("INSERT INTO frames(image_id,frame_id,native_json,native_sha,geometry_json,geometry_sha,qc_json) VALUES(?,?,?,?,?,?,?)",
               (image_id, frame_id, raw.decode("utf-8"), expected, geometry_json, sha(geometry_json.encode()), encoded(warnings)))
    return True
