"""Sample audits for the isolated, simulator-labelled TuLane import.

Nothing here opens the production registry or adds photos to a training run.
An approved cohort is merely eligible for a later, explicit experiment build.
"""
from __future__ import annotations

from collections import Counter
import copy
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import threading
import time
import uuid
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

API = "/api/studio/simulation"
_ID = re.compile(r"tulane-[a-f0-9]{16}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_EVENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{7,159}\Z")
_FINAL_STATES = {"ready", "partial_ready", "complete", "awaiting_audit", "awaiting_audits", "audit_ready"}


class SimulationDecision(BaseModel):
    image_id: str = Field(min_length=1, max_length=80)
    action: str
    event_id: str = Field(min_length=8, max_length=160)
    revision: int = Field(ge=0)
    fingerprint: str = Field(min_length=64, max_length=64)


class SimulationCorrection(BaseModel):
    image_id: str = Field(min_length=1, max_length=80)
    revision: int = Field(ge=0)
    fingerprint: str = Field(min_length=64, max_length=64)
    lanes: list[dict]
    resolved_qc_reasons: list[str] = Field(default_factory=list)
    approve: bool = False
    complete_annotation: bool = False
    no_visible_lane_paint: bool = False


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _relative(relative: str) -> PurePosixPath:
    if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
            or "\x00" in relative):
        raise HTTPException(409, "Invalid isolated simulation asset path")
    path = PurePosixPath(relative)
    if path.is_absolute() or PureWindowsPath(relative).is_absolute() or ".." in path.parts:
        raise HTTPException(409, "Simulation assets must stay inside their collection")
    return path


def _inside(root: Path, relative: str) -> Path:
    path = _relative(relative)
    resolved = (root / Path(*path.parts)).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise HTTPException(409, "Simulation assets must stay inside their collection")
    return resolved


