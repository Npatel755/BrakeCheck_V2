"""Reuse unchanged College audit confirmations only within their exact cohort.

This artifact creates no votes, edits no source geometry and cannot propagate
full-photo annotation claims. Every original sample must have an actual current
approval and unchanged geometry, with the same ordinary warnings confirmed.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import time

from fastapi import HTTPException

from .simulation_review import _digest, _inside

SCHEMA = "college_group_warning_clearance:v1"
SOURCE = "college_station_lane_marking"
FILENAME = "group_warning_clearance.json"


def has_observed_training_span(geometry):
    """Require two CULane output rows without inventing a boundary extension."""
    height = geometry["height"]
    # The converted-only export uses a 590-pixel canvas, sampled at 250..580.
    anchors = [row * height / 590 for row in range(250, 581, 10)]
    trainable = False
    for lane in geometry["lanes"]:
        rows, spans = set(), []
        for section in lane.get("segments", [lane["points"]]):
            points = section.get("points") if isinstance(section, dict) else section
            visible = []
            runs = []
            for point in points:
                if len(point) > 2 and point[2] is False:
                    runs.append(visible)
                    visible = []
                else:
                    visible.append(point)
            runs.append(visible)
            for run in runs:
                if len(run) < 2:
                    continue
                ys = [point[1] for point in run]
                if not (all(a < b for a, b in zip(ys, ys[1:]))
                        or all(a > b for a, b in zip(ys, ys[1:]))):
                    return False
                first, last = min(ys), max(ys)
                if any(max(first, a) < min(last, b) for a, b in spans):
                    return False
                spans.append((first, last))
                rows.update(index for index, y in enumerate(anchors) if first <= y <= last)
        trainable |= len(rows) >= 2
    return trainable


def _hash(value):
    return _digest(json.dumps(value, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _code_fingerprints():
    package = Path(__file__).resolve().parent
    return {name: _digest((package / name).read_bytes()) for name in (
        "college_group_clearance.py", "real_source_review.py", "simulation_review.py")}


def _physical_lanes(store, geometry):
    normalized = store._validate_lanes(geometry["lanes"], geometry["width"], geometry["height"],
                                       source_metadata=True)
    original = {lane["slot"]: lane for lane in geometry["lanes"]}
    result = []
    for cleaned in normalized:
        lane = original[cleaned["slot"]]
        physical = deepcopy(cleaned)
        # An acknowledgement cannot erase visibility or join separated paint.
        # Unused fitted/reference metadata does not define the displayed label.
        physical["point_metadata"] = [deepcopy(point[2:]) for point in lane["points"]]
        physical["physical"] = lane.get("physical", True)
        physical["kind"] = lane.get("kind")
        if "segments" in lane:
            sections = [segment.get("points") if isinstance(segment, dict) else segment
                        for segment in lane["segments"]]
            physical["segment_point_metadata"] = [[deepcopy(point[2:]) for point in section]
                                                    for section in sections]
        result.append(physical)
    return sorted(result, key=lambda lane: lane["slot"])


def _events(store, decisions):
    event_ids = [decision["event_id"] for decision in decisions.values()
                 if isinstance(decision, dict) and isinstance(decision.get("event_id"), str)]
    if not event_ids:
        return {}
    path = _inside(store._root(), "review.sqlite")
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='decision_events'").fetchone():
            return {}
        result = {}
        for offset in range(0, len(event_ids), 500):
            chunk = event_ids[offset:offset + 500]
            query = "SELECT * FROM decision_events WHERE event_id IN (" + ",".join("?" for _ in chunk) + ")"
            result.update({row["event_id"]: dict(row) for row in con.execute(query, chunk)})
        return result
    finally:
        con.close()


def _current_event(decision, events):
    event = events.get(decision.get("event_id"))
    if (not event or any(event.get(key) != decision.get(key)
                         for key in ("image_id", "action", "reviewer", "event_id", "fingerprint", "revision"))
            or type(decision.get("revision")) is not int or decision["revision"] < 1
            or event.get("previous_revision") != decision["revision"] - 1
            or event.get("created_at") != decision.get("updated_at")
            or not isinstance(decision.get("reviewer"), str) or not decision["reviewer"].strip()):
        return None
    return event


def _group_proof(store, key, group, records, manifest_sha, audits_sha, decisions, corrections, events):
    samples = group["sample_ids"]
    if (not samples or len(samples) != len(set(samples)) or len(samples) != min(3, group["count"])):
        return None
    common = None
    proofs = {}
    for image_id in samples:
        item = records.get(image_id)
        decision = decisions.get(image_id)
        if (not item or item["source"] != SOURCE or item["group_key"] != key
                or item["split"] != group["split"] or not decision or decision.get("action") != "approve"):
            return None
        correction = store._active_correction(item, manifest_sha, audits_sha, corrections)
        if not correction or correction.get("image_id") != image_id:
            return None
        fingerprint = store.effective_fingerprint(item, manifest_sha, audits_sha, corrections)
        event = _current_event(decision, events)
        if (decision.get("image_id") != image_id or decision.get("fingerprint") != fingerprint
                or not event):
            return None
        source = store._verified(item, geometry=True)
        corrected = store._correction_geometry(correction, source)
        info = store._warning_info(item, corrected)
        if (corrected.get("human_complete_annotation") is True
                or corrected.get("annotation_scope") == "single_photo_manual_replacement"
                or info["qc_reasons"]
                or _hash(_physical_lanes(store, source)) != _hash(_physical_lanes(store, corrected))):
            return None
        # Existing equality semantics preserve untouched imported point/section
        # metadata. Also require segment presence symmetrically in the canonical
        # representation above, so dropping all sections cannot qualify.
        source_lanes = {lane["slot"]: lane for lane in source["lanes"]}
        submitted = store._validate_lanes(corrected["lanes"], corrected["width"], corrected["height"],
                                          source_metadata=True)
        if any(not store._same_lane_geometry(source_lanes[lane["slot"]], lane) for lane in submitted):
            return None
        ordinary = set(store._repairable_qc_reasons(item)) - set(store._complete_annotation_qc_reasons(item))
        confirmed = set(item["qc_reasons"]) & set(info["resolved_qc_reasons"]) & ordinary
        common = confirmed if common is None else common & confirmed
        if not common:
            return None
        # _verified checked the image, original geometry and native-label bytes;
        # _correction_geometry separately checked the saved correction bytes.
        proofs[image_id] = {
            "item": deepcopy(item), "decision": deepcopy(decision), "decision_event": deepcopy(event),
            "correction": deepcopy(correction), "source_fingerprint": store.fingerprint(item, manifest_sha, audits_sha),
            "effective_fingerprint": fingerprint,
            "files": {"image": {"path": item["image"], "sha256": item["source_sha256"]},
                      "geometry": {"path": item["geometry"], "sha256": item["geometry_sha256"]},
                      "native_label": {"path": item["source_label"], "sha256": item["source_label_sha256"]},
                      "correction": {"path": correction["geometry_path"], "sha256": correction["geometry_sha256"]}},
            "physical_geometry_sha256": _hash(_physical_lanes(store, source)),
            "source_qc_reasons": deepcopy(item["qc_reasons"]),
            "resolved_qc_reasons": info["resolved_qc_reasons"], "geometry_unchanged": True,
        }
    return {"group_key": key, "group": deepcopy(group), "sample_ids": list(samples),
            "resolved_qc_reasons": sorted(common), "samples": proofs,
            "sample_decisions": [deepcopy(decisions[image_id]) for image_id in samples],
            "scope": "same_import_same_group_ordinary_warning_codes_only",
            "geometry_unchanged": True, "full_photo_annotations_propagated": False,
            "human_decisions_created": 0}


def prepare(store):
    """Build a reviewable artifact from existing votes; never write import data."""
    if getattr(store, "source", None) != SOURCE:
        raise ValueError("Group warning clearance is restricted to College Station")
    _, records, groups, manifest_sha, audits_sha = store.snapshot(fresh=True)
    decisions, corrections = store.decisions(), store.corrections()
    events = _events(store, decisions)
    code = _code_fingerprints()
    proofs = {}
    excluded = {}
    for key, group in sorted(groups.items()):
        try:
            proof = _group_proof(store, key, group, records, manifest_sha, audits_sha,
                                 decisions, corrections, events)
        except (HTTPException, OSError, ValueError, TypeError, KeyError, AttributeError):
            proof = None
        if proof:
            proofs[key] = proof
        else:
            excluded[key] = "All original samples must have current approvals, unchanged geometry and common ordinary warning confirmations"
    if code != _code_fingerprints():
        raise ValueError("College clearance validation code changed while preparing")
    artifact = {"schema": SCHEMA, "source": SOURCE, "manifest_sha256": manifest_sha,
                "audits_sha256": audits_sha, "code_sha256": code, "groups": proofs,
                "group_count": len(proofs), "sample_count": sum(len(proof["samples"]) for proof in proofs.values()),
                "excluded_groups": excluded, "human_decisions_created": 0,
                "full_photo_annotations_propagated": False, "auto_promote": False,
                "created_at": time.time()}
    artifact["content_sha256"] = _hash(artifact)
    return artifact


def _verify(store, artifact, records, groups, manifest_sha, audits_sha, decisions, corrections):
    if (getattr(store, "source", None) != SOURCE or not isinstance(artifact, dict)
            or artifact.get("schema") != SCHEMA or artifact.get("source") != SOURCE
            or artifact.get("manifest_sha256") != manifest_sha or artifact.get("audits_sha256") != audits_sha
            or artifact.get("code_sha256") != _code_fingerprints()
            or artifact.get("human_decisions_created") != 0
            or artifact.get("full_photo_annotations_propagated") is not False
            or artifact.get("auto_promote") is not False):
        raise ValueError("College clearance policy, source snapshot or validation code changed")
    content = {key: value for key, value in artifact.items() if key != "content_sha256"}
    if artifact.get("content_sha256") != _hash(content) or not isinstance(artifact.get("groups"), dict):
        raise ValueError("College clearance artifact bytes changed")
    selected = artifact["groups"]
    if artifact.get("group_count") != len(selected):
        raise ValueError("College clearance group count changed")
    sample_decisions = {image_id: decisions[image_id] for proof in selected.values()
                        for image_id in proof["sample_ids"] if image_id in decisions}
    events = _events(store, sample_decisions)
    verified = {}
    sample_count = 0
    for key, proof in selected.items():
        if key not in groups:
            raise ValueError("College clearance no longer refers to an existing cohort")
        fresh = _group_proof(store, key, groups[key], records, manifest_sha, audits_sha,
                             decisions, corrections, events)
        if not fresh or proof != fresh:
            raise ValueError("College clearance sample decision, correction, group or source bytes changed")
        verified[key] = fresh
        sample_count += len(fresh["samples"])
    if sample_count != artifact.get("sample_count"):
        raise ValueError("College clearance sample count changed")
    return verified


def verify(store, artifact):
    """Revalidate a prepared artifact against current votes and sample bytes."""
    _, records, groups, manifest_sha, audits_sha = store.snapshot(fresh=True)
    return _verify(store, artifact, records, groups, manifest_sha, audits_sha,
                   store.decisions(), store.corrections())


def valid_clearances(store, records, groups, manifest_sha, audits_sha, decisions, corrections):
    """Return verified existing cohort proofs, or no clearance on any mismatch."""
    if getattr(store, "source", None) != SOURCE:
        return {}
    try:
        path = _inside(store._root(), FILENAME)
        artifact = json.loads(path.read_bytes())
        return _verify(store, artifact, records, groups, manifest_sha, audits_sha, decisions, corrections)
    except (HTTPException, OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        return {}
