"""Freeze cleared real-source annotations into a converted-only training run.

The original registry, isolated imports and their decisions remain unchanged.
Only TRAIN additions are rendered; the cleared base's validation and reserved
test identities remain exactly as frozen by the reviewed exporter.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import shutil
import time
import uuid

import cv2
import numpy as np

from .bootstrap import atomic_json
from .export_ufld import ROWS_LINES, cache_entry
from .quality import read_rgb, write_jpg, write_png
from .real_source_review import RealSourceReview, SOURCES
from .simulation_review import _inside
from .teachers.base import file_sha256
from .ufld_baseline import HEIGHT, WIDTH, load_frozen_export
from .ufld_experiment import _base_state, _digest, _relative, _sample_paths, _session, _verify_sample

FORMAT = "ufld_converted_merge_v1"
REPAIR_SOURCE = "college_held_repairs"
# Repair audits enter only through an explicit source selection. Ordinary
# merges keep their existing source scope and never promote the audit UI.
MERGE_SOURCES = {**SOURCES, REPAIR_SOURCE: ("Audited College repairs", "CC0-1.0", "college-")}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _hash(record):
    return record.get("source_sha256") or record.get("sha256")


def _copy_verified(source, destination, digest=None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if digest and file_sha256(destination) != digest:
        raise ValueError("Source changed while copying: " + str(source))


def _source_state(data_root, sources):
    result = {}
    for source in sources:
        if source == REPAIR_SOURCE:
            from .college_repair_training import CollegeRepairTraining
            store = CollegeRepairTraining(data_root)
        else:
            store = RealSourceReview(data_root, source)
        metadata, original, _, manifest_sha, audits_sha = store.snapshot(fresh=True)
        if not original:
            result[source] = {"store": store, "records": {}, "eligible": [], "pin": None}
            continue
        group_clearance_path = _inside(store.root, "group_warning_clearance.json")
        group_clearance_sha = file_sha256(group_clearance_path) if group_clearance_path.is_file() else None
        eligibility = store.eligible_records()
        eligible = eligibility["records"]
        pin = {"manifest_sha256": manifest_sha, "audits_sha256": audits_sha,
               "import_sha256": file_sha256(store.root / "import.json"),
               "clearance_sha256": _digest(eligible)}
        if source == REPAIR_SOURCE:
            middle = store.root / "middle_boundary_annotations.json"
            pin.update(collection=REPAIR_SOURCE,
                       middle_boundary_index_sha256=file_sha256(middle) if middle.is_file() else None)
        if any(row["clearance"]["type"] == "sampled_unchanged_warning_cohort" for row in eligible):
            if group_clearance_sha != file_sha256(group_clearance_path):
                raise ValueError("Group warning clearance changed while reading its eligible photos")
            pin["group_warning_clearance_sha256"] = group_clearance_sha
        result[source] = {"store": store, "records": original, "eligible": eligible,
                          "metadata": metadata, "pin": pin, "rejected": eligibility.get("rejected", [])}
    return result


def _inventory(base_inventory, source_states):
    inventory = {key: dict(value) for key, value in base_inventory.items()}
    for state in source_states.values():
        for original in state["records"].values():
            split = original.get("intended_split") if original["split"] == "hold" else original["split"]
            if split not in {"train", "val", "test"}:
                continue
            row = {**copy.deepcopy(original), "split": split, "original_split": original["split"]}
            previous = inventory.get(row["image_id"])
            if previous and (previous["split"] != split or _session(previous) != _session(row)
                             or _hash(previous) != _hash(row)):
                raise ValueError("Isolated source disagrees with authoritative split inventory: " + row["image_id"])
            inventory.setdefault(row["image_id"], row)
    sessions = {}
    for row in inventory.values():
        session = _session(row)
        if session in sessions and sessions[session] != row["split"]:
            raise ValueError("Recording session crosses training/validation/test splits: " + str(session))
        sessions[session] = row["split"]
    return inventory


def _state(data_root, base_run, split_inventory, sources):
    base_run = Path(base_run).resolve()
    if split_inventory is None:
        path = base_run / "dataset/split_inventory.json"
        if not path.is_file():
            raise ValueError("The cleared base needs a full assigned split_inventory.json, including reserved test sessions")
        split_inventory = _read(path)
    elif isinstance(split_inventory, (str, Path)):
        split_inventory = _read(split_inventory)
    base, records, marker, base_inventory, _, reserved = _base_state(base_run, split_inventory)
    sources = tuple(SOURCES) if sources is None else tuple(sources)
    if len(set(sources)) != len(sources) or any(source not in MERGE_SOURCES for source in sources):
        raise ValueError("Choose unique supported real lane sources")
    source_states = _source_state(Path(data_root).resolve(), sources)
    inventory = _inventory(base_inventory, source_states)
    protected = [row for row in inventory.values() if row["split"] in {"val", "test"}]
    protected_ids = {row["image_id"] for row in protected} | set(reserved)
    protected_sessions = {_session(row) for row in protected}
    protected_hashes = {_hash(row) for row in protected} - {None, ""}
    base_by_id = {row["image_id"]: row for row in records}
    seen_hashes = {_hash(row) for row in records} - {None, ""}
    selected, rejected = [], []
    for source, state in source_states.items():
        for row in state["eligible"]:
            image_id = row["image_id"]
            if row["split"] != "train" or row.get("synthetic") is not False or row.get("pool") != "commercial":
                raise ValueError("Only cleared real commercial TRAIN additions are permitted")
            if image_id in protected_ids or _session(row) in protected_sessions or _hash(row) in protected_hashes:
                raise ValueError("Cleared source addition overlaps validation/test identity, session or content: " + image_id)
            previous = base_by_id.get(image_id)
            if previous:
                proof = previous.get("isolated_source_provenance", {})
                if (_hash(previous) != _hash(row) or proof.get("record_fingerprint") != row["fingerprint"]
                        or source == REPAIR_SOURCE and previous.get("geometry_sha256") != row["geometry_sha256"]):
                    raise ValueError("An already converted source annotation changed: " + image_id)
                rejected.append({"image_id": image_id, "source": source, "reason": "already_in_converted_base"})
                continue
            if _hash(row) in seen_hashes:
                rejected.append({"image_id": image_id, "source": source, "reason": "duplicate_source_bytes"})
                continue
            raw_geometry = _inside(state["store"].root, row["geometry"]).read_bytes()
            if hashlib.sha256(raw_geometry).hexdigest() != row["geometry_sha256"]:
                raise ValueError("Cleared source geometry changed during selection: " + image_id)
            if not _has_renderable_span(json.loads(raw_geometry)):
                rejected.append({"image_id": image_id, "source": source, "reason": "no_trainable_output_rows"})
                continue
            seen_hashes.add(_hash(row))
            selected.append((source, row))
    report = {"format": FORMAT, "collection_id": "converted_only", "base_run": str(base_run),
              "base_train_images": sum(row["split"] == "train" for row in records),
              "base_val_images": sum(row["split"] == "val" for row in records),
              "candidate_train_images": len(selected),
              "candidate_counts_by_source": dict(Counter(source for source, _ in selected)),
              "rejected": rejected, "source_snapshots": {source: state["pin"] for source, state in source_states.items()},
              "training_geometry_holds": {source: state.get("rejected", []) for source, state in source_states.items()
                                          if state.get("rejected")},
              "test_images_used": 0, "source_val_images_used": 0, "auto_promote": False,
              "original_registry_unchanged": True, "original_imports_unchanged": True}
    return base, records, marker, reserved, inventory, source_states, selected, report


def plan_converted_merge(data_root, base_run, *, split_inventory=None, sources=None):
    """Validate clearance and split provenance without writing any artifact."""
    return _state(data_root, base_run, split_inventory, sources)[-1]


def _sections(lane):
    """Use explicit observed sections and visibility flags, never a stale fit."""
    sections = lane.get("segments")
    if sections is None:
        sections = [lane["points"]]
    if not isinstance(sections, list):
        raise ValueError("Visible lane sections must be explicit point lists")
    result = []
    for section in sections:
        points = section if isinstance(section, list) else section.get("points")
        if not isinstance(points, list):
            raise ValueError("Invalid visible lane section")
        visible = []
        for point in points:
            if len(point) > 2 and point[2] is False:
                if len(visible) >= 2:
                    result.append(visible)
                visible = []
            else:
                visible.append(point[:2])
        if len(visible) >= 2:
            result.append(visible)
    return result


def _render(rgb, geometry, destination, image, *, human_approved_empty=False):
    height, width = rgb.shape[:2]
    RealSourceReview._validate_lanes(geometry["lanes"], width, height, source_metadata=True)
    canvas = cv2.resize(rgb, (WIDTH, HEIGHT), interpolation=cv2.INTER_AREA)
    mask = np.zeros((HEIGHT, WIDTH), np.uint8)
    rows = [[], [], [], []]
    normalized = []
    for lane in geometry["lanes"]:
        if lane.get("physical", True) is not True or lane.get("kind") in {"road_edge", "curb", "pavement_edge"}:
            raise ValueError("Converted lane slots accept visible physical paint only")
        sampled, spans, sections = {}, [], []
        for section in _sections(lane):
            points = np.asarray(section, np.float64)
            differences = np.diff(points[:, 1])
            if not (np.all(differences > 0) or np.all(differences < 0)):
                raise ValueError("A visible lane section must be single-valued in image rows")
            points = points[np.argsort(points[:, 1])]
            first, last = points[0, 1], points[-1, 1]
            if any(max(first, a) < min(last, b) for a, b in spans):
                raise ValueError("Visible sections of one boundary overlap in image rows")
            spans.append((first, last))
            for y in ROWS_LINES:
                source_y = y * height / HEIGHT
                if first <= source_y <= last:
                    x = round(float(np.interp(source_y, points[:, 1], points[:, 0]) * WIDTH / width), 3)
                    if y in sampled and sampled[y] != x:
                        raise ValueError("A boundary has multiple positions on the same image row")
                    sampled[y] = x
            canvas_points = points * np.asarray([WIDTH / width, HEIGHT / height])
            sections.append(canvas_points)
        lane_rows = [(sampled[y], y) for y in ROWS_LINES if y in sampled]
        if len(lane_rows) < 2:
            continue
        rows[lane["slot"] - 1] = lane_rows
        for points in sections:
            cv2.polylines(mask, [np.round(points).astype(np.int32)], False, lane["slot"], thickness=16)
        normalized.append({"slot": lane["slot"], "sections": [points.tolist() for points in sections]})
    if geometry["lanes"] and not any(rows):
        raise ValueError("Cleared visible geometry has no trainable observed row span")
    if not geometry["lanes"] and not (human_approved_empty or geometry.get("no_visible_lane_paint") is True
                                      or geometry.get("tags", {}).get("no_lanes") is True):
        raise ValueError("Empty geometry needs explicit verified no-visible-paint evidence")
    write_jpg(destination / image, canvas, 95)
    segmentation = "laneseg_label_w16/" + image.replace(".jpg", ".png")
    write_png(destination / segmentation, mask)
    (destination / image).with_suffix(".lines.txt").write_text("".join(
        " ".join(f"{x:.3f} {y}" for x, y in points) + " \n" for points in rows if points), encoding="utf-8")
    return cache_entry(rows), sum(bool(points) for points in rows), _digest(normalized)


def _has_renderable_span(geometry):
    """A positive needs two actual output anchors; never extend short labels."""
    if not geometry["lanes"]:
        return True  # Explicit human-negative evidence is checked by _render.
    anchors = [y * geometry["height"] / HEIGHT for y in ROWS_LINES]
    for lane in geometry["lanes"]:
        rows = set()
        for section in _sections(lane):
            first, last = min(point[1] for point in section), max(point[1] for point in section)
            rows.update(index for index, y in enumerate(anchors) if first <= y <= last)
        if len(rows) >= 2:
            return True
    return False


def _render_selected(target, source, row, state):
    """Render one cleared photo with its complete immutable approval evidence."""
    store = state["store"]
    original = state["records"][row["image_id"]]
    source_geometry = store._verified(original, geometry=True)
    correction = store._active_correction(original, row["source_manifest_sha256"], row["source_audits_sha256"])
    geometry = (store.training_geometry(original, row) if source == REPAIR_SOURCE else
                store._correction_geometry(correction, source_geometry) if correction else source_geometry)
    if store.effective_fingerprint(original, row["source_manifest_sha256"], row["source_audits_sha256"]) != row["fingerprint"]:
        raise ValueError("Source clearance changed while building: " + row["image_id"])
    image = f"images/train/real/{source}/{row['image_id']}.jpg"
    rgb = read_rgb(_inside(store.root, row["image"]))
    if rgb is None or (rgb.shape[1], rgb.shape[0]) != (geometry["width"], geometry["height"]):
        raise ValueError("Source image dimensions changed")
    clearance = row["clearance"]
    decisions = ([clearance["decision"]] if clearance["type"] == "individual_human_approval"
                 else clearance["sample_decisions"])
    human_empty = bool(correction and not geometry["lanes"] and any(
        decision["image_id"] == row["image_id"] and decision["action"] == "approve"
        and decision["fingerprint"] == row["fingerprint"] for decision in decisions))
    entry, lane_count, render_digest = _render(rgb, geometry, target, image, human_approved_empty=human_empty)
    evidence = "source_evidence/" + source + "/" + row["image_id"]
    assets = {"image": (original["image"], original["source_sha256"], "/original" + Path(original["image"]).suffix),
              "source_label": (original["source_label"], original["source_label_sha256"], "/native" + Path(original["source_label"]).suffix),
              "original_geometry": (original["geometry"], original["geometry_sha256"], "/original_geometry.json"),
              "geometry": (row["geometry"], row["geometry_sha256"], "/effective_geometry.json")}
    if source == REPAIR_SOURCE:
        assets["original_geometry"] = (original["original_geometry"], original["original_geometry_sha256"], "/original_geometry.json")
        assets["candidate_geometry"] = (original["geometry"], original["geometry_sha256"], "/candidate_geometry.json")
        for number, asset in enumerate(original.get("evidence_files", [])):
            assets["proposal_evidence_" + str(number)] = (asset["path"], asset["sha256"],
                "/proposal_evidence/" + str(number) + "-" + Path(asset["path"]).name)
    evidence_paths = {}
    for key, (relative, digest, suffix) in assets.items():
        name = evidence + suffix
        _copy_verified(_inside(store.root, relative), target / name, digest)
        evidence_paths[key] = name
    atomic_json(target / (evidence + "/clearance.json"), {
        "original_manifest_record": original, "eligible_record": row,
        "source_snapshot": state["pin"]})
    human = clearance["type"] == "individual_human_approval" or bool(correction)
    record = {**copy.deepcopy(row), "image": image, "geometry": evidence_paths["geometry"],
              "original_geometry": evidence_paths["original_geometry"], "source_label": evidence_paths["source_label"],
              "license": MERGE_SOURCES[source][1], "label_origin": "human" if human else "source_validated",
              "decision": "corrected" if human else "source_validated", "lanes": lane_count,
              "reviewer": correction["reviewer"] if correction else "",
              "geometry_version": geometry.get("version", geometry.get("converter", "native")),
              "source_ref": original.get("source_member", original.get("native_member", source + ":" + row["image_id"])),
              "native_evidence": evidence_paths, "render_geometry_sha256": render_digest,
              "source_validation": json.dumps({"group": row["group_key"], "evidence": {
                  "human_approved_sample_ids": [decision["image_id"] for decision in decisions]}}, sort_keys=True),
              "isolated_source_provenance": {"record_fingerprint": row["fingerprint"],
                  "source_snapshot": state["pin"], "source_split": original["split"],
                  "intended_split": original.get("intended_split"), "source_qc_reasons": original["qc_reasons"],
                  "resolved_qc_reasons": row.get("resolved_qc_reasons", []), "clearance": clearance,
                  "manual_complete_annotation": geometry.get("human_complete_annotation") is True,
                  "empty_label_verification": "human_approved_empty_correction" if human_empty else None,
                  "evidence": evidence + "/clearance.json"}}
    if source == REPAIR_SOURCE:
        # An audited model proposal keeps its model origin, even when
        # the user approves its quality or requests dash completion.
        record.update(decision="corrected" if correction else "approved",
                      label_origin="human_audit_correction" if correction else
                          "human_audited_model_proposal" if row["candidate_origin"] == "model_proposal" else "audited_source_repair",
                      repair_collection=REPAIR_SOURCE,
                      training_eligible=True, candidate_only=False,
                      boundary_completion=geometry.get("boundary_completion"))
        record["isolated_source_provenance"].update(
            candidate_origin=row["candidate_origin"], candidate_geometry_sha256=original["geometry_sha256"],
            source_qc_reasons=original.get("original_qc_reasons", original["qc_reasons"]),
            candidate_qc_reasons=original["qc_reasons"],
            saved_annotation=row.get("saved_annotation"),
            inferred_sections_are_observed_paint=False)
    _verify_sample(target, record, entry)
    return record, entry


def _lists(target, records, cache):
    folder = target / "list"
    folder.mkdir(exist_ok=True)
    for split in ("train", "val"):
        lines = []
        for record in records:
            if record["split"] != split:
                continue
            image, _, segmentation = _sample_paths(record)
            flags = [int(sum(point[0] >= 0 for point in lane) >= 2) for lane in cache[record["image"]]]
            lines.append(f"/{image.as_posix()} /{segmentation.as_posix()} " + " ".join(map(str, flags)))
        (folder / (split + "_gt.txt")).write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    for name in ("test.txt", "test_gt.txt", "train_gt_synthetic.txt"):
        (folder / name).write_text("", encoding="utf-8")


def build_converted_merge(data_root, base_run, output_run, *, split_inventory=None, sources=None, progress=None):
    """Publish a new immutable converted-only snapshot after verifying all inputs."""
    output_run, base_run = Path(output_run).resolve(), Path(base_run).resolve()
    if output_run.exists() or output_run == base_run or output_run.is_relative_to(base_run):
        raise ValueError("Converted output must be a new directory outside the cleared base")
    base, base_records, marker, reserved, inventory, states, selected, report = _state(
        data_root, base_run, split_inventory, sources)
    stage = output_run.with_name(output_run.name + ".preparing-" + uuid.uuid4().hex)
    target = stage / "dataset"
    target.mkdir(parents=True)
    started = time.time()

    def update(phase, processed=0):
        value = {"phase": phase, "processed": processed, "total": len(selected),
                 "started_at": started, "updated_at": time.time(), "output_run": str(output_run)}
        atomic_json(stage / "converted_merge_status.json", value)
        if progress:
            progress(value)

    try:
        update("copying_cleared_base")
        base_hashes = _read(base / "hashes.json")
        for relative, digest in base_hashes.items():
            path = _relative(relative)
            _copy_verified(base / path, target / path, digest)
        for name in ("manifest.json", "culane_anno_cache.json", "hashes.json", "reserved_test_ids.json", "export_marker.json"):
            _copy_verified(base / name, target / ("base_" + name))
        for name in ("NOTICE.txt", "licenses"):
            source = base / name
            if source.is_dir():
                shutil.copytree(source, target / name, dirs_exist_ok=True)
            elif source.is_file():
                _copy_verified(source, target / name)
        records, cache = copy.deepcopy(base_records), _read(base / "culane_anno_cache.json")
        for source, state in states.items():
            if not state["pin"]:
                continue
            for name in ("import.json", "manifest.jsonl", "audits.json", "group_warning_clearance.json", "middle_boundary_annotations.json", "NOTICE.txt", "official_metadata.json", "pinned_inventory.json", "04-115_Data_Specification.pdf"):
                path = _inside(state["store"].root, name)
                digest = state["pin"].get("group_warning_clearance_sha256") if name == "group_warning_clearance.json" else None
                if path.is_file() or digest:
                    _copy_verified(path, target / "source_imports" / source / name, digest)
            if source == REPAIR_SOURCE:
                for name in ("import.json", "manifest.jsonl", "audits.json"):
                    key = {"import.json": "import_sha256", "manifest.jsonl": "manifest_sha256", "audits.json": "audits_sha256"}[name]
                    _copy_verified(state["store"].original.root / name,
                                   target / "source_imports" / source / "original_source" / name,
                                   state["metadata"]["source_snapshot"][key])
        update("rendering_cleared_real_sources")
        additions = []
        def render_one(selection):
            source, row = selection
            return _render_selected(target, source, row, states[source])

        # Independent photos have disjoint output paths. Bound each batch so
        # failures stop promptly, and consume results in the selected order.
        with ThreadPoolExecutor(max_workers=4) as workers:
            for start in range(0, len(selected), 100):
                batch = selected[start:start + 100]
                for record, entry in workers.map(render_one, batch):
                    records.append(record)
                    cache[record["image"]] = entry
                    additions.append(record)
                update("rendering_cleared_real_sources", start + len(batch))
        update("verifying_source_clearance", len(selected))
        latest = _source_state(data_root, [source for source in states if source != REPAIR_SOURCE])
        if {source: state["pin"] for source, state in latest.items()} != {
                source: pin for source, pin in report["source_snapshots"].items() if source != REPAIR_SOURCE}:
            raise ValueError("Real-source annotations or decisions changed while building; rebuild the snapshot")
        if REPAIR_SOURCE in states:
            from fastapi import HTTPException
            try:
                report["repair_selection_verification"] = states[REPAIR_SOURCE]["store"].verify_training_selection(
                    [row for source, row in selected if source == REPAIR_SOURCE])
            except HTTPException as error:
                raise ValueError(str(error.detail)) from error
        atomic_json(target / "manifest.json", records)
        atomic_json(target / "culane_anno_cache.json", cache)
        atomic_json(target / "reserved_test_ids.json", reserved)
        atomic_json(target / "split_inventory.json", list(inventory.values()))
        report.update(output_run=str(output_run), created_at=time.time(), experimental=False,
                      extra_train_images=len(additions), train_images=sum(row["split"] == "train" for row in records),
                      val_images=sum(row["split"] == "val" for row in records),
                      extra_counts_by_source=dict(Counter(row["source"] for row in additions)),
                      individually_cleared_held_images=sum(
                          row["original_split"] == "hold" and row["clearance"]["type"] == "individual_human_approval"
                          for row in additions),
                      group_cleared_held_images=sum(
                          row["original_split"] == "hold" and row["clearance"]["type"] == "sampled_unchanged_warning_cohort"
                          for row in additions),
                      repair_audit_cleared_images=sum(row.get("repair_collection") == REPAIR_SOURCE for row in additions),
                      base_manifest_sha256=file_sha256(base / "manifest.json"),
                      base_validation_manifest_sha256=_digest([row for row in base_records if row["split"] == "val"]),
                      render_policy=("full_sensor_frame_explicit_sections_with_user_authorized_middle_dash_connections_no_extrapolation"
                                     if REPAIR_SOURCE in states else "full_sensor_frame_piecewise_linear_observed_sections_visibility_flags_no_extrapolation"))
        atomic_json(target / "converted_merge.json", report)
        atomic_json(target / "export_marker.json", {"format": FORMAT, "collection_id": "converted_only",
            "revision": marker["revision"], "validation": {"ok": True}, "errors": [],
            "experimental": False, "auto_promote": False, "manifest_sha256": file_sha256(target / "manifest.json")})
        _lists(target, records, cache)
        _copy_verified(Path(__file__), target / "converted_merge_builder.py")
        if REPAIR_SOURCE in states:
            _copy_verified(Path(__file__).with_name("college_repair_training.py"), target / "college_repair_training_builder.py")
        update("hashing_snapshot", len(selected))
        atomic_json(target / "hashes.json", {path.relative_to(target).as_posix(): file_sha256(path)
            for path in target.rglob("*") if path.is_file() and path != target / "hashes.json"})
        load_converted_merge(stage)
        update("complete", len(selected))
        stage.rename(output_run)
        return report
    except Exception as error:
        atomic_json(stage / "FAILED.json", {"error": str(error), "originals_unchanged": True})
        raise


def _verify_frozen_repair(target, record, proof, frozen):
    """Bind audited model/source repairs to the copied immutable evidence."""
    eligible, original, pin = proof["eligible_record"], proof["original_manifest_record"], proof["source_snapshot"]
    if not frozen:
        root = target / "source_imports" / REPAIR_SOURCE
        for name, key in (("manifest.jsonl", "manifest_sha256"), ("audits.json", "audits_sha256"), ("import.json", "import_sha256")):
            if file_sha256(root / name) != pin[key]:
                raise ValueError("Frozen repair source snapshot changed")
        frozen.update(records={row["image_id"]: row for row in
                               (json.loads(line) for line in (root / "manifest.jsonl").read_text().splitlines())},
                      audits={group["group_key"]: group for group in _read(root / "audits.json")["groups"]},
                      pin=pin)
    clearance = eligible["clearance"]
    if (pin != frozen["pin"] or original != frozen["records"].get(record["image_id"])
            or original.get("audit_mode", "ready") != "ready"
            or record.get("candidate_origin") != original["candidate_origin"]
            or record.get("source") != "college_station_lane_marking"
            or record.get("candidate_origin") not in {"source_repair", "model_proposal"}
            or record.get("training_eligible") is not True or record.get("candidate_only") is not False
            or clearance["type"] not in {"sampled_repair_cohort", "individual_human_approval"}
            or record.get("label_origin") not in {"human_audited_model_proposal", "human_audit_correction", "audited_source_repair"}):
        raise ValueError("Converted repair lost its audited origin or clearance")
    if clearance["type"] == "sampled_repair_cohort":
        group = frozen["audits"].get(original["group_key"])
        if (not group or group["count"] > 100 or clearance.get("edited_sample_ids") != []
                or [vote["image_id"] for vote in clearance["sample_decisions"]] != group["sample_ids"]):
            raise ValueError("Converted repair cohort lost its exact sample approvals")
    evidence = record["native_evidence"]
    for name, digest in (("candidate_geometry", original["geometry_sha256"]),
                         ("original_geometry", original["original_geometry_sha256"]),
                         ("geometry", eligible["geometry_sha256"])):
        if file_sha256(target / _relative(evidence[name])) != digest:
            raise ValueError("Converted repair geometry lost its source evidence")
    for number, asset in enumerate(original.get("evidence_files", [])):
        if file_sha256(target / _relative(evidence["proposal_evidence_" + str(number)])) != asset["sha256"]:
            raise ValueError("Converted repair model evidence changed")
    if eligible.get("saved_annotation") and eligible["saved_annotation"]["sha256"] != eligible["geometry_sha256"]:
        raise ValueError("Converted repair lost its saved middle-boundary binding")


def load_converted_merge(output_run):
    """Verify base preservation and train-only real clearance in the frozen merge."""
    target, records, marker = load_frozen_export(output_run)
    if marker.get("format") != FORMAT or marker.get("collection_id") != "converted_only":
        raise ValueError("Not a converted-only merged snapshot")
    base = _read(target / "base_manifest.json")
    if records[:len(base)] != base:
        raise ValueError("Converted merge changed the frozen cleared base")
    base_cache = _read(target / "base_culane_anno_cache.json")
    cache, hashes, base_hashes = _read(target / "culane_anno_cache.json"), _read(target / "hashes.json"), _read(target / "base_hashes.json")
    for record in base:
        if cache[record["image"]] != base_cache[record["image"]]:
            raise ValueError("Converted merge changed cleared base targets")
        if any(hashes.get(path.as_posix()) != base_hashes.get(path.as_posix()) for path in _sample_paths(record)):
            raise ValueError("Converted merge changed cleared base bytes")
    if _read(target / "reserved_test_ids.json") != _read(target / "base_reserved_test_ids.json"):
        raise ValueError("Converted merge changed reserved test identities")
    inventory = _inventory({row["image_id"]: row for row in _read(target / "split_inventory.json")}, {})
    protected = [row for row in inventory.values() if row["split"] in {"val", "test"}]
    protected_ids, protected_sessions = {row["image_id"] for row in protected}, {_session(row) for row in protected}
    protected_hashes = {_hash(row) for row in protected} - {None, ""}
    frozen_repairs = {}
    for record in records[len(base):]:
        if (record["split"] != "train" or record["synthetic"] is not False
                or record["source"] not in SOURCES or record["image_id"] in protected_ids
                or _session(record) in protected_sessions or _hash(record) in protected_hashes):
            raise ValueError("Converted source addition overlaps protected evaluation data")
        proof = _read(target / record["isolated_source_provenance"]["evidence"])
        eligible = proof["eligible_record"]
        if eligible["fingerprint"] != record["isolated_source_provenance"]["record_fingerprint"]:
            raise ValueError("Converted source annotation lost its clearance binding")
        clearance = eligible["clearance"]
        decisions = [clearance["decision"]] if clearance["type"] == "individual_human_approval" else clearance["sample_decisions"]
        if not decisions or any(row["action"] != "approve" for row in decisions):
            raise ValueError("Converted source lacks human clearance evidence")
        if clearance["type"] == "individual_human_approval" and (clearance["source_split"] != "hold"
                or clearance["intended_split"] != "train" or decisions[0]["fingerprint"] != eligible["fingerprint"]):
            raise ValueError("Held photo clearance does not authorize this exact TRAIN correction")
        if record.get("repair_collection") == REPAIR_SOURCE:
            _verify_frozen_repair(target, record, proof, frozen_repairs)
        _verify_sample(target, record, cache[record["image"]])
    return target, records, marker


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--base-run", type=Path, required=True)
    parser.add_argument("--output-run", type=Path)
    parser.add_argument("--split-inventory", type=Path)
    parser.add_argument("--source", action="append", choices=list(MERGE_SOURCES))
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args(argv)
    if args.plan:
        result = plan_converted_merge(args.data_root, args.base_run, split_inventory=args.split_inventory, sources=args.source)
    else:
        if args.output_run is None:
            parser.error("--output-run is required unless --plan is selected")
        result = build_converted_merge(args.data_root, args.base_run, args.output_run,
            split_inventory=args.split_inventory, sources=args.source,
            progress=lambda value: print(json.dumps(value), flush=True))
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