def _sha(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise HTTPException(400, "Invalid simulated photo identifier")
    return value


class SimulationReview:
    def __init__(self, data_root: Path):
        self.data_root = Path(data_root).resolve()
        self.root = self.data_root / "experiments" / "simulated_tulane"
        self._cache = None
        self._lock = threading.RLock()

    def _root(self):
        resolved = self.root.resolve()
        if not resolved.is_relative_to(self.data_root) or resolved == self.data_root:
            raise HTTPException(409, "The simulation collection leaves its isolated directory")
        return resolved

    def _json(self, name, *, missing=False):
        path = _inside(self._root(), name)
        try:
            raw = path.read_bytes()
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value, _digest(raw)
        except FileNotFoundError:
            if missing:
                return {}, None
            raise HTTPException(409, "The simulated collection is still being prepared") from None
        except (OSError, ValueError):
            raise HTTPException(409, "Simulation metadata is unavailable or being updated") from None

    def _metadata(self):
        metadata, _ = self._json("import.json", missing=True)
        if metadata and (metadata.get("schema") != "tulane_simulation_import:v1"
                         or metadata.get("source") != "carlane_tulane"
                         or metadata.get("domain") != "synthetic"
                         or metadata.get("license") != "Apache-2.0"):
            raise HTTPException(409, "The import is not the cleared synthetic TuLane source")
        return metadata

    def snapshot(self, *, fresh=False):
        """Validate complete audit membership before any photo can be reviewed."""
        metadata = self._metadata()
        if not metadata or metadata.get("state") not in _FINAL_STATES:
            return metadata, {}, {}, None, None
        root = self._root()
        manifest_path = _inside(root, "manifest.jsonl")
        audits_path = _inside(root, "audits.json")
        try:
            stamps = tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in (manifest_path, audits_path))
        except FileNotFoundError:
            return metadata, {}, {}, None, None
        with self._lock:
            if not fresh and self._cache and self._cache[0] == stamps:
                return metadata, *self._cache[1:]
        try:
            raw_manifest = manifest_path.read_bytes()
            raw_audits = audits_path.read_bytes()
            audits = json.loads(raw_audits)
            if (not isinstance(audits, dict) or audits.get("schema") != "tulane_simulation_audits:v1"
                    or not isinstance(audits.get("groups"), list)):
                raise ValueError("Unsupported simulation audit schema")
            records = {}
            for line in raw_manifest.splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError("Incomplete simulation manifest")
                image_id = item.get("image_id")
                if (not isinstance(image_id, str) or not _ID.fullmatch(image_id) or image_id in records
                        or item.get("source") != "carlane_tulane" or item.get("synthetic") is not True
                        or item.get("pool") != "commercial" or item.get("split") not in ("train", "val")
                        or not isinstance(item.get("session_id"), str) or not item["session_id"]
                        or not isinstance(item.get("group_key"), str) or not item["group_key"]
                        or not isinstance(item.get("qc_reasons"), list)
                        or any(not isinstance(reason, str) for reason in item["qc_reasons"])
                        or any(not _sha(item.get(key)) for key in
                               ("source_sha256", "geometry_sha256", "source_label_sha256"))):
                    raise ValueError("Invalid simulation manifest record")
                # Validate path syntax now; resolve selected files when requested.
                for name in ("image", "geometry", "source_label"):
                    _relative(item.get(name))
                records[image_id] = item
            groups, members, sample_ids = {}, Counter(), set()
            for group in audits["groups"]:
                if (not isinstance(group, dict) or not isinstance(group.get("group_key"), str)
                        or group["group_key"] in groups or group.get("split") not in ("train", "val")
                        or type(group.get("count")) is not int or group["count"] < 1
                        or not isinstance(group.get("sample_ids"), list)
                        or len(group["sample_ids"]) != min(3, group["count"])):
                    raise ValueError("Invalid simulation audit group")
                for image_id in group["sample_ids"]:
                    if image_id in sample_ids or image_id not in records:
                        raise ValueError("Invalid simulation audit sample membership")
                    item = records[image_id]
                    if item["group_key"] != group["group_key"] or item["split"] != group["split"]:
                        raise ValueError("Simulation audit sample crosses cohorts or splits")
                    sample_ids.add(image_id)
                groups[group["group_key"]] = group
            for item in records.values():
                group = groups.get(item["group_key"])
                if not group or group["split"] != item["split"]:
                    raise ValueError("Simulation manifest has no matching audit group")
                members[item["group_key"]] += 1
            if any(members[key] != group["count"] for key, group in groups.items()):
                raise ValueError("Simulation audit cohort counts differ from the manifest")
            manifest_sha, audits_sha = _digest(raw_manifest), _digest(raw_audits)
            if audits.get("manifest_sha256") not in (None, manifest_sha):
                raise ValueError("Simulation audits refer to another manifest")
        except HTTPException:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(409, "Simulation manifest or audits failed integrity checks") from None
        with self._lock:
            self._cache = (stamps, records, groups, manifest_sha, audits_sha)
        return metadata, records, groups, manifest_sha, audits_sha

    @staticmethod
    def fingerprint(item, manifest_sha, audits_sha):
        return _digest(json.dumps({"image_id": item["image_id"], "group_key": item["group_key"],
                                   "manifest_sha256": manifest_sha, "audits_sha256": audits_sha,
                                   **{key: item[key] for key in
                                      ("source_sha256", "geometry_sha256", "source_label_sha256")}},
                                  sort_keys=True, separators=(",", ":")).encode())

    def _verified(self, item, *, geometry=False):
        root = self._root()
        try:
            image = _inside(root, item["image"])
            if image.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                raise ValueError
            if _digest(image.read_bytes()) != item["source_sha256"]:
                raise ValueError
            path = _inside(root, item["geometry"])
            raw = path.read_bytes()
            if _digest(raw) != item["geometry_sha256"]:
                raise ValueError
            if _digest(_inside(root, item["source_label"]).read_bytes()) != item["source_label_sha256"]:
                raise ValueError
            value = json.loads(raw)
            if (not isinstance(value, dict) or value.get("width") != 1280 or value.get("height") != 720
                    or not isinstance(value.get("lanes"), list)):
                raise ValueError
            slots = set()
            for lane in value["lanes"]:
                slot, points = lane.get("slot"), lane.get("points")
                if type(slot) is not int or slot not in (1, 2, 3, 4) or slot in slots or not isinstance(points, list) or len(points) < 2:
                    raise ValueError
                slots.add(slot)
                for point in points:
                    if (not isinstance(point, list) or len(point) < 2
                            or any(type(v) not in (int, float) or not math.isfinite(v) for v in point[:2])
                            or not 0 <= point[0] < 1280 or not 0 <= point[1] < 720):
                        raise ValueError
            return value if geometry else image
        except HTTPException:
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(409, "The simulation image or labels changed; reimport or repair this collection") from None

    def decisions(self):
        path = _inside(self._root(), "review.sqlite")
        if not path.exists():
            return {}
        try:
            con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                con.execute("PRAGMA query_only=ON")
                if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='decisions'").fetchone():
                    return {}
                return {row["image_id"]: dict(row) for row in con.execute("SELECT * FROM decisions")}
            finally:
                con.close()
        except sqlite3.OperationalError:
            raise HTTPException(409, "Simulation decisions are initializing; retry shortly") from None

    def corrections(self):
        """Read human overlays without changing the checksummed import."""
        path = _inside(self._root(), "review.sqlite")
        if not path.exists():
            return {}
        try:
            con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                con.execute("PRAGMA query_only=ON")
                if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='corrections'").fetchone():
                    return {}
                return {row["image_id"]: dict(row) for row in con.execute("SELECT * FROM corrections")}
            finally:
                con.close()
        except sqlite3.OperationalError:
            raise HTTPException(409, "Audit corrections are initializing; retry shortly") from None

    @staticmethod
    def _validate_lanes(lanes, width, height, *, source_metadata=False):
        if not isinstance(lanes, list) or len(lanes) > 4:
            raise HTTPException(422, "Use at most four painted lane boundaries")
        slots = set()
        cleaned = []
        for lane in lanes:
            if not isinstance(lane, dict) or type(lane.get("slot")) is not int or lane["slot"] not in (1, 2, 3, 4):
                raise HTTPException(422, "Each boundary needs a valid lane slot")
            if lane["slot"] in slots:
                raise HTTPException(422, "Lane slots must be unique")
            slots.add(lane["slot"])
            points = lane.get("points")
            if not isinstance(points, list) or not 2 <= len(points) <= 2048:
                raise HTTPException(422, "Each visible boundary needs between two and 2048 points")
            normalized = []
            for point in points:
                if source_metadata and isinstance(point, (list, tuple)):
                    point = point[:2]
                if (not isinstance(point, (list, tuple)) or len(point) != 2
                        or any(type(value) not in (int, float) or not math.isfinite(value) for value in point)
                        or not 0 <= point[0] < width or not 0 <= point[1] < height):
                    raise HTTPException(422, "Lane points must stay inside the photo")
                normalized.append([point[0], point[1]])
            result = {"slot": lane["slot"], "points": normalized}
            if "segments" in lane:
                segments = lane["segments"]
                if not isinstance(segments, list) or len(segments) > 2048:
                    raise HTTPException(422, "Visible boundary sections must be a list")
                point_set = {tuple(point) for point in normalized}
                sections = []
                total = 0
                for segment in segments:
                    if source_metadata and isinstance(segment, dict):
                        segment = segment.get("points")
                    if not isinstance(segment, list) or not 1 <= len(segment) <= 2048:
                        raise HTTPException(422, "Each visible section needs at least one lane point")
                    section = []
                    for point in segment:
                        if source_metadata and isinstance(point, (list, tuple)):
                            point = point[:2]
                        if (not isinstance(point, (list, tuple)) or len(point) != 2
                                or any(type(value) not in (int, float) or not math.isfinite(value) for value in point)
                                or tuple(point) not in point_set):
                            raise HTTPException(422, "Visible sections must use points from their lane boundary")
                        section.append([point[0], point[1]])
                    total += len(section)
                    if total > 2048:
                        raise HTTPException(422, "Use at most 2048 visible section points per boundary")
                    sections.append(section)
                result["segments"] = sections
            cleaned.append(result)
        return cleaned

    @staticmethod
    def _same_lane_geometry(source, submitted):
        # Imported points may carry visibility flags and sections may carry
        # provenance metadata. A browser editing XY coordinates must not strip
        # those fields from an untouched lane.
        if [point[:2] for point in source["points"]] != submitted["points"]:
            return False
        if "segments" not in submitted:
            return True
        if "segments" not in source:
            return False
        sections = [segment if isinstance(segment, list) else segment["points"]
                    for segment in source["segments"]]
        return [[point[:2] for point in segment] for segment in sections] == submitted["segments"]

    def _active_correction(self, item, manifest_sha, audits_sha, corrections=None):
        correction = (corrections if corrections is not None else self.corrections()).get(item["image_id"])
        if correction and correction["source_fingerprint"] == self.fingerprint(item, manifest_sha, audits_sha):
            return correction
        return None

    def _repairable_qc_reasons(self, item):
        return []

    def _complete_annotation_qc_reasons(self, item):
        return []

    def _validate_warning_resolution(self, geometry, resolved):
        self._validate_lanes(geometry["lanes"], geometry["width"], geometry["height"], source_metadata=True)

    def _warning_info(self, item, correction_geometry=None):
        repairable = self._repairable_qc_reasons(item)
        complete_reasons = self._complete_annotation_qc_reasons(item)
        resolved = correction_geometry.get("audit_resolved_qc_reasons", []) if correction_geometry else []
        if (not isinstance(resolved, list) or any(not isinstance(reason, str) for reason in resolved)
                or not set(resolved).issubset(set(repairable) | set(complete_reasons))):
            raise HTTPException(409, "Saved warning resolution is inconsistent with this source")
        resolved = sorted(set(resolved))
        if resolved:
            try:
                self._validate_warning_resolution(correction_geometry, resolved)
            except HTTPException:
                raise HTTPException(409, "Saved correction no longer satisfies its warning resolution") from None
        return {"source_qc_reasons": item["qc_reasons"], "resolved_qc_reasons": resolved,
                "repairable_qc_reasons": repairable,
                "complete_annotation_qc_reasons": complete_reasons,
                "human_complete_annotation": bool(correction_geometry and
                    correction_geometry.get("human_complete_annotation") is True),
                "no_visible_lane_paint": bool(correction_geometry and
                    correction_geometry.get("no_visible_lane_paint") is True),
                "qc_reasons": [reason for reason in item["qc_reasons"] if reason not in resolved]}

    def _effective_qc_reasons(self, item, manifest_sha, audits_sha, corrections=None):
        correction = self._active_correction(item, manifest_sha, audits_sha, corrections)
        if not item["qc_reasons"] or not correction:
            return item["qc_reasons"]
        try:
            geometry = self._correction_geometry(correction, self._verified(item, geometry=True))
            return self._warning_info(item, geometry)["qc_reasons"]
        except HTTPException:
            return item["qc_reasons"]

    def _correction_geometry(self, correction, source_geometry):
        try:
            path = _inside(self._root(), correction["geometry_path"])
            raw = path.read_bytes()
            if _digest(raw) != correction["geometry_sha256"]:
                raise ValueError
            value = json.loads(raw)
            if (not isinstance(value, dict) or value.get("image_id") != source_geometry.get("image_id")
                    or value.get("width") != source_geometry.get("width")
                    or value.get("height") != source_geometry.get("height")):
                raise ValueError
            self._validate_lanes(value.get("lanes"), value["width"], value["height"], source_metadata=True)
            return value
        except (OSError, ValueError, KeyError, TypeError):
            raise HTTPException(409, "Saved audit correction changed; repair the correction") from None

    def effective_fingerprint(self, item, manifest_sha, audits_sha, corrections=None):
        source = self.fingerprint(item, manifest_sha, audits_sha)
        correction = self._active_correction(item, manifest_sha, audits_sha, corrections)
        return _digest((source + correction["geometry_sha256"]).encode()) if correction else source

    def _eligible_record(self, item, manifest_sha, audits_sha, corrections=None):
        source_geometry = self._verified(item, geometry=True)
        correction = self._active_correction(item, manifest_sha, audits_sha, corrections)
        result = dict(item)
        if correction:
            geometry = self._correction_geometry(correction, source_geometry)
            result.update(original_geometry=item["geometry"],
                          original_geometry_sha256=item["geometry_sha256"],
                          geometry=correction["geometry_path"],
                          geometry_sha256=correction["geometry_sha256"],
                          geometry_origin="human_audit_correction",
                          correction_reviewer=correction["reviewer"])
            result.update(self._warning_info(item, geometry))
        result["fingerprint"] = self.effective_fingerprint(item, manifest_sha, audits_sha, corrections)
        return result

    def _groups(self, records, groups, manifest_sha, audits_sha, decisions, *, verify_samples=False):
        result = []
        corrections = self.corrections() if groups else {}
        qc_counts = Counter(item["group_key"] for item in records.values()
                            if self._effective_qc_reasons(item, manifest_sha, audits_sha, corrections))
        for key, group in sorted(groups.items()):
            states = []
            for image_id in group["sample_ids"]:
                decision = decisions.get(image_id)
                if decision and decision["fingerprint"] == self.effective_fingerprint(records[image_id], manifest_sha, audits_sha, corrections):
                    action = decision["action"]
                    if verify_samples and action == "approve":
                        try:
                            source_geometry = self._verified(records[image_id], geometry=True)
                            correction = self._active_correction(records[image_id], manifest_sha, audits_sha, corrections)
                            if correction:
                                self._correction_geometry(correction, source_geometry)
                        except HTTPException:
                            action = "stale"
                else:
                    action = "pending"
                states.append(action)
            qc_members = qc_counts[key]
            state = "blocked" if "exclude" in states else "eligible" if states and all(s == "approve" for s in states) else "awaiting_audit"
            result.append({"group_key": key, "split": group["split"], "images": group["count"],
                           "sample_ids": group["sample_ids"], "samples": len(states),
                           "approved": states.count("approve"), "skipped": states.count("skip"),
                           "excluded": states.count("exclude"), "pending": states.count("pending") + states.count("stale"),
                           "qc_flagged_images": qc_members, "state": state,
                           "eligible_train_images": group["count"] - qc_members if state == "eligible" and group["split"] == "train" else 0})
        return result

    def status(self):
        metadata, records, groups, manifest_sha, audits_sha = self.snapshot()
        decisions = self.decisions() if groups else {}
        audit_groups = self._groups(records, groups, manifest_sha, audits_sha, decisions, verify_samples=True)
        sample_ids = {image_id for g in groups.values() for image_id in g["sample_ids"]}
        counts = Counter({"imported": len(records), "train": 0, "val": 0, "samples": len(sample_ids),
                          "approved": 0, "excluded": 0, "skipped": 0, "pending": 0,
                          "groups": len(groups), "groups_eligible": 0, "groups_blocked": 0,
                          "eligible_train_candidates": 0})
        counts.update(item["split"] for item in records.values())
        for group in audit_groups:
            for key in ("approved", "excluded", "skipped", "pending"):
                counts[key] += group[key]
            counts["groups_eligible"] += group["state"] == "eligible"
            counts["groups_blocked"] += group["state"] == "blocked"
            counts["eligible_train_candidates"] += group["eligible_train_images"]
        return {"schema": "tulane_simulation_review:v1", "source": "carlane_tulane", "domain": "synthetic",
                "license": "Apache-2.0", "metadata": metadata, "audits_ready": bool(groups),
                "counts": dict(counts), "groups": audit_groups,
                "manifest_sha256": manifest_sha, "audits_sha256": audits_sha,
                "auto_promote": False, "original_reviews_unchanged": True,
                "policy": "Approve three samples per cohort after checking visible longitudinal paint, slots and occlusion. Simulator projections can include invisible road geometry. An exclusion blocks its cohort; a skip holds it. QC-flagged images remain excluded. Eligible photos require an explicit experiment build; the converted base and real validation/test sets stay unchanged."}

    def detail(self, image_id, *, snapshot=None):
        image_id = _id(image_id)
        _, records, groups, manifest_sha, audits_sha = snapshot or self.snapshot()
        item = records.get(image_id)
        if not item or not any(image_id in group["sample_ids"] for group in groups.values()):
            raise HTTPException(404, "This photo is not a simulation audit sample")
        source_geometry = self._verified(item, geometry=True)
        correction = self._active_correction(item, manifest_sha, audits_sha)
        geometry = self._correction_geometry(correction, source_geometry) if correction else source_geometry
        fingerprint = self.effective_fingerprint(item, manifest_sha, audits_sha,
                                                 {image_id: correction} if correction else {})
        decision = self.decisions().get(image_id)
        effective = decision if decision and decision["fingerprint"] == fingerprint else None
        return {"image_id": image_id, "split": item["split"], "session_id": item["session_id"],
                "source": "carlane_tulane", "synthetic": True, "label_origin": "simulator_projected_gt",
                "group_key": item["group_key"], "group_images": groups[item["group_key"]]["count"],
                **self._warning_info(item, geometry if correction else None),
                "annotation_policy_risks": item.get("annotation_policy_risks", []),
                "geometry": geometry, "source_geometry": source_geometry,
                "correction": {"reviewer": correction["reviewer"],
                               "geometry_sha256": correction["geometry_sha256"],
                               "updated_at": correction["updated_at"]} if correction else None,
                "image_url": API + "/samples/" + image_id + "/photo",
                "fingerprint": fingerprint, "source_fingerprint": self.fingerprint(item, manifest_sha, audits_sha),
                "manifest_sha256": manifest_sha, "audits_sha256": audits_sha,
                **{key: item[key] for key in ("source_sha256", "geometry_sha256", "source_label_sha256")},
                "decision": effective, "previous_decision_stale": bool(decision and not effective),
                "revision": decision["revision"] if decision else 0,
                "original_reviews_unchanged": True, "auto_promote": False}

    def queue(self, split="all", status="pending", limit=1, after=""):
        if split not in ("all", "train", "val") or status not in ("pending", "all", "approve", "exclude", "skip"):
            raise HTTPException(400, "Invalid simulation audit filter")
        if type(limit) is not int or not 1 <= limit <= 12:
            raise HTTPException(422, "Request between one and twelve samples")
        if after:
            _id(after)
        snapshot = self.snapshot()
        _, records, groups, manifest_sha, audits_sha = snapshot
        decisions = self.decisions() if groups else {}
        ids = sorted({image_id for group in groups.values() for image_id in group["sample_ids"]})
        selected = []
        for image_id in ids:
            item = records[image_id]
            if image_id <= after or split != "all" and item["split"] != split:
                continue
            decision = decisions.get(image_id)
            action = decision["action"] if decision and decision["fingerprint"] == self.effective_fingerprint(item, manifest_sha, audits_sha) else None
            if status == "pending" and action or status not in ("pending", "all") and action != status:
                continue
            selected.append(self.detail(image_id, snapshot=snapshot))
            if len(selected) >= limit:
                break
        return {"items": selected, "split": split, "status": status,
                "next_after": selected[-1]["image_id"] if selected else None,
                "audits_ready": bool(groups), "auto_promote": False}

    def photo(self, image_id):
        image_id = _id(image_id)
        _, records, groups, _, _ = self.snapshot()
        if image_id not in records or not any(image_id in group["sample_ids"] for group in groups.values()):
            raise HTTPException(404, "Simulation audit photo not found")
        return self._verified(records[image_id])

    def correct(self, body: SimulationCorrection, reviewer: str):
        """Save a separate human overlay, optionally approving it in the same transaction."""
        if not _sha(body.fingerprint):
            raise HTTPException(400, "Invalid sample fingerprint")
        if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 80:
            raise HTTPException(400, "A reviewer name is required")
        current = self.detail(body.image_id, snapshot=self.snapshot(fresh=True))
        cleaned = self._validate_lanes(body.lanes, current["geometry"]["width"], current["geometry"]["height"])
        if current["revision"] != body.revision or current["fingerprint"] != body.fingerprint:
            raise HTTPException(409, "Another reviewer changed this sample; reload it")
        resolved = sorted(set(body.resolved_qc_reasons if "resolved_qc_reasons" in body.model_fields_set
                              else current["resolved_qc_reasons"]))
        if not set(resolved).issubset(set(current["repairable_qc_reasons"]) | set(current["complete_annotation_qc_reasons"])):
            raise HTTPException(422, "Only this sample's repairable geometry warnings can be resolved")
        old = {lane["slot"]: lane for lane in current["geometry"]["lanes"]}
        for lane in cleaned:
            previous = old.get(lane["slot"])
            if (previous and "segments" in previous and "segments" not in lane
                    and not self._same_lane_geometry(previous, lane)):
                raise HTTPException(422, "Keep the visible sections when editing a segmented boundary")
        lanes = [copy.deepcopy(old[lane["slot"]]) if lane["slot"] in old
                 and self._same_lane_geometry(old[lane["slot"]], lane) else lane for lane in cleaned]
        unchanged = lanes == current["geometry"]["lanes"]
        complete = body.complete_annotation if "complete_annotation" in body.model_fields_set else (
            current["human_complete_annotation"] if unchanged else False)
        no_paint = body.no_visible_lane_paint if "no_visible_lane_paint" in body.model_fields_set else (
            current["no_visible_lane_paint"] if unchanged else False)
        needs_complete = bool(set(resolved) & set(current["complete_annotation_qc_reasons"]))
        if needs_complete and not complete:
            raise HTTPException(422, "Confirm that you checked and annotated every visible longitudinal boundary in this entire photo")
        if complete and not needs_complete:
            raise HTTPException(422, "Full-photo annotation must resolve this photo's missing or malformed annotation warnings")
        if (complete and no_paint != (not lanes)) or (no_paint and not complete):
            raise HTTPException(422, "An empty full-photo annotation requires explicit confirmation that no lane paint is visible")
        if (unchanged and resolved == current["resolved_qc_reasons"]
                and complete == current["human_complete_annotation"] and no_paint == current["no_visible_lane_paint"]
                and not body.approve):
            return current
        geometry = copy.deepcopy(current["geometry"])
        geometry.update(lanes=lanes, origin="human_audit_correction", version="audit-correction:v1")
        if resolved or "audit_resolved_qc_reasons" in geometry:
            geometry["audit_resolved_qc_reasons"] = resolved
        if complete or "human_complete_annotation" in geometry:
            geometry.update(human_complete_annotation=complete, no_visible_lane_paint=no_paint)
        if complete:
            geometry["annotation_scope"] = "single_photo_manual_replacement"
        elif geometry.get("annotation_scope") == "single_photo_manual_replacement":
            geometry.pop("annotation_scope")
        if resolved:
            self._validate_warning_resolution(geometry, resolved)
        raw = (json.dumps(geometry, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        geometry_sha = _digest(raw)
        relative = f"review_corrections/{body.image_id}-{geometry_sha}.json"
        path = _inside(self._root(), relative)
        db_path = _inside(self._root(), "review.sqlite")
        con = sqlite3.connect(db_path, timeout=10)
        try:
            con.execute("""CREATE TABLE IF NOT EXISTS corrections(
                image_id TEXT PRIMARY KEY, source_fingerprint TEXT NOT NULL,
                geometry_path TEXT NOT NULL, geometry_sha256 TEXT NOT NULL,
                reviewer TEXT NOT NULL, updated_at REAL NOT NULL)""")
            if body.approve:
                self._decision_schema(con)
            con.execute("BEGIN IMMEDIATE")
            latest = self.detail(body.image_id, snapshot=self.snapshot(fresh=True))
            if latest["revision"] != body.revision or latest["fingerprint"] != body.fingerprint:
                raise HTTPException(409, "Another reviewer changed this sample; reload it")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if path.read_bytes() != raw:
                    raise HTTPException(409, "Saved correction file conflicts with this sample")
            else:
                temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
                try:
                    temporary.write_bytes(raw)
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            _, records, _, manifest_sha, audits_sha = self.snapshot(fresh=True)
            source_fingerprint = self.fingerprint(records[body.image_id], manifest_sha, audits_sha)
            if source_fingerprint != current["source_fingerprint"]:
                raise HTTPException(409, "The source or audit changed; reload this sample")
            con.execute("""INSERT INTO corrections VALUES(?,?,?,?,?,?)
                ON CONFLICT(image_id) DO UPDATE SET
                source_fingerprint=excluded.source_fingerprint,
                geometry_path=excluded.geometry_path,
                geometry_sha256=excluded.geometry_sha256,
                reviewer=excluded.reviewer,updated_at=excluded.updated_at""",
                (body.image_id, source_fingerprint, relative, geometry_sha, reviewer, time.time()))
            if body.approve and not set(latest["source_qc_reasons"]).difference(resolved):
                approval = SimulationDecision(image_id=body.image_id, action="approve",
                    event_id="correction-" + uuid.uuid4().hex, revision=latest["revision"],
                    fingerprint=_digest((source_fingerprint + geometry_sha).encode()))
                self._record_decision(con, approval, reviewer, latest["revision"])
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return self.detail(body.image_id)

    @staticmethod
    def _decision_schema(con):
        con.executescript("""
            CREATE TABLE IF NOT EXISTS decisions(
                image_id TEXT PRIMARY KEY, action TEXT NOT NULL, reviewer TEXT NOT NULL,
                event_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
                revision INTEGER NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS decision_events(
                event_id TEXT PRIMARY KEY, image_id TEXT NOT NULL, action TEXT NOT NULL,
                reviewer TEXT NOT NULL, fingerprint TEXT NOT NULL,
                previous_revision INTEGER NOT NULL, revision INTEGER NOT NULL, created_at REAL NOT NULL);
        """)

    @staticmethod
    def _record_decision(con, body, reviewer, revision):
        now = time.time()
        con.execute("INSERT INTO decision_events VALUES(?,?,?,?,?,?,?,?)",
                    (body.event_id, body.image_id, body.action, reviewer, body.fingerprint, revision, revision + 1, now))
        con.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?) ON CONFLICT(image_id) DO UPDATE SET action=excluded.action,reviewer=excluded.reviewer,event_id=excluded.event_id,fingerprint=excluded.fingerprint,revision=excluded.revision,updated_at=excluded.updated_at",
                    (body.image_id, body.action, reviewer, body.event_id, body.fingerprint, revision + 1, now))

    def decide(self, body: SimulationDecision, reviewer: str):
        if body.action not in ("approve", "exclude", "skip") or not _EVENT.fullmatch(body.event_id) or not _sha(body.fingerprint):
            raise HTTPException(400, "Choose approve, exclude, or skip with a valid decision identifier")
        if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 80:
            raise HTTPException(400, "A reviewer name is required")
        current = self.detail(body.image_id, snapshot=self.snapshot(fresh=True))
        if current["fingerprint"] != body.fingerprint:
            raise HTTPException(409, "The simulation labels or audit changed; reload this photo")
        if body.action == "approve" and current["qc_reasons"]:
            raise HTTPException(422, "This sample has conversion warnings; exclude or skip its cohort until repaired")
        path = _inside(self._root(), "review.sqlite")
        con = sqlite3.connect(path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            self._decision_schema(con)
            con.execute("BEGIN IMMEDIATE")
            previous = con.execute("SELECT * FROM decision_events WHERE event_id=?", (body.event_id,)).fetchone()
            if previous:
                if tuple(previous[k] for k in ("image_id", "action", "reviewer", "fingerprint", "previous_revision")) != (
                        body.image_id, body.action, reviewer, body.fingerprint, body.revision):
                    raise HTTPException(409, "This decision identifier was already used")
                con.rollback()
                return {"image_id": body.image_id, "action": body.action, "revision": previous["revision"],
                        "event_id": body.event_id, "idempotent": True, "auto_promote": False}
            row = con.execute("SELECT revision FROM decisions WHERE image_id=?", (body.image_id,)).fetchone()
            revision = row["revision"] if row else 0
            if body.revision != revision:
                raise HTTPException(409, "Another reviewer changed this sample; reload it")
            # Re-read bytes while holding the transaction; stale browser state
            # cannot approve a replacement manifest or replacement source file.
            latest = self.detail(body.image_id, snapshot=self.snapshot(fresh=True))
            if latest["fingerprint"] != body.fingerprint:
                raise HTTPException(409, "The simulation labels or audit changed; reload this photo")
            self._record_decision(con, body, reviewer, revision)
            con.commit()
            return {"image_id": body.image_id, "action": body.action, "revision": revision + 1,
                    "event_id": body.event_id, "idempotent": False,
                    "original_reviews_unchanged": True, "auto_promote": False}
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def eligible_records(self):
        """Verify eligible TRAIN bytes for a later explicit collection builder.

        This method grants no training promotion and writes no artifact. A
        caller must still pin this result in its own immutable experiment.
        """
        _, records, groups, manifest_sha, audits_sha = self.snapshot(fresh=True)
        audited = self._groups(records, groups, manifest_sha, audits_sha, self.decisions(), verify_samples=True)
        eligible_groups = {g["group_key"] for g in audited if g["state"] == "eligible" and g["split"] == "train"}
        result = []
        corrections = self.corrections()
        for image_id, item in sorted(records.items()):
            if (item["group_key"] not in eligible_groups
                    or self._effective_qc_reasons(item, manifest_sha, audits_sha, corrections)):
                continue
            result.append({**self._eligible_record(item, manifest_sha, audits_sha, corrections),
                           "manifest_sha256": manifest_sha, "audits_sha256": audits_sha})
        return {"schema": "tulane_simulation_eligible:v1", "records": result,
                "manifest_sha256": manifest_sha, "audits_sha256": audits_sha,
                "train_images": len(result), "val_images_used": 0, "auto_promote": False}


def create_simulation_router(data_root: Path, require_session: Callable) -> APIRouter:
    router = APIRouter(prefix=API, tags=["isolated simulation audits"])
    store = SimulationReview(data_root)

    def authenticated(session=Depends(require_session)):
        if not isinstance(session, dict) or not session.get("reviewer"):
            raise HTTPException(401, "Pair this device before reviewing simulations")
        return session

    @router.get("")
    def status(session=Depends(authenticated)):
        return store.status()

    @router.get("/queue")
    def queue(split: str = "all", status: str = "pending", limit: int = Query(1, ge=1, le=12),
              after: str = "", session=Depends(authenticated)):
        return store.queue(split, status, limit, after)

    @router.get("/samples/{image_id}")
    def detail(image_id: str, session=Depends(authenticated)):
        return store.detail(image_id)

    @router.get("/samples/{image_id}/photo")
    def photo(image_id: str, session=Depends(authenticated)):
        return FileResponse(store.photo(image_id), headers={"Cache-Control": "private, no-store"})

    @router.post("/decision")
    def decision(body: SimulationDecision, session=Depends(authenticated)):
        return store.decide(body, session["reviewer"])

    @router.post("/correction")
    def correction(body: SimulationCorrection, session=Depends(authenticated)):
        return store.correct(body, session["reviewer"])

    return router
