"""Isolated model proposals for the incomplete US290 source subset.

Inference only. Artifacts are isolated, retain their original source receipts,
and are never approvals or training labels. Output points are observed mask run
centres; polynomial fits are used only to associate pieces and select slots.
Nothing is extended through an occlusion, between dashes, or beyond paint.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import time

import cv2
import numpy as np

from .bootstrap import atomic_json
from .geometry import (GeoParams, _Geo, _lane_from_chain, assign_slots,
                       filter_segments, group_dashes, track_segments)
from .gpu_candidates import Sam3Candidate, candidate_version, profile
from .real_source_review import RealSourceReview

SCHEMA = "college_paint_proposal:v1"
VERSION = "model-observed-mask-runs:v3"
AUDIT_GATE_VERSION = "observed-order-and-unassigned:v2"
SOURCE = "college_station_lane_marking"
REASON = "native_coverage_incomplete"
EXTRACTION = GeoParams(horizon_y=.36, threshold=.35, min_segment_rows=8,
                       dash_max_dx_px=30, dash_max_dslope=.9)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(root, name):
    root = Path(root).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Artifact path leaves its source root")
    return path


def coverage_records(data_root):
    """Read current original holds without creating DB rows or changing votes."""
    store = RealSourceReview(Path(data_root), SOURCE)
    _, records, _, manifest_sha, audits_sha = store.snapshot(fresh=True)
    corrections = store.corrections()
    originals = [row for row in records.values() if REASON in row["qc_reasons"]]
    originals.sort(key=lambda row: (row["sequence_id"], row["source_member"]))
    remaining, completed = [], []
    for row in originals:
        effective = store._effective_qc_reasons(row, manifest_sha, audits_sha, corrections)
        (remaining if REASON in effective else completed).append(row)
    return store, remaining, completed, manifest_sha, audits_sha


def select_pilot(remaining, completed, limit=12):
    """Keep past approved fixes diagnostic-only and cover the remaining span."""
    if limit < 1:
        raise ValueError("Pilot limit must be positive")
    # Temporal ordering is natural frame order, rather than lexical 1,10,100.
    def frame_key(row):
        import re
        return [int(part) if part.isdigit() else part
                for part in re.split(r"(\d+)", row["source_member"])]
    remaining = sorted(remaining, key=frame_key)
    completed = sorted(completed, key=frame_key)
    selected = completed[:min(3, limit)]
    count = min(len(remaining), limit - len(selected))
    if count:
        selected += [remaining[int(index)] for index in np.linspace(0, len(remaining)-1, count)]
    return selected


def single_valued_sections(sections, probability):
    """Keep one actual observed point per row and retain section boundaries.

    The shared dash association tolerates tiny overlaps, useful for detection
    but ambiguous in a single-valued audit polyline. Such rows are canonicalized
    only for a drawable triage representation; the ambiguity is never cleared.
    """
    by_row = {}
    conflicts = set()
    for section_id, section in enumerate(sections):
        for point in section:
            x, y = point[:2]
            candidate = (float(probability[y, int(round(x))]), -section_id, point)
            if y in by_row:
                conflicts.add(y)
                if candidate[:2] > by_row[y][:2]:
                    by_row[y] = candidate
            else:
                by_row[y] = candidate
    result, run, previous_section = [], [], None
    for y in sorted(by_row):
        _, negative_section, point = by_row[y]
        section_id = -negative_section
        if run and (point[1] != run[-1][1]+1 or section_id != previous_section):
            if len(run) >= 2:
                result.append(run)
            run = []
        run.append(point)
        previous_section = section_id
    if len(run) >= 2:
        result.append(run)
    return result, len(conflicts)


def observed_geometry(lane_probability, road_probability=None, occlusion_probability=None,
                      params=None):
    """Associate observed pixels, with each section explicitly separated.

    SAM3 is a candidate detector, so mask support is evidence of the model's
    observation, never proof of physical paint. Every retained point must lie
    inside its accepted lane mask and outside its vehicle exclusion mask.
    """
    params = params or deepcopy(EXTRACTION)
    prob = np.asarray(lane_probability, dtype=np.float32)
    if prob.ndim != 2 or not np.isfinite(prob).all():
        raise ValueError("Expected a finite two-dimensional lane probability map")
    h, w = prob.shape
    mask = prob >= params.threshold
    if occlusion_probability is not None:
        occlusion = np.asarray(occlusion_probability)
        if occlusion.shape != prob.shape or not np.isfinite(occlusion).all():
            raise ValueError("Invalid vehicle exclusion map")
        mask &= occlusion < params.threshold
    if road_probability is not None:
        road = np.asarray(road_probability)
        if road.shape != prob.shape or not np.isfinite(road).all():
            raise ValueError("Invalid road map")
        # A missing road detection cannot fabricate a road region or labels.
        road = cv2.dilate((road >= params.threshold).astype(np.uint8), np.ones((9, 9), np.uint8))
        mask &= road.astype(bool)
    n, components, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    if n > 1:
        mask &= ~np.isin(components, np.flatnonzero(stats[:, cv2.CC_STAT_AREA] < 6))
    geo = _Geo((h, w), params, h-1)
    tracks, tracking_stats = track_segments(mask, geo)
    segments, filtering_stats = filter_segments(tracks, geo)
    chains = group_dashes(segments, geo)
    associations = [_lane_from_chain(chain, geo) for chain in chains]
    assign_slots(associations, geo)
    lanes = []
    rejected = 0
    rejected_long = 0
    for chain, association in zip(chains, associations):
        if association["slot"] not in (1, 2, 3, 4):
            rejected += 1
            rejected_long += int(chain.y_bottom-chain.y_top >= .1*h and len(chain.ys) >= .04*h)
            continue
        sections = []
        for segment in chain.segments:
            points = []
            for x, y in sorted(zip(segment.xs, segment.ys), key=lambda pair: pair[1]):
                xi, yi = int(round(float(x))), int(y)
                if not (0 <= xi < w and 0 <= yi < h and mask[yi, xi]):
                    raise ValueError("Extracted point does not lie inside observed model evidence")
                points.append([round(float(x), 3), yi, True])
            run = []
            for point in points:
                if run and point[1] != run[-1][1] + 1:
                    if len(run) >= 2:
                        sections.append(run)
                    run = []
                run.append(point)
            if len(run) >= 2:
                sections.append(run)
        sections.sort(key=lambda section: section[0][1])
        sections, overlapping_rows = single_valued_sections(sections, prob)
        points = [point for section in sections for point in section]
        if len(points) < 2:
            rejected += 1
            continue
        # Dropping all fitted coordinates also avoids extending the apparent
        # boundary across dashed gaps when clients draw visible sections.
        lanes.append({"slot": association["slot"], "points": points, "segments": sections,
                      "kind": "dashed" if len(sections) > 1 else "unknown",
                      "physical": True, "visible_paint_verified": False,
                      "physical_evidence": "sam3_candidate_mask_unverified",
                      "visibility": "model_mask_observation_unverified",
                      "y_top": points[0][1], "y_bottom": points[-1][1],
                      "association_x_ref": association["x_ref"],
                      "flags": ["model_proposal_requires_sample_audit"],
                      "overlapping_section_rows": overlapping_rows,
                      "model_support": {"point_count": len(points),
                                        "minimum_probability": float(min(prob[y, int(round(x))] for x, y, _ in points))}})
    lanes.sort(key=lambda lane: lane["slot"])
    return {"schema": SCHEMA, "version": VERSION, "origin": "model_proposal",
            "coordinate_system": "original_image_pixels", "width": w, "height": h,
            "horizon_y": geo.hy, "valid_bottom": h-1, "lanes": lanes,
            "label_policy": "model_observed_sections_only_no_gap_fill_no_road_edges_no_extension",
            "candidate_only": True, "training_eligible": False, "human_complete_annotation": False,
            "tags": {"lane_count": len(lanes), "no_lanes": not bool(lanes)},
            "stats": {**tracking_stats, **filtering_stats, "tracks": len(tracks),
                      "chains": len(chains), "unassigned_chains": rejected,
                      "long_unassigned_chains": rejected_long,
                      "accepted_mask_pixels": int(mask.sum())},
            "extraction_parameters": asdict(params)}


def audit_disposition(geometry):
    """Keep uncertain branch/slot associations out of a ready sample cohort."""
    lanes, reasons = geometry["lanes"], []
    slots = {lane["slot"] for lane in lanes}
    if len(lanes) >= 4:
        reasons.append("branch_or_exit_gore_candidates")
    if len(lanes) < 3:
        reasons.append("incomplete_three_boundary_candidate")
    if not {2, 3}.issubset(slots):
        reasons.append("ego_corridor_candidate_incomplete")
    if geometry["stats"].get("long_unassigned_chains", 0):
        reasons.append("unassigned_long_marking_candidate")
    if (any(lane.get("overlapping_section_rows", 0) for lane in lanes)
            or geometry.get("representation_repair", {}).get("overlapping_rows_per_slot")):
        reasons.append("overlapping_observed_section_association")
    ordered = sorted(lanes, key=lambda lane: lane["slot"])
    for index, first in enumerate(ordered):
        first_rows = {y:x for x,y,*_ in first["points"]}
        for second in ordered[index+1:]:
            second_rows = {y:x for x,y,*_ in second["points"]}
            common = set(first_rows)&set(second_rows)
            if any(first_rows[y] >= second_rows[y] for y in common):
                reasons.append("observed_slot_order_reversal_or_crossing")
    for side in ({1, 2}, {3, 4}):
        selected = [lane for lane in lanes if lane["slot"] in side]
        if len(selected) == 2 and abs(selected[0]["association_x_ref"]-selected[1]["association_x_ref"]) < .055*geometry["width"]:
            reasons.append("potential_duplicate_boundary_association")
    return {"repair_disposition": "held" if reasons else "ready_for_audit",
            "repair_qc_reasons": sorted(set(reasons)),
            "audit_gate_version": AUDIT_GATE_VERSION,
            "candidate_caveat": "Model-proposed observed paint sections; source coverage remains held until separate human sample audit"}


def _write_compact_evidence(path, lane, road, occlusion, params):
    """Quantized probabilities plus exact pre-quantization threshold evidence."""
    mask = lane >= params.threshold
    mask &= occlusion < params.threshold
    mask &= cv2.dilate((road >= params.threshold).astype(np.uint8), np.ones((9, 9), np.uint8)).astype(bool)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, lane_u8=np.rint(np.clip(lane, 0, 1)*255).astype(np.uint8),
                        road_u8=np.rint(np.clip(road, 0, 1)*255).astype(np.uint8),
                        occlusion_u8=np.rint(np.clip(occlusion, 0, 1)*255).astype(np.uint8),
                        accepted_mask_bits=np.packbits(mask, axis=1),
                        original_shape=np.asarray(lane.shape, np.int32))
    return {"probability_storage": "round(probability*255), uint8; maximum quantization error 0.5/255",
            "accepted_mask_storage": "np.packbits(axis=1), original_shape retained; exact before probability quantization",
            "accepted_mask_policy": "lane>=threshold AND occlusion<threshold AND dilate(road>=threshold,9x9)",
            "threshold": params.threshold}


def run_adapted(data_root, output, *, limit=None):
    """Inference with the existing trained local model; no optimizer or training."""
    from filelock import FileLock
    import torch
    from .annotation_training import cached_model
    if not torch.cuda.is_available():
        raise RuntimeError("Existing adapted model inference requires CUDA")
    data_root, output = Path(data_root).resolve(), Path(output).resolve()
    store, remaining, completed, manifest_sha, audits_sha = coverage_records(data_root)
    if output == store.root.resolve() or output.is_relative_to(store.root.resolve()):
        raise ValueError("Proposals must stay outside the original import")
    import re
    remaining.sort(key=lambda row: [int(part) if part.isdigit() else part
                                    for part in re.split(r"(\d+)", row["source_member"])])
    rows = remaining[:limit] if limit else remaining
    checkpoint = data_root / "models/comma_segnet_adapted/run-20261018-233159/best.pt"
    expected_sha = "77fe3e8cb22654e0fd4a0fc07ad9b9c9c5ebfff5671f9aa3dcdc83b40e203438"
    if sha256(checkpoint) != expected_sha:
        raise ValueError("The selected local adapted checkpoint bytes changed")
    state = torch.load(checkpoint, map_location="cpu", weights_only=True)
    if (state.get("schema") != "comma_segnet_adapted:v1" or state.get("trained_steps", 0) < 1
            or not state.get("metrics") or state.get("test_images_used") != 0):
        raise ValueError("The adapted checkpoint lacks its actual trained/validated provenance")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    torch.set_num_threads(2)
    cv2.setNumThreads(1)
    params = replace(EXTRACTION, threshold=.5)
    receipt = {"model": "comma_segnet_adapted", "checkpoint": str(checkpoint),
               "checkpoint_sha256": expected_sha, "trained_steps": state["trained_steps"],
               "epoch": state["epoch"], "metrics": state["metrics"], "license": "MIT",
               "training_cohort_sha256": state["config"].get("cohort_sha256"),
               "input": [state["config"]["width"], state["config"]["height"]],
               "input_range": [0, 255], "class_names": state["config"]["class_names"],
               "source_guided": False, "reference_inputs": [], "candidate_only": True,
               "auto_promote": False, "training_performed_this_run": False,
               "device": torch.cuda.get_device_name(0), "precision": "bfloat16_autocast",
               "selection_reason": "Same 12-source-photo diagnostic: ~10x faster than pinned SAM3, recovered visible missing interior paint with cleaner association on straight examples; branch examples remain held",
               "selection_scope": "proposal recipe selection only; not a held-out accuracy benchmark"}
    receipt["training_license_receipts"] = {
        path.name: {"path": str(path), "sha256": sha256(path)}
        for path in sorted((checkpoint.parent/"licenses").glob("*")) if path.is_file()}
    output.mkdir(parents=True, exist_ok=True)
    status = {"schema": "college_paint_proposal_index:v1", "state": "loading", "version": VERSION,
              "teacher": "comma_segnet_adapted", "candidate_only": True, "training_eligible": False,
              "training_performed": False, "source_manifest_sha256": manifest_sha,
              "source_audits_sha256": audits_sha, "source_remaining_count": len(remaining),
              "diagnostic_approved_count": len(completed), "mode": "complete_remaining" if not limit else "bounded_remaining",
              "total": len(rows), "done": 0, "ready_for_audit": 0, "held": 0, "failed": [],
              "model_forwards_this_run": 0, "image_encoder_forwards_this_run": 0,
              "resumed": 0, "model_receipt": receipt, "extraction_parameters": asdict(params), "rows": []}
    protected = [store.root/name for name in ("import.json", "manifest.jsonl", "audits.json", "group_warning_clearance.json", "review.sqlite")]
    before = {str(path): sha256(path) for path in protected if path.exists()}
    started = time.monotonic()
    model = None
    index_path = output/"proposal-index.json"
    with FileLock(str(output/".proposal-generation.lock"), timeout=0), FileLock(str(data_root/"logs/large-models.lock"), timeout=0):
        atomic_json(index_path, status)
        try:
            model, pretrained = cached_model()
            receipt["pretrained_architecture"] = pretrained
            model.load_state_dict(state["model"])
            model.to("cuda").eval()
            del state
            status.update(state="running", parameters=sum(p.numel() for p in model.parameters()))
            for position, row in enumerate(rows):
                image_id = row["image_id"]
                relative = "proposals/"+image_id+".json"
                path = output/relative
                try:
                    if path.is_file():
                        proposal = json.loads(path.read_text(encoding="utf-8"))
                        if (proposal.get("version") != VERSION or proposal.get("source_receipt") != row
                                or proposal.get("model_receipt", {}).get("checkpoint_sha256") != expected_sha
                                or proposal.get("extraction_parameters") != asdict(params)):
                            raise ValueError("Cached local proposal differs from frozen recipe/source")
                        if any(sha256(_inside(output, asset["path"])) != asset["sha256"] for asset in proposal["evidence"].values()):
                            raise ValueError("Cached local proposal evidence changed")
                        if proposal.get("audit_gate_version") != AUDIT_GATE_VERSION:
                            # The initial unreviewed pilot may predate the final
                            # ordering gate. Reuse its exact accepted pixels,
                            # never rerun a model or alter an audited artifact.
                            with np.load(_inside(output, proposal["evidence"]["model_probability_maps"]["path"])) as evidence:
                                h, w = evidence["original_shape"]
                                accepted = np.unpackbits(evidence["accepted_mask_bits"], axis=1)[:, :w]
                                refreshed = observed_geometry(accepted.astype(np.float32), params=params)
                                quantized_lane = evidence["lane_u8"]
                                for lane in refreshed["lanes"]:
                                    lane["model_support"]["minimum_probability"] = float(min(quantized_lane[y, int(round(x))]/255 for x,y,*_ in lane["points"]))
                                    lane["model_support"]["probability_precision"] = "quantized_uint8; exact accepted binary mask retained"
                            proposal.update(refreshed)
                            proposal.update(audit_disposition(proposal))
                            atomic_json(path, proposal)
                        status["resumed"] += 1
                    else:
                        for key, digest in (("image", "source_sha256"), ("geometry", "geometry_sha256"), ("source_label", "source_label_sha256")):
                            if sha256(_inside(store.root, row[key])) != row[digest]:
                                raise ValueError("Original source evidence changed")
                        rgb = cv2.cvtColor(cv2.imread(str(_inside(store.root, row["image"]))), cv2.COLOR_BGR2RGB)
                        h, w = rgb.shape[:2]
                        resized = cv2.resize(rgb, tuple(receipt["input"]), interpolation=cv2.INTER_AREA)
                        tensor = torch.from_numpy(resized).permute(2, 0, 1)[None].float().to("cuda")
                        forward_start = time.monotonic()
                        with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
                            logits = model(tensor)
                        probability = torch.softmax(logits.float(), dim=1)[0].cpu().numpy()
                        forward_seconds = time.monotonic()-forward_start
                        status["model_forwards_this_run"] += 1
                        status["image_encoder_forwards_this_run"] += 1
                        p = np.stack([cv2.resize(channel, (w, h), interpolation=cv2.INTER_LINEAR) for channel in probability])
                        lane, road, occlusion = p[1], p[0]+p[1], p[3]+p[4]
                        proposal = observed_geometry(lane, road, occlusion, params)
                        proposal.update(audit_disposition(proposal))
                        proposal.update(image_id=image_id, teacher="comma_segnet_adapted",
                                        teacher_version="comma_segnet_adapted:"+expected_sha+":"+VERSION,
                                        source_receipt=deepcopy(row), model_receipt=receipt,
                                        model_meta={"model_forwards": 1, "image_encoder_forwards": 1,
                                                    "source_guided": False, "reference_inputs": [], "input": receipt["input"]},
                                        inference_seconds=round(forward_seconds, 4), source_guided=False,
                                        reference_inputs=[], diagnostic_only=False)
                        evidence_path = output/"evidence"/(image_id+".npz")
                        proposal["evidence_encoding"] = _write_compact_evidence(evidence_path, lane, road, occlusion, params)
                        preview_path = output/"previews"/(image_id+".jpg")
                        preview_path.parent.mkdir(parents=True, exist_ok=True)
                        native = json.loads(_inside(store.root, row["geometry"]).read_text(encoding="utf-8"))
                        if not cv2.imwrite(str(preview_path), proposal_preview(rgb, native, proposal, lane, .5), [cv2.IMWRITE_JPEG_QUALITY, 85]):
                            raise ValueError("Cannot save proposal preview")
                        proposal["evidence"] = {"model_probability_maps": {"path": evidence_path.relative_to(output).as_posix(), "sha256": sha256(evidence_path)},
                                                "comparison_preview": {"path": preview_path.relative_to(output).as_posix(), "sha256": sha256(preview_path)}}
                        atomic_json(path, proposal)
                    disposition = proposal["repair_disposition"]
                    status[disposition] += 1
                    status["rows"].append({"image_id": image_id, "proposal": relative, "sha256": sha256(path),
                                           "source_member": row["source_member"], "source_qc_reasons": row["qc_reasons"],
                                           "repair_disposition": disposition, "repair_qc_reasons": proposal["repair_qc_reasons"],
                                           "lanes": len(proposal["lanes"]), "slots": [lane["slot"] for lane in proposal["lanes"]],
                                           "temporal_block": position//100,
                                           "observed_sections": sum(len(lane["segments"]) for lane in proposal["lanes"])})
                    status["done"] += 1
                    status["last_image"] = image_id
                except Exception as error:
                    status["failed"].append({"image_id": image_id, "error_type": type(error).__name__, "reason": str(error)[:200]})
                if (position+1)%10 == 0 or position+1 == len(rows):
                    status["elapsed_seconds"] = round(time.monotonic()-started, 1)
                    atomic_json(index_path, status)
                if (position+1)%100 == 0:
                    print(json.dumps({key:status[key] for key in ("done", "total", "ready_for_audit", "held", "elapsed_seconds")}), flush=True)
        finally:
            if model is not None:
                del model
                torch.cuda.empty_cache()
            after = {str(path): sha256(path) for path in protected if path.exists()}
            status.update(state="complete" if not status["failed"] and status["done"] == len(rows) else "partial_error",
                          protected_files_unchanged=before == after, protected_files_before=before,
                          protected_files_after=after, elapsed_seconds=round(time.monotonic()-started, 1))
            atomic_json(index_path, status)
            if before != after:
                raise RuntimeError("Original source/review state changed during proposal generation")
    return status


def proposal_preview(rgb, source, proposal, lane_probability, threshold=.35):
    """Original / native / proposed visible sections; no gap joins."""
    base = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    original, native, model = base.copy(), base.copy(), base.copy()
    tint = model.copy()
    tint[lane_probability >= threshold] = (40, 210, 40)
    model = cv2.addWeighted(model, .78, tint, .22, 0)
    colors = {1: (220, 80, 220), 2: (40, 200, 255), 3: (50, 255, 80), 4: (255, 180, 50)}
    for panel, geometry in ((native, source), (model, proposal)):
        for lane in geometry["lanes"]:
            sections = lane.get("segments", [lane["points"]])
            for section in sections:
                section = section.get("points") if isinstance(section, dict) else section
                points = np.rint([point[:2] for point in section]).astype(np.int32)
                if len(points) > 1:
                    cv2.polylines(panel, [points], False, colors.get(lane.get("slot"), (255, 255, 255)), 3)
    panels = []
    for label, panel in (("Original source photo", original), ("Original native annotation (unchanged)", native),
                         (geometry_label(proposal), model)):
        cv2.rectangle(panel, (0, 0), (panel.shape[1], 40), (20, 20, 20), -1)
        cv2.putText(panel, label, (15, 28), cv2.FONT_HERSHEY_SIMPLEX, .7, (255, 255, 255), 2)
        panels.append(cv2.resize(panel, (820, 295)))
    return np.concatenate(panels, axis=0)


def geometry_label(proposal):
    name = "Adapted comma" if proposal.get("teacher") == "comma_segnet_adapted" else "SAM3"
    return name + " observed sections - pending audit"


def run(data_root, output, *, limit=12, complete=False):
    """Create resumable isolated candidates; never touch registry/human state."""
    from filelock import FileLock
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("Pinned SAM3 requires CUDA; no smaller model fallback is permitted")
    data_root, output = Path(data_root).resolve(), Path(output).resolve()
    store, remaining, completed, manifest_sha, audits_sha = coverage_records(data_root)
    if output == store.root.resolve() or output.is_relative_to(store.root.resolve()):
        raise ValueError("Proposals must be outside the original source import")
    rows = remaining if complete else select_pilot(remaining, completed, limit)
    output.mkdir(parents=True, exist_ok=True)
    cfg = profile()["models"]["sam3"]
    version = candidate_version("sam3", cfg)
    # Explicit offline mode prevents model/proc downloads or revision drift.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from huggingface_hub import hf_hub_download
    checkpoint = Path(hf_hub_download(cfg["hub_id"], filename="model.safetensors",
                                    revision=cfg["revision"], local_files_only=True))
    receipt = {"hub_id": cfg["hub_id"], "revision": cfg["revision"],
               "checkpoint_sha256": sha256(checkpoint), "teacher_version": version,
               "license": cfg["license"], "commercial_status": cfg["commercial_status"],
               "license_sha256": sha256(data_root / "licenses" / "sam3-LICENSE.txt"),
               "configuration": cfg, "device": torch.cuda.get_device_name(0),
               "precision": "bfloat16", "source_guided": False, "reference_inputs": []}
    status = {"schema": "college_paint_proposal_index:v1", "state": "loading", "version": VERSION,
              "candidate_only": True, "training_eligible": False,
              "source_manifest_sha256": manifest_sha, "source_audits_sha256": audits_sha,
              "source_remaining_count": len(remaining), "diagnostic_approved_count": len(completed),
              "mode": "complete_remaining" if complete else "representative_pilot",
              "total": len(rows), "done": 0, "failed": [], "model_receipt": receipt,
              "model_forwards_this_run": 0, "image_encoder_forwards_this_run": 0,
              "prompt_forwards_this_run": 0, "resumed": 0, "rows": []}
    original_db = store.root / "review.sqlite"
    protected_paths = [store.root / name for name in ("import.json", "manifest.jsonl", "audits.json", "group_warning_clearance.json")]
    protected_paths += [original_db] if original_db.exists() else []
    before = {str(path): sha256(path) for path in protected_paths}
    teacher = Sam3Candidate(cfg)
    with FileLock(str(output / ".proposal-generation.lock"), timeout=0):
        atomic_json(output / "proposal-index.json", status)
        loaded = False
        try:
            teacher.load()
            loaded = True
            status.update(state="running", parameters=sum(p.numel() for p in teacher.model.parameters()))
            for row in rows:
                image_id = row["image_id"]
                relative = "proposals/" + image_id + ".json"
                proposal_path = output / relative
                entry = {"image_id": image_id, "proposal": relative,
                         "source_member": row["source_member"],
                         "diagnostic_only": row in completed}
                try:
                    if proposal_path.is_file():
                        cached = json.loads(proposal_path.read_text(encoding="utf-8"))
                        if (cached.get("version") != VERSION or cached.get("source_receipt") != row
                                or cached.get("teacher_version") != version):
                            raise ValueError("Existing proposal does not match the frozen source/model")
                        for evidence in cached["evidence"].values():
                            if sha256(_inside(output, evidence["path"])) != evidence["sha256"]:
                                raise ValueError("Cached proposal evidence changed")
                        proposal = cached
                        status["resumed"] += 1
                    else:
                        for key, digest in (("image", "source_sha256"), ("geometry", "geometry_sha256"),
                                            ("source_label", "source_label_sha256")):
                            if sha256(_inside(store.root, row[key])) != row[digest]:
                                raise ValueError("Source evidence bytes changed")
                        rgb = cv2.cvtColor(cv2.imread(str(_inside(store.root, row["image"]))), cv2.COLOR_BGR2RGB)
                        started = time.monotonic()
                        prediction = teacher.predict(rgb)
                        for key in ("model_forwards", "image_encoder_forwards", "prompt_forwards"):
                            status[key + "_this_run"] += int(teacher.last_counts.get(key, 0))
                        proposal = observed_geometry(prediction.lane, prediction.road, prediction.extra.get("occlusion"))
                        proposal.update(image_id=image_id, teacher="sam3", teacher_version=version,
                                        source_receipt=deepcopy(row), model_receipt=receipt,
                                        model_meta=prediction.meta, diagnostic_only=row in completed,
                                        inference_seconds=round(time.monotonic()-started, 3),
                                        source_guided=False, reference_inputs=[])
                        evidence_path = output / "evidence" / (image_id + ".npz")
                        evidence_path.parent.mkdir(parents=True, exist_ok=True)
                        np.savez_compressed(evidence_path, lane=prediction.lane.astype(np.float16),
                                            road=prediction.road.astype(np.float16),
                                            occlusion=prediction.extra["occlusion"].astype(np.float16))
                        original = json.loads(_inside(store.root, row["geometry"]).read_text(encoding="utf-8"))
                        preview_path = output / "previews" / (image_id + ".jpg")
                        preview_path.parent.mkdir(parents=True, exist_ok=True)
                        if not cv2.imwrite(str(preview_path), proposal_preview(rgb, original, proposal, prediction.lane)):
                            raise ValueError("Cannot write proposal visual evidence")
                        proposal["evidence"] = {"model_probability_maps": {"path": evidence_path.relative_to(output).as_posix(), "sha256": sha256(evidence_path)},
                                                "comparison_preview": {"path": preview_path.relative_to(output).as_posix(), "sha256": sha256(preview_path)}}
                        atomic_json(proposal_path, proposal)
                    entry.update(sha256=sha256(proposal_path), lanes=len(proposal["lanes"]),
                                 observed_sections=sum(len(lane["segments"]) for lane in proposal["lanes"]),
                                 stats=proposal["stats"])
                    status["rows"].append(entry)
                    status["done"] += 1
                    status["last_image"] = image_id
                except Exception as error:
                    status["failed"].append({"image_id": image_id, "error_type": type(error).__name__, "reason": str(error)[:200]})
                atomic_json(output / "proposal-index.json", status)
        finally:
            if loaded:
                teacher.unload()
                torch.cuda.empty_cache()
            after = {str(path): sha256(path) for path in protected_paths}
            status.update(state="complete" if not status["failed"] and status["done"] == len(rows) else "partial_error",
                          protected_files_unchanged=before == after,
                          protected_files_before=before, protected_files_after=after)
            atomic_json(output / "proposal-index.json", status)
            if before != after:
                raise RuntimeError("Original source/review state changed during inference")
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="BrakeCheck_Dataset")
    parser.add_argument("--output", default="BrakeCheck_Dataset/experiments/college_held_repairs-20261022")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--complete", action="store_true")
    parser.add_argument("--teacher", choices=("sam3", "adapted"), default="sam3")
    args = parser.parse_args()
    status = (run_adapted(args.data_root, args.output, limit=None if args.complete else args.limit)
              if args.teacher == "adapted" else run(args.data_root, args.output, limit=args.limit, complete=args.complete))
    print(json.dumps({key: value for key, value in status.items() if key not in ("rows", "model_receipt")}))


if __name__ == "__main__":
    main()
