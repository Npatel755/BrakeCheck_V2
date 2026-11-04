"""Evidence for a bounded distal slope warning on native longitudinal paint.

This never edits geometry or proves human approval. The caller must separately
retain all other QC, source-retention, regression and fresh-audit requirements.
"""
from __future__ import annotations

import math

import cv2
import numpy as np

from .geometry import lane_xs

VERSION = "native-distal-tip-evidence-v1"
TIP_ROWS = 8  # Native image rows, deliberately not expanded with image height.
CONTINUATION_ROWS = 32


def measure(geometry, probability, native_evidence):
    """Return source-only evidence for every excessive-slope pair, else None.

    `native_evidence` must be the current source_target_evidence.measure result
    for these exact inputs. No local slope exception can replace its complete
    coverage and topology gates. Inferred/hidden points cannot support a tip.
    """
    try:
        if (not isinstance(native_evidence, dict)
                or native_evidence.get("eligible_for_fresh_audit") is not True
                or native_evidence.get("version") != "native-four-target-evidence-v2"
                or any(native_evidence.get(key) for key in (
                    "unrepresented_components", "unhandled_converter_warnings", "crossings", "near_duplicate_slot_pairs"))):
            return None
        for key in ("represented_fraction", "observed_points_on_paint"):
            value = native_evidence.get(key)
            if (isinstance(value, (bool, np.bool_))
                    or not isinstance(value, (int, float, np.integer, np.floating))
                    or not math.isfinite(value) or not .98 <= value <= 1.):
                return None
        width, height = geometry["width"], geometry["height"]
        if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
            return None
        paint = np.asarray(probability) >= .5
        if paint.shape != (height, width):
            return None
        paint = paint.copy()
        paint[:int(geometry["horizon_y"])] = False
        paint[int(geometry["valid_bottom"]) + 1:] = False
        _, labels, stats, _ = cv2.connectedComponentsWithStats(paint.astype(np.uint8), 8)
        verified = []
        seen_slots = set()
        for lane in geometry["lanes"]:
            slot = lane.get("slot")
            if slot is None:
                continue
            if type(slot) is not int or slot not in (1, 2, 3, 4):
                return None
            if slot in seen_slots or lane.get("physical", True) is not True:
                return None
            seen_slots.add(slot)
            points = lane.get("points", [])
            bad = []
            for first, second in zip(points, points[1:]):
                if (len(first) < 2 or len(second) < 2
                        or not all(not isinstance(value, (bool, np.bool_)) and math.isfinite(value)
                                   for value in (*first[:2], *second[:2]))):
                    return None
                dx, dy = abs(first[0] - second[0]), abs(first[1] - second[1])
                if dx > 6 * max(1., dy):
                    bad.append((first, second))
            if not bad:
                continue
            top, bottom = lane["y_top"], lane["y_bottom"]
            if (not math.isfinite(top) or not math.isfinite(bottom)
                    or bottom - top < max(40., .08 * height)
                    or top < 0 or bottom >= height):
                return None
            for first, second in bad:
                if any(not top <= point[1] <= top + TIP_ROWS
                       or len(point) > 2 and point[2] is not True for point in (first, second)):
                    return None
            if any(len(point) > 2 and point[2] is not True
                   for point in points if top <= point[1] <= top + TIP_ROWS):
                return None
            lo = int(math.ceil(top))
            tip_end = int(math.floor(top + TIP_ROWS))
            end = tip_end + CONTINUATION_ROWS
            if end > bottom or end >= height:
                return None
            ys = np.arange(lo, end + 1)
            xs = np.asarray(lane_xs(lane, ys), dtype=float)
            if xs.shape != ys.shape or not np.isfinite(xs).all() or np.any((xs < 0) | (xs >= width)):
                return None
            # Every measured row must stay in the SAME source stroke. No
            # nearest-paint lookup can bridge a gap or hop to a parallel line.
            ids = labels[ys, np.rint(xs).astype(int)]
            if not len(ids) or ids[0] == 0 or np.any(ids != ids[0]):
                return None
            component = int(ids[0])
            x0, y0, stroke_width, stroke_height, area = map(int, stats[component])
            if (abs(y0 - top) > 1 or stroke_height < max(40., .08 * height)
                    or stroke_height < .2 * stroke_width):
                return None
            # A connected crossbar/fork can still pass point-on-paint tests.
            # Reject any multi-run row anywhere in this complete component.
            region = labels[y0:y0 + stroke_height, x0:x0 + stroke_width] == component
            edges = np.diff(np.pad(region.astype(np.int8), ((0, 0), (1, 1))), axis=1)
            if np.any((edges == 1).sum(axis=1) != 1):
                return None
            starts = x0 + (edges == 1).argmax(axis=1)
            stops = x0 + (edges == -1).argmax(axis=1) - 1
            widths = stops - starts + 1
            local = ys - y0
            if (np.any(local < 0) or np.any(local >= len(widths))
                    or np.any(xs < starts[local]) or np.any(xs > stops[local])):
                return None
            # The drawn points, not just a possibly stale fit, must have the
            # same native support. A fit-only check cannot excuse a wrong tip.
            for first, second in bad:
                for x, y, *_ in (first, second):
                    local_row = int(round(y)) - y0
                    if (local_row < 0 or local_row >= len(widths)
                            or not starts[local_row] <= x <= stops[local_row]
                            or abs(x - .5 * (starts[local_row] + stops[local_row]))
                               > max(2., .15 * widths[local_row])
                            or abs(x - float(lane_xs(lane, y))) > 2.):
                        return None
            centres = .5 * (starts[local] + stops[local])
            if np.any(np.abs(xs - centres) > np.maximum(2., .15 * widths[local])):
                return None
            tip_width = int(np.max(widths[lo - y0:tip_end - y0 + 1]))
            continuation_width = float(np.median(widths[tip_end + 1 - y0:end + 1 - y0]))
            # An attached T/stop bar at the endpoint is a single connected run
            # too: absolute and relative width bounds must both pass.
            if tip_width > max(4., 32. * width / 1920.) or tip_width > 4. * continuation_width:
                return None
            continuation_xs = xs[ys > tip_end]
            if np.any(np.abs(np.diff(continuation_xs)) > 6.):
                return None
            verified.append({"slot": slot, "component": component,
                "component_bbox": [x0, y0, stroke_width, stroke_height], "component_area": area,
                "bad_pairs": [{"first": list(a[:2]), "second": list(b[:2])} for a, b in bad],
                "tip_y": [lo, tip_end], "maximum_tip_width": tip_width,
                "median_continuation_width": continuation_width,
                "continuation_y": [tip_end + 1, end],
                "all_rows_source_supported": True, "whole_component_single_run": True})
        if not verified:
            return None
        return {"eligible": True, "version": VERSION,
                "verified_slots": [item["slot"] for item in verified], "evidence": verified,
                "geometry_changed": False, "requires_fresh_human_audit": True}
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, OverflowError):
        return None
