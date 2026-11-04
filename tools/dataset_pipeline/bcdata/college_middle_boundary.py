"""User-directed continuous middle boundaries, separate from observed paint.

The published candidates and audit votes stay immutable. Derived annotations
retain the original painted sections and identify every interpolated gap.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import time
import uuid

from fastapi import HTTPException

from .bootstrap import atomic_json
from .simulation_review import _digest, _inside

POLICY = "user-requested-middle-dash-connections:v1"
INDEX = "middle_boundary_annotations.json"


def connect_middle(geometry, occlusion=None):
    """Join the middle of three ordered boundaries inside its existing extent."""
    lanes = sorted(geometry["lanes"], key=lambda lane: lane["slot"])
    if len(lanes) != 3 or [lane["slot"] for lane in lanes] not in ([1, 2, 3], [2, 3, 4]):
        return None, {"reason": "middle_boundary_identity_ambiguous"}
    middle = lanes[1]
    original_sections = deepcopy(middle.get("segments", [middle["points"]]))
    sections = [section.get("points") if isinstance(section, dict) else section for section in original_sections]
    if len(sections) < 2:
        return None, {"reason": "middle_boundary_already_connected", "middle_slot": middle["slot"]}
    sections = [sorted([point[:2] for point in section], key=lambda point: point[1]) for section in sections]
    if any(len(section) < 2 or any(a[1] >= b[1] for a, b in zip(section, section[1:])) for section in sections):
        return None, {"reason": "invalid_observed_middle_sections"}
    sections.sort(key=lambda section: section[0][1])
    if any(a[-1][1] >= b[0][1] for a, b in zip(sections, sections[1:])):
        return None, {"reason": "overlapping_middle_sections"}
    if occlusion is not None and tuple(occlusion.shape) != (geometry["height"], geometry["width"]):
        raise ValueError("Vehicle evidence dimensions do not match the annotation")
    joined = [deepcopy(sections[0])]
    inferred, blocked = [], []
    for section in sections[1:]:
        a, b = joined[-1][-1], section[0]
        points = []
        for y in range(math.floor(a[1]) + 1, math.ceil(b[1])):
            if not a[1] < y < b[1]:
                continue
            x = a[0] + (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1])
            points.append([round(x, 6), y])
        # Dash interpolation must not paint through a vehicle/occlusion mask.
        if occlusion is not None and any(occlusion[min(geometry["height"] - 1, max(0, int(round(y)))),
                                                  min(geometry["width"] - 1, max(0, int(round(x))))] >= 128
                                         for x, y in points):
            blocked.append({"from": a, "to": b, "reason": "vehicle_occlusion"})
            joined.append(deepcopy(section))
            continue
        inferred.append({"points": [deepcopy(a), *deepcopy(points), deepcopy(b)],
                         "observed_paint": False, "method": "linear_between_observed_endpoints"})
        joined[-1].extend(points)
        joined[-1].extend(deepcopy(section))
    if not inferred:
        return None, {"reason": "all_middle_gaps_occluded", "blocked_gaps": blocked}
    result = deepcopy(geometry)
    target = next(lane for lane in result["lanes"] if lane["slot"] == middle["slot"])
    target.update(points=[point for section in joined for point in section], segments=joined,
                  observed_paint_segments=original_sections, inferred_sections=inferred,
                  annotation_role="continuous_lane_boundary", visible_paint_verified=False,
                  physical_evidence="observed_sections_plus_user_requested_dash_interpolation")
    result["boundary_completion"] = {"policy": POLICY, "middle_slot": middle["slot"],
        "interpolated_gaps": len(inferred), "interpolated_points": sum(len(section["points"]) - 2 for section in inferred),
        "observed_paint_preserved": True, "extends_beyond_observed_endpoints": False,
        "blocked_gaps": blocked, "human_review_created": False,
        "interpretation": "Continuous boundary annotation; interpolated gaps are not observed paint."}
    result["version"] = POLICY
    from .college_repair_batch import observed_slot_order_ok
    if not observed_slot_order_ok(result):
        return None, {"reason": "interpolation_crosses_another_boundary"}
    return result, deepcopy(result["boundary_completion"])


def _index(store):
    path = _inside(store._root(), INDEX)
    if not path.exists():
        return None
    try:
        stamp = (path.stat().st_mtime_ns, path.stat().st_size)
        cached = getattr(store, "_middle_boundary_cache", None)
        if cached and cached[0] == stamp:
            return cached[1]
        raw = path.read_bytes()
        value = json.loads(raw)
        content = {key: item for key, item in value.items() if key != "content_sha256"}
        if (value.get("policy") != POLICY or value.get("human_decisions_created") != 0
                or value.get("content_sha256") != _digest(json.dumps(content, sort_keys=True, separators=(",", ":")).encode())
                or not isinstance(value.get("records"), dict) or not isinstance(value.get("proofs"), dict)):
            raise ValueError("Invalid middle-boundary annotation index")
        store._middle_boundary_cache = (stamp, value)
        return value
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(409, "Saved middle-boundary annotations changed; reload or repair them") from None


def apply_saved_connection(store, item, detail):
    index = _index(store)
    if not index:
        return detail
    if (index["manifest_sha256"] != detail["manifest_sha256"] or index["audits_sha256"] != detail["audits_sha256"]):
        raise HTTPException(409, "Saved boundary connections refer to a different audit batch")
    entry = index["records"].get(item["image_id"])
    if not entry or entry["baseline_fingerprint"] != detail["fingerprint"]:
        return detail
    if entry["candidate_geometry_sha256"] != item["geometry_sha256"]:
        raise HTTPException(409, "A middle-boundary connection lost its candidate binding")
    decisions = store.decisions()
    proof = index["proofs"][entry["proof_key"]]
    _, records, _, manifest_sha, audits_sha = store.snapshot()
    corrections = store.corrections()
    if any(decisions.get(image_id) != decision or decision.get("action") != "approve"
           or image_id not in records
           or decision["fingerprint"] != store.effective_fingerprint(records[image_id], manifest_sha, audits_sha, corrections)
           for image_id, decision in proof["decisions"].items()):
        return detail
    try:
        raw = _inside(store._root(), entry["geometry_path"]).read_bytes()
        if _digest(raw) != entry["geometry_sha256"]:
            raise ValueError("Derived annotation changed")
        geometry = json.loads(raw)
        if (geometry["image_id"] != item["image_id"] or geometry.get("boundary_completion", {}).get("policy") != POLICY
                or (geometry["width"], geometry["height"]) != (detail["geometry"]["width"], detail["geometry"]["height"])):
            raise ValueError("Derived annotation binding changed")
        store._validate_lanes(geometry["lanes"], geometry["width"], geometry["height"], source_metadata=True)
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(409, "The saved connected middle boundary changed") from None
    result = dict(detail)
    result.update(geometry=geometry, boundary_completion=geometry["boundary_completion"],
                  saved_annotation={"path": entry["geometry_path"], "sha256": entry["geometry_sha256"],
                    "policy": POLICY, "baseline_fingerprint": entry["baseline_fingerprint"],
                    "approval_scope": "Original observed candidate; gap completion separately authorized by the user."})
    return result


def save_approved_middle_connections(store):
    """Apply the explicit user instruction to approved samples and passed groups."""
    if (_inside(store._root(), INDEX)).exists():
        raise ValueError("Preserve the existing saved connection revision")
    _, records, groups, manifest_sha, audits_sha = store.snapshot(fresh=True)
    decisions, corrections = store.decisions(), store.corrections()
    audited = store._groups(records, groups, manifest_sha, audits_sha, decisions, verify_samples=True)
    passed = {group["group_key"] for group in audited if group["state"] == "eligible" and group["audit_mode"] == "ready"}
    selected, proofs = {}, {}
    for key in sorted(passed):
        proof_key = "cohort:" + key
        proofs[proof_key] = {"scope": "fully_approved_cohort", "group_key": key,
                            "decisions": {image_id: decisions[image_id] for image_id in groups[key]["sample_ids"]}}
        for image_id, item in records.items():
            if item["group_key"] == key:
                selected[image_id] = proof_key
    for image_id, decision in decisions.items():
        if (decision["action"] == "approve" and image_id not in selected
                and records[image_id].get("audit_mode", "ready") == "ready"
                and decision["fingerprint"] == store.effective_fingerprint(records[image_id], manifest_sha, audits_sha, corrections)):
            key = "sample:" + image_id
            proofs[key] = {"scope": "approved_sample_only", "group_key": records[image_id]["group_key"],
                           "decisions": {image_id: decision}}
            selected[image_id] = key
    revision = "boundary_annotations/middle-" + uuid.uuid4().hex[:12]
    entries, skipped = {}, []
    for number, (image_id, proof_key) in enumerate(sorted(selected.items()), 1):
        item = records[image_id]
        if image_id in corrections:
            skipped.append({"image_id": image_id, "reason": "existing_manual_correction_preserved"})
            continue
        geometry = store._verified(item, geometry=True)
        geometry["image_id"] = image_id
        occlusion = None
        assets = [asset for asset in item.get("evidence_files", []) if asset["path"].endswith(".npz")]
        if assets:
            import numpy as np
            with np.load(_inside(store._root(), assets[0]["path"])) as maps:
                if "occlusion_u8" in maps:
                    occlusion = maps["occlusion_u8"].copy()
                elif "occlusion" in maps:
                    occlusion = (maps["occlusion"] * 255).clip(0, 255).astype("uint8")
        connected, evidence = connect_middle(geometry, occlusion)
        if connected is None:
            skipped.append({"image_id": image_id, **evidence})
            continue
        connected["boundary_completion"].update(candidate_geometry_sha256=item["geometry_sha256"],
                                              proof_key=proof_key, user_directed=True)
        relative = revision + "/" + image_id + ".json"
        atomic_json(_inside(store._root(), relative), connected)
        entries[image_id] = {"geometry_path": relative, "geometry_sha256": _digest(_inside(store._root(), relative).read_bytes()),
            "candidate_geometry_sha256": item["geometry_sha256"], "proof_key": proof_key,
            "baseline_fingerprint": store.effective_fingerprint(item, manifest_sha, audits_sha, corrections),
            "middle_slot": evidence["middle_slot"], "interpolated_gaps": evidence["interpolated_gaps"],
            "interpolated_points": evidence["interpolated_points"], "blocked_gap_count": len(evidence["blocked_gaps"])}
        if number % 250 == 0:
            print(json.dumps({"phase": "saving_connected_middle_annotations", "processed": number, "total": len(selected)}), flush=True)
    latest = store.decisions()
    if any(latest.get(image_id) != decision for image_id, decision in decisions.items()) or store.corrections() != corrections:
        raise ValueError("Existing reviews changed during connection preparation; preserve this unpublished revision and recheck")
    index = {"schema": "college_middle_boundary_annotations:v1", "policy": POLICY,
        "authorization": "User explicitly requested saved annotations connected across dash gaps on approved audits.",
        "created_at": time.time(), "manifest_sha256": manifest_sha, "audits_sha256": audits_sha,
        "records": entries, "proofs": proofs, "skipped": skipped, "approved_samples_preserved": sum(d["action"] == "approve" for d in decisions.values()),
        "fully_approved_groups": len(passed), "selected_photos": len(selected),
        "saved_annotations": len(entries), "interpolated_gaps": sum(entry["interpolated_gaps"] for entry in entries.values()),
        "human_decisions_created": 0, "training_started": False, "auto_promote": False}
    index["content_sha256"] = _digest(json.dumps(index, sort_keys=True, separators=(",", ":")).encode())
    atomic_json(_inside(store._root(), INDEX), index)
    return {key: value for key, value in index.items() if key not in {"records", "proofs", "skipped"}} | {"skipped_photos": len(skipped)}
