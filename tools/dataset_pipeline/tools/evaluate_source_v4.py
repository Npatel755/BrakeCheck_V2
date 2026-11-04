"""Independently compare source-only V4 drafts with immutable audit decisions.

Only report files are written. SQLite is opened read-only. Previously approved
samples are regression controls, not a new held-out test set or new approvals.
Human geometry is used only after conversion to score the resulting draft.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

import numpy as np

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_ROOT))

from bcdata.source_boundary_maps import load_boundary_maps
from bcdata.teachers.source_labels import load_classes
from tools.evaluate_source_repair import score
from tools.evaluate_source_v3 import observed_extents, points, slotted, support


METRIC = "Same-slot 20px raster IoU >= 0.5 against actual immutable human audit reviews"
LIMITATION = ("Source-converter development checks on previously inspected samples; "
              "not held-out accuracy, human approval, or an official UFLDv2 benchmark")
CURRENT_FAILURES = {
    "comma10k-cf86b1a810395c5c": "unassigned_central_fragment_and_ego_slot",
    "comma10k-bdbb9db5235de33d": "split_boundary_and_truncated_right_continuations",
    "comma10k-6de59376ad716835": "duplicated_left_boundary",
    "comma10k-c41f78165b9cd168": "truncated_ego_right_continuation",
    "comma10k-dc0c4d1c982f27c4": "curve_extent_and_marking_uncertainty",
}
HELD_CASES = {
    "comma10k-ec8bb95a6bccfaa2": "ego_right_split_across_slots_3_4",
    "comma10k-3872d7f22e4ebe94": "ego_left_split_across_slots_1_2",
    "comma10k-6f85bd87ef9f3498": "slot_2_3_crossing",
    "comma10k-a00b5e39a536c7b6": "left_fragments_split_or_duplicated",
    "comma10k-e97652a1082b73f6": "farther_ego_left_dashes_uncovered",
    "comma10k-366a0b8492b83b62": "short_unslotted_outer_right_fragment",
    "comma10k-6fd08fc7c32913a8": "nonoverlapping_curved_center_fragment_cannot_establish_new_lane",
    "comma10k-c8dd8748b22265a9": "nonoverlapping_intersection_fragment_cannot_establish_new_lane",
}
NO_OVERLAP_CONTROLS = {"comma10k-6fd08fc7c32913a8", "comma10k-c8dd8748b22265a9"}
WARNING_MECHANISMS = {
    "unassigned_central_fragment_and_ego_slot": ("fragment", "slot"),
    "split_boundary_and_truncated_right_continuations": ("boundary", "continuation", "join", "association", "slot"),
    "duplicated_left_boundary": ("duplicate", "association", "curve", "slot"),
    "truncated_ego_right_continuation": ("continuation", "extent", "fragment", "join"),
    "curve_extent_and_marking_uncertainty": ("curve", "cross", "extent", "boundary"),
}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def lane_rows(lane, height):
    """Interpolate stored polyline centers, without extrapolating either end."""
    raw = np.asarray([p[:2] for p in lane.get("points", [])], dtype=float).reshape(-1, 2)
    raw = raw[np.isfinite(raw).all(axis=1)]
    if not len(raw):
        return np.full(height, np.nan)
    order = np.argsort(raw[:, 1], kind="stable")
    raw = raw[order]
    ys, first = np.unique(raw[:, 1], return_index=True)
    xs = raw[first, 0]
    result = np.full(height, np.nan)
    rows = np.arange(max(0, int(np.ceil(ys[0]))), min(height, int(np.floor(ys[-1])) + 1))
    if len(rows):
        result[rows] = np.interp(rows, ys, xs)
    return result


def paint_rows(lane, paint, radius=12):
    """Rows where a stored trace runs near actual paint in the source mask.

    Twelve native pixels allow a double-line center between two painted stripes.
    This is a source-mask agreement diagnostic, never proof of paint visibility.
    """
    h, w = paint.shape
    xs = lane_rows(lane, h)
    supported = np.zeros(h, dtype=bool)
    for y in np.flatnonzero(np.isfinite(xs)):
        x = int(round(xs[y]))
        if 0 <= x < w:
            supported[y] = bool(paint[y, max(0, x-radius):min(w, x+radius+1)].max() >= .5)
    return xs, supported


def paint_supported_identity(candidate, human, paint):
    """Match each human slot only over rows near source-labeled paint.

    Unsupported human extensions are explicitly measured but never required as
    converter targets. Candidate gap interpolation may cover supported rows;
    extension beyond candidate endpoints cannot count as a recovered row.
    """
    h, _ = paint.shape
    output = {}
    candidates = slotted(candidate)
    for slot, lane in slotted(human).items():
        hx, selected = paint_rows(lane, paint)
        cx = lane_rows(candidates[slot], h) if slot in candidates else np.full(h, np.nan)
        errors = np.abs(hx - cx)
        matched = selected & np.isfinite(cx) & (errors <= 12)
        supported_count = int(selected.sum())
        selected_ys = np.flatnonzero(selected)
        common_errors = errors[selected & np.isfinite(cx)]
        output[str(slot)] = {
            "human_trace_rows": int(np.isfinite(hx).sum()),
            "source_supported_human_rows": supported_count,
            "source_supported_human_y": [int(selected_ys[0]), int(selected_ys[-1])] if len(selected_ys) else None,
            "unsupported_human_rows": int((np.isfinite(hx) & ~selected).sum()),
            "matched_rows_within_12px": int(matched.sum()),
            "source_supported_row_recall": float(matched.sum() / supported_count) if supported_count else None,
            "mean_common_row_error_px": float(common_errors.mean()) if len(common_errors) else None,
            "sufficient_source_rows": supported_count >= 4,
        }
    return output


def crossing_checks(geometry):
    """Report adjacent slot crossings at common rows; never extrapolate lanes."""
    h = geometry["height"]
    lanes = slotted(geometry)
    output = []
    for left, right in zip(sorted(lanes), sorted(lanes)[1:]):
        lx, rx = lane_rows(lanes[left], h), lane_rows(lanes[right], h)
        valid = np.isfinite(lx) & np.isfinite(rx)
        crossed = valid & (lx > rx + 3)
        if crossed.any():
            ys = np.flatnonzero(crossed)
            output.append({"slots": [left, right], "crossing_rows": int(crossed.sum()),
                           "y": [int(ys[0]), int(ys[-1])]})
    return output


def source_boundary_retention(original, candidate, paint):
    """Match physical observed source centers across possibly reassigned slots.

    This measures retaining a previously observed boundary while changing its
    slot. It does not assume that a human's hand-drawn coordinate is exact.
    Only original observed points actually near source paint count.
    """
    h, w = paint.shape
    result = {}
    for old_slot, lane in slotted(original).items():
        observed = points(lane)
        selected = []
        for x, y in observed:
            xi, yi = round(float(x)), round(float(y))
            if 0 <= yi < h and 0 <= xi < w and paint[yi, max(0, xi-4):min(w, xi+5)].max() >= .5:
                selected.append([float(x), yi])
        selected = np.asarray(selected, float).reshape(-1, 2)
        destinations = {}
        for new_slot, replacement in slotted(candidate).items():
            xs = lane_rows(replacement, h)
            errors = np.abs(xs[selected[:, 1].astype(int)] - selected[:, 0]) if len(selected) else np.array([])
            matches = np.isfinite(errors) & (errors <= 4)
            destinations[str(new_slot)] = {"matched_observed_points_within_4px": int(matches.sum()),
                                            "observed_points": len(selected),
                                            "retained_fraction": float(matches.sum()/len(selected)) if len(selected) else None}
        result[str(old_slot)] = destinations
    return result


def source_fragment_overlap(original, paint):
    """Independent evidence that a short fragment is a distinct parallel lane.

    Two traces must both meet source-painted pixels at the same native rows.
    Extrapolated x_ref ordering cannot establish a distinct physical boundary.
    """
    results = []
    inner = {slot: lane for slot, lane in slotted(original).items() if slot in (2, 3)}
    for index, fragment in enumerate(original.get("lanes", [])):
        if fragment.get("slot") in (1, 2, 3, 4):
            continue
        fx, fs = paint_rows(fragment, paint, radius=4)
        for slot, lane in inner.items():
            lx, ls = paint_rows(lane, paint, radius=4)
            common = fs & ls
            ys = np.flatnonzero(common)
            distance = (fx - lx) if slot == 2 else (lx - fx)
            clear = distance > max(16., .018 * original["width"])
            usable = common & clear
            usable_ys = np.flatnonzero(usable)
            span = int(usable_ys[-1]-usable_ys[0]) if len(usable_ys) else 0
            results.append({"fragment_index": index, "fragment_id": fragment.get("id"),
                            "existing_inner_slot": slot, "source_painted_overlap_rows": int(common.sum()),
                            "overlap_y": [int(ys[0]), int(ys[-1])] if len(ys) else None,
                            "distinct_inward_paint_rows": int(usable.sum()), "distinct_inward_span_px": span,
                            "distinct_inward_boundary_supported": bool(usable.sum() >= 5 and span >= 6)})
    return results


def unsupported_inward_promotions(record):
    """Check every held draft, including cases absent from the named controls."""
    output = []
    for inner, outer in ((2, 1), (3, 4)):
        if inner not in record["old_slots"] or outer in record["old_slots"] or outer not in record["v4_slots"]:
            continue
        retained = record["source_boundary_retention"].get(str(inner), {})
        moved = retained.get(str(outer), {}).get("retained_fraction") or 0.
        stayed = retained.get(str(inner), {}).get("retained_fraction") or 0.
        if moved < .90 or moved <= stayed:
            continue
        evidence = [e for e in record["source_fragment_overlap"] if e["existing_inner_slot"] == inner]
        if not any(e["distinct_inward_boundary_supported"] for e in evidence):
            output.append({"image_id": record["image_id"], "old_inner_slot": inner, "moved_to_outer_slot": outer,
                           "cause": "inward_slot_promotion_without_simultaneously_observed_distinct_source_paint"})
    return output


def no_overlap_control_check(record):
    """No assertion of human truth: insufficient evidence must stay ambiguous."""
    evidence = [e for e in record["source_fragment_overlap"] if e["existing_inner_slot"] == 2]
    issues = []
    if not evidence or any(e["source_painted_overlap_rows"] for e in evidence):
        issues.append("control_no_longer_has_nonoverlapping_source_fragment")
    if record["old_slots"] != record["v4_slots"]:
        issues.append("nonoverlapping_fragment_displaced_existing_slot_identity")
    retained = record["source_boundary_retention"].get("2", {}).get("2", {})
    if (retained.get("retained_fraction") or 0.) < .95:
        issues.append("existing_ego_left_source_boundary_not_retained")
    warning = "ambiguous_inward_source_continuation"
    if warning not in record["warnings"]:
        issues.append("nonoverlapping_fragment_has_no_explicit_ambiguity_warning")
    return {"image_id": record["image_id"], "cause": HELD_CASES[record["image_id"]],
            "status": "flagged" if not issues else "unresolved", "criteria_failures": issues,
            "flagged": not issues, "warnings": [warning] if warning in record["warnings"] else [],
            "human_truth_asserted": False, "source_fragment_overlap": evidence}


def geometry_identity(geometry):
    """Ignore provenance/version fields when deciding whether paint changed."""
    return canonical_sha256(geometry.get("lanes", []))


def duplicate_slots(geometry):
    counts = Counter(lane.get("slot") for lane in geometry.get("lanes", []) if lane.get("slot") in (1, 2, 3, 4))
    return sorted(slot for slot, count in counts.items() if count > 1)


def _aggregate(records, version):
    rows = [r for r in records if r.get("human") is not None]
    tp, fp, fn = (sum(r[version][key] for r in rows) for key in ("tp", "fp", "fn"))
    observed = sum(r["source_paint_support"][version]["observed"] for r in records)
    supported = sum(r["source_paint_support"][version]["supported"] for r in records)
    return {"images": len(rows), "tp": tp, "fp": fp, "fn": fn,
            "f1": 2 * tp / max(1, 2 * tp + fp + fn),
            "count_matches": sum(r[version]["count_match"] for r in rows),
            "observed_point_source_support": supported / observed if observed else None}


def known_failure_check(record):
    """Demand supported boundary identity/extent, or an explicit cause warning.

    cf86's human trace follows an unpainted seam below the distant dash. Require
    only observed central paint in slot 2 and old far-left paint in slot 1.
    Other cases must retain the human slot set, recover supported source rows,
    avoid crossings, and match the complete human lane raster where supported.
    These are development acceptance checks, not blanket cohort approvals.
    """
    image_id = record["image_id"]
    identity = record["paint_supported_identity"]["v4"]
    human_slots, new_slots = record["human_slots"], record["v4_slots"]
    issues = []
    if record.get("candidate_duplicate_slots"):
        issues.append("candidate_assigns_multiple_boundaries_to_one_slot")
    if new_slots != human_slots:
        issues.append("candidate_slot_set_differs_from_corrected_boundary_identities")
    if record["crossings"]["v4"]:
        issues.append("candidate_has_adjacent_slot_crossing")
    for slot, detail in identity.items():
        if image_id == "comma10k-cf86b1a810395c5c" and slot == "1":
            # This old boundary is remapped 2->1; its source-painted centers
            # are the physical target, rather than an imprecise human stroke.
            continue
        if detail["sufficient_source_rows"] and detail["source_supported_row_recall"] < .80:
            issues.append("source_supported_human_rows_not_recovered_in_slot_" + slot)
    if image_id == "comma10k-cf86b1a810395c5c":
        central = identity.get("2", {})
        if not central.get("sufficient_source_rows"):
            issues.append("insufficient_central_paint_rows_to_verify_slot_2")
        candidate_slot2 = record["candidate_extents"].get("2")
        source_ys = central.get("source_supported_human_y")
        if candidate_slot2 and source_ys and candidate_slot2[1] > source_ys[1] + 12:
            issues.append("central_boundary_extends_beyond_verified_paint")
        reassigned = record.get("source_boundary_retention", {}).get("2", {}).get("1", {})
        if reassigned.get("observed_points", 0) < 2 or (reassigned.get("retained_fraction") or 0) < .95:
            issues.append("old_far_left_source_boundary_not_retained_in_slot_1")
    elif record["v4"]["tp"] != len(human_slots):
        issues.append("same_slot_full_human_raster_does_not_match_all_corrected_boundaries")
    warnings = record["warnings"]
    # Generic risk words do not count. These are declared converter failure
    # mechanism warnings; the deployment path must emit them as QC issues.
    specific = [w for w in warnings if any(token in w for token in WARNING_MECHANISMS[CURRENT_FAILURES[image_id]])]
    return {"image_id": image_id, "cause": CURRENT_FAILURES[image_id],
            "status": "passed" if not issues else "flagged" if specific else "unresolved", "criteria_failures": issues,
            "flagged": bool(specific) if issues else False, "warnings": specific,
            "unsupported_human_extent_is_not_a_target": image_id == "comma10k-cf86b1a810395c5c"}


def evaluate(data_root: Path, output: Path, include_held=False):
    from bcdata.source_converter_v4 import VERSION, repair_geometry_v4

    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Report directory must be new or empty; immutable evidence will not be overwritten")
    output.mkdir(parents=True, exist_ok=True)
    code = PIPELINE_ROOT / "bcdata" / "source_converter_v4.py"
    code_sha = sha256(code)
    dependencies = {str(path.resolve()): sha256(path) for path in (
        PIPELINE_ROOT / "bcdata" / "source_converter.py",
        PIPELINE_ROOT / "bcdata" / "source_converter_v3.py",
        PIPELINE_ROOT / "bcdata" / "geometry.py",
        PIPELINE_ROOT / "bcdata" / "source_boundary_maps.py",
        PIPELINE_ROOT / "bcdata" / "teachers" / "source_labels.py",
        PIPELINE_ROOT / "bcdata" / "sources" / "comma10k.py",
        PIPELINE_ROOT / "bcdata" / "quality.py",
        Path(__file__).with_name("evaluate_source_v3.py"),
        Path(__file__).with_name("evaluate_source_repair.py"),
    )}
    evaluator_sha = sha256(__file__)
    con = sqlite3.connect((data_root / "metadata.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("BEGIN")
    records, excluded, checked_formats, groups_by_image = [], [], set(), {}
    try:
        for group in con.execute("SELECT * FROM source_validation_groups ORDER BY group_key"):
            for image_id in json.loads(group["sample_ids"]):
                if image_id in groups_by_image:
                    raise ValueError("Audit sample belongs to multiple groups: " + image_id)
                groups_by_image[image_id] = dict(group)
        missing = sorted(set(CURRENT_FAILURES) - groups_by_image.keys())
        if missing:
            raise ValueError("Required current corrected samples are absent: " + str(missing))
        held_ids = set()
        if include_held:
            held_ids = {row["image_id"] for row in con.execute("""SELECT c.image_id FROM source_conversion_checks c
                JOIN source_validation_groups g USING(group_key) JOIN images i ON c.image_id=i.image_id
                WHERE c.state='awaiting_audit' AND g.state='blocked' AND i.status='curated' AND i.locked=0
                AND i.review_status NOT IN ('approved','corrected','rejected')
                AND NOT EXISTS(SELECT 1 FROM review_events e WHERE e.image_id=c.image_id)""")}
        identifiers = sorted(set(groups_by_image) | set(HELD_CASES) | held_ids)
        for index, image_id in enumerate(identifiers):
            image = con.execute("SELECT * FROM images WHERE image_id=?", (image_id,)).fetchone()
            if image is None:
                raise ValueError("Missing audit image: " + image_id)
            group = groups_by_image.get(image_id)
            accepted = image["review_status"] in ("approved", "corrected") and bool(image["locked"])
            if group and not accepted:
                excluded.append({"image_id": image_id, "decision": image["review_status"],
                                 "reason": "no_current_accepted_human_review"})
                continue
            check = con.execute("SELECT * FROM source_conversion_checks WHERE image_id=?", (image_id,)).fetchone()
            if check is None or (group and check["group_key"] != group["group_key"]):
                raise ValueError("Audit check/group binding changed: " + image_id)
            source = con.execute("SELECT * FROM annotations WHERE id=? AND kind='source_label'", (check["source_id"],)).fetchone()
            human_ann = con.execute("SELECT * FROM annotations WHERE image_id=? AND kind='human' ORDER BY created_at DESC,id DESC LIMIT 1", (image_id,)).fetchone() if accepted else None
            if source is None or (accepted and human_ann is None):
                raise ValueError("Missing bound source or human annotation: " + image_id)
            if json.loads(source["params"] or "{}").get("human") is not True:
                raise ValueError("Evaluation requires the real human source mask: " + image_id)
            fingerprints = {"check": dict(check), "source_annotation_id": source["id"],
                            "source_path": source["path"], "source_sha256": sha256(source["path"]),
                            "geometry_path": check["geometry_path"], "geometry_sha256": sha256(check["geometry_path"])}
            if fingerprints["source_sha256"] != check["source_sha256"] or fingerprints["geometry_sha256"] != check["geometry_sha256"]:
                raise ValueError("Immutable source/check fingerprint mismatch: " + image_id)
            original = json.loads(Path(check["geometry_path"]).read_text(encoding="utf-8"))
            maps = load_boundary_maps(source)
            paint = maps["lane"]
            if paint.shape != (original["height"], original["width"]):
                raise ValueError("Native source mask/geometry dimensions differ: " + image_id)
            fmt = json.loads(source["params"] or "{}").get("format")
            if fmt not in checked_formats:
                classes = load_classes(source)
                if not all(np.array_equal(maps[name] >= .5, classes == cls)
                           for name, cls in (("lane", 2), ("my_car", 5), ("movable", 4))):
                    raise ValueError("Boundary loader differs from shared source taxonomy")
                checked_formats.add(fmt)
            original_digest = canonical_sha256(original)
            mask_digest = {name: hashlib.sha256(value.tobytes()).hexdigest() for name, value in maps.items()}
            candidate = repair_geometry_v4(original, paint, my_car=maps["my_car"], movable=maps["movable"])
            if canonical_sha256(original) != original_digest or any(hashlib.sha256(value.tobytes()).hexdigest() != mask_digest[name] for name, value in maps.items()):
                raise ValueError("Converter mutated its geometry or source-mask inputs: " + image_id)
            if (candidate["height"], candidate["width"]) != paint.shape:
                raise ValueError("Converter changed native dimensions: " + image_id)
            candidate["image_id"] = image_id
            candidate_path = output / "geometry" / (image_id + ".json")
            candidate_path.parent.mkdir(exist_ok=True)
            candidate_path.write_text(json.dumps(candidate, indent=2, sort_keys=True), encoding="utf-8")
            fingerprints.update(candidate_path=str(candidate_path), candidate_sha256=sha256(candidate_path))
            human = None
            if human_ann:
                fingerprints.update(human_path=human_ann["path"], human_sha256=sha256(human_ann["path"]),
                                    human_annotation_id=human_ann["id"])
                human = json.loads(Path(human_ann["path"]).read_text(encoding="utf-8"))
                if (human["height"], human["width"]) != paint.shape:
                    raise ValueError("Human/source native dimensions differ: " + image_id)
            metadata = candidate.get("source_converter", {})
            record = {"image_id": image_id, "decision": image["review_status"], "split": image["split"],
                      "group_key": group["group_key"] if group else check["group_key"],
                      "stratum": group["stratum"] if group else None,
                      "audit_sample": bool(group), "held_case": HELD_CASES.get(image_id),
                      "currently_held": image_id in held_ids,
                      "geometry_changed": geometry_identity(original) != geometry_identity(candidate),
                      "old_slots": sorted(slotted(original)), "v4_slots": sorted(slotted(candidate)),
                      "candidate_duplicate_slots": duplicate_slots(candidate),
                      "human_slots": sorted(slotted(human)) if human else None,
                      "old": score(original, human) if human else None,
                      "v4": score(candidate, human) if human else None,
                      "human": {"annotation_id": human_ann["id"]} if human_ann else None,
                      "source_paint_support": {"old": support(original, paint), "v4": support(candidate, paint)},
                      "paint_supported_identity": {"old": paint_supported_identity(original, human, paint),
                                                   "v4": paint_supported_identity(candidate, human, paint)} if human else None,
                      "observed_extents": observed_extents(original, candidate),
                      "source_boundary_retention": source_boundary_retention(original, candidate, paint),
                      "source_fragment_overlap": source_fragment_overlap(original, paint),
                      "candidate_extents": {str(slot): [float(points(lane)[:, 1].min()), float(points(lane)[:, 1].max())]
                                            for slot, lane in slotted(candidate).items() if len(points(lane))},
                      "crossings": {"old": crossing_checks(original), "v4": crossing_checks(candidate)},
                      "warnings": list(metadata.get("warnings", [])), "source_converter": metadata,
                      "fingerprints": fingerprints}
            records.append(record)
            if (index+1) % 25 == 0:
                print(json.dumps({"processed": index+1, "total": len(identifiers)}), flush=True)
    finally:
        con.rollback()
        con.close()
    if sha256(code) != code_sha or sha256(__file__) != evaluator_sha or any(sha256(p) != digest for p, digest in dependencies.items()):
        raise ValueError("Converter/evaluator dependencies changed during evaluation; rerun stable code")
    for record in records:
        for kind in ("source", "geometry", "human"):
            fp = record["fingerprints"]
            if kind + "_path" in fp and sha256(fp[kind + "_path"]) != fp[kind + "_sha256"]:
                raise ValueError("Immutable annotation changed during evaluation: " + record["image_id"])
    regressions = [{"image_id": r["image_id"], "decision": r["decision"], "old": r["old"], "v4": r["v4"],
                    "lost_slots": sorted(set(r["old_slots"]) - set(r["v4_slots"]))}
                   for r in records if r["human"] is not None and
                   (r["candidate_duplicate_slots"] or r["v4"]["tp"] < r["old"]["tp"] or r["v4"]["fp"] > r["old"]["fp"] or r["v4"]["fn"] > r["old"]["fn"])]
    approved_regressions = [r for r in regressions if r["decision"] == "approved"]
    known = [known_failure_check(r) for r in records if r["image_id"] in CURRENT_FAILURES]
    control_checks = [no_overlap_control_check(r) for r in records if r["image_id"] in NO_OVERLAP_CONTROLS]
    known.extend(control_checks)
    unresolved = [r for r in known if r["status"] != "passed"]
    if len(known) != len(CURRENT_FAILURES) + len(NO_OVERLAP_CONTROLS):
        raise ValueError("Not every required corrected sample was evaluated")
    subsets = {"all_human": [r for r in records if r["human"] is not None],
               "approved": [r for r in records if r["decision"] == "approved"],
               "corrected": [r for r in records if r["decision"] == "corrected"],
               "current_five_corrected": [r for r in records if r["image_id"] in CURRENT_FAILURES]}
    held = [r for r in records if r["currently_held"]]
    unsupported_promotions = [item for r in records for item in unsupported_inward_promotions(r)]
    held_summary = {"included": include_held, "images": len(held),
                    "geometry_changed": sum(r["geometry_changed"] for r in held),
                    "with_mechanism_warnings": sum(bool(r["warnings"]) for r in held),
                    "warnings": dict(Counter(w for r in held for w in r["warnings"])),
                    "risk_families": dict(Counter(r["source_converter"].get("risk_family", "unknown") for r in held)),
                    "old_crossings": sum(bool(r["crossings"]["old"]) for r in held),
                    "v4_crossings": sum(bool(r["crossings"]["v4"]) for r in held),
                    "crossings_without_converter_warning": [r["image_id"] for r in held if r["crossings"]["v4"] and not r["warnings"]],
                    "limitation": "Automatic source-only diagnostics; every fresh unflagged cohort still requires its actual human audit."}
    report = {"metric": METRIC, "limitation": LIMITATION, "created_at": datetime.now(timezone.utc).isoformat(),
              "application_policy": "Run source-only V4 on held source conversions; no human or model geometry enters conversion. Warnings must remain flagged for human inspection.",
              "paint_identity_metric": "Human trace rows within 12 native pixels of source paint; candidate same-slot center must be within 12px at those rows. No endpoint extrapolation.",
              "converter_version": VERSION, "converter_code_path": str(code.resolve()), "converter_code_sha256": code_sha,
              "dependency_sha256": dependencies, "evaluator_code_path": str(Path(__file__).resolve()),
              "evaluator_code_sha256": evaluator_sha,
              "input_manifest_sha256": canonical_sha256([{ "image_id": r["image_id"], "decision": r["decision"], **r["fingerprints"]} for r in records]),
              "audit_samples_registered": len(groups_by_image), "audit_samples_evaluated": sum(r["audit_sample"] for r in records),
              "excluded": excluded, "decisions": dict(Counter(r["decision"] for r in records)),
              "safety_checks": {"database_opened_read_only": True, "database_writes": False,
                                "original_annotations_unchanged": True, "source_fingerprints_verified": True,
                                "converter_and_dependencies_stable": True, "boundary_loader_verified_against_shared_taxonomy": True,
                                "approved_sample_regressions": len(approved_regressions),
                                "candidate_slot_uniqueness_verified": not any(r["candidate_duplicate_slots"] for r in records),
                                "no_unsupported_inward_slot_promotions": not unsupported_promotions,
                                "known_failures_resolved_or_flagged": not unsupported_promotions and all(r["status"] == "passed" or r["flagged"] for r in known)},
              "totals": {name: {v: _aggregate(rows, v) for v in ("old", "v4")} for name, rows in subsets.items()},
              "regressions": regressions, "known_failure_checks": known, "unresolved_known_failures": unresolved,
              "held_summary": held_summary,
              "held_control_checks": control_checks, "unsupported_inward_promotions": unsupported_promotions,
              "held_case_diagnostics": [r["image_id"] for r in records if r["held_case"]], "records": records}
    report_path = output / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"report_path": str(report_path), "report_sha256": sha256(report_path),
                      "totals": report["totals"], "safety_checks": report["safety_checks"],
                      "held_summary": held_summary,
                      "known_failure_checks": known, "regressions": regressions}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path(os.environ.get("BRAKECHECK_DATA_ROOT", PIPELINE_ROOT.parent.parent / "BrakeCheck_Dataset")))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--include-held", action="store_true", help="Also write source-only diagnostics for every currently held unreviewed image")
    args = parser.parse_args()
    data = args.data_root.resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    evaluate(data, args.output or data / "reports" / ("source_v4_evaluation-" + stamp), include_held=args.include_held)


if __name__ == "__main__":
    main()
