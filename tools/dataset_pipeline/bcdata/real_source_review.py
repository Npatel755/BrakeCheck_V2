"""Isolated sample audits for licensed, real, forward-camera lane datasets.

Audit decisions live beside each import. Nothing here changes the production
registry, the cleared base, or an already frozen experiment.
"""
from __future__ import annotations

from collections import Counter
import io
import json
import math
from pathlib import Path
import re
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from PIL import Image

from .college_group_clearance import has_observed_training_span, valid_clearances, _hash as clearance_hash
from .simulation_review import (
    SimulationCorrection, SimulationDecision, SimulationReview, _digest, _inside, _relative, _sha,
)

API = "/api/studio/real-sources"
SOURCES = {
    "pandaset_plus": ("pandaset_plus", "CC-BY-4.0", "panda-"),
    "college_station_lane_marking": ("college_station_lane_marking", "CC0-1.0", "college-"),
}
_ID = re.compile(r"[a-z][a-z0-9_-]{4,79}\Z")
_SPLITS = {"train", "val", "test", "hold"}
_READY = {"awaiting_audit", "awaiting_audits", "audit_ready"}
_REPAIRABLE_COLLEGE_WARNINGS = {
    "potential_transverse_marking", "short_native_polyline",
    "ego_slot_center_ambiguous", "ego_slot_long_extrapolation",
    "native_polyline_not_single_valued", "native_points_outside_image",
}
_COMPLETE_ANNOTATION_COLLEGE_WARNINGS = {
    "native_coverage_incomplete", "invalid_native_number",
    "invalid_native_polyline", "degenerate_native_polyline",
}


class RealSourceReview(SimulationReview):
    def __init__(self, data_root: Path, source: str):
        if source not in SOURCES:
            raise HTTPException(404, "Unknown real lane dataset")
        super().__init__(data_root)
        self.source = source
        self.root = self.data_root / "experiments" / SOURCES[source][0]

    def _repairable_qc_reasons(self, item):
        if self.source != "college_station_lane_marking":
            return []
        return sorted(set(item["qc_reasons"]) & _REPAIRABLE_COLLEGE_WARNINGS)

    def _complete_annotation_qc_reasons(self, item):
        if self.source != "college_station_lane_marking":
            return []
        return sorted(set(item["qc_reasons"]) & _COMPLETE_ANNOTATION_COLLEGE_WARNINGS)

    def _validate_warning_resolution(self, geometry, resolved):
        super()._validate_warning_resolution(geometry, resolved)
        complete = bool(set(resolved) & _COMPLETE_ANNOTATION_COLLEGE_WARNINGS)
        if complete:
            if geometry.get("human_complete_annotation") is not True:
                raise HTTPException(422, "A full-photo human annotation confirmation is required")
            if (geometry.get("no_visible_lane_paint") is True) != (not geometry["lanes"]):
                raise HTTPException(422, "Confirm explicitly when this entire photo has no visible lane paint")
            if geometry.get("annotation_scope") != "single_photo_manual_replacement":
                raise HTTPException(422, "Complete annotations apply only to this manually reviewed photo")
        if "native_polyline_not_single_valued" in resolved or complete:
            for lane in geometry["lanes"]:
                ys = [point[1] for point in lane["points"]]
                if not (all(a < b for a, b in zip(ys, ys[1:]))
                        or all(a > b for a, b in zip(ys, ys[1:]))):
                    raise HTTPException(422, "Each corrected boundary must follow image rows in one direction, with one point per row")

    def _metadata(self):
        metadata, _ = self._json("import.json", missing=True)
        if metadata and (metadata.get("schema") != "real_lane_import:v1"
                         or metadata.get("source") != self.source
                         or metadata.get("license") != SOURCES[self.source][1]
                         or metadata.get("auto_promote") is True):
            raise HTTPException(409, "Real lane import provenance is inconsistent")
        return metadata

    def snapshot(self, *, fresh=False):
        metadata = self._metadata()
        if not metadata or metadata.get("state") not in _READY:
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
            manifest_sha, audits_sha = _digest(raw_manifest), _digest(raw_audits)
            if (metadata.get("manifest_sha256") not in (None, manifest_sha)
                    or metadata.get("audits_sha256") not in (None, audits_sha)):
                raise ValueError("Import snapshot hashes changed")
            audits = json.loads(raw_audits)
            if (not isinstance(audits, dict) or audits.get("schema") != "real_lane_audits:v1"
                    or not isinstance(audits.get("groups"), list)
                    or audits.get("manifest_sha256") not in (None, manifest_sha)):
                raise ValueError("Unknown audit schema or manifest")
            records = {}
            for line in raw_manifest.splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                image_id = item.get("image_id")
                if (not isinstance(image_id, str) or not _ID.fullmatch(image_id)
                        or not image_id.startswith(SOURCES[self.source][2]) or image_id in records
                        or item.get("source") != self.source or item.get("synthetic") is not False
                        or item.get("pool") != "commercial" or item.get("split") not in _SPLITS
                        or not isinstance(item.get("session_id"), str) or not item["session_id"]
                        or not isinstance(item.get("group_key"), str) or not item["group_key"]
                        or not isinstance(item.get("qc_reasons"), list)
                        or any(not isinstance(reason, str) for reason in item["qc_reasons"])
                        or any(not _sha(item.get(key)) for key in
                               ("source_sha256", "geometry_sha256", "source_label_sha256"))):
                    raise ValueError("Invalid real source manifest row")
                for key in ("image", "geometry", "source_label"):
                    _relative(item.get(key))
                records[image_id] = item
            if not records:
                raise ValueError("Empty complete import")
            groups, members, sample_ids = {}, Counter(), set()
            for group in audits["groups"]:
                key = group.get("group_key")
                samples = group.get("sample_ids")
                if (not isinstance(key, str) or not key or key in groups
                        or group.get("split") not in _SPLITS
                        or type(group.get("count")) is not int or group["count"] < 1
                        or not isinstance(samples, list)
                        or len(samples) != min(3, group["count"])):
                    raise ValueError("Invalid audit group")
                for image_id in samples:
                    if (image_id in sample_ids or image_id not in records
                            or records[image_id]["group_key"] != key
                            or records[image_id]["split"] != group["split"]):
                        raise ValueError("Audit sample crosses groups or splits")
                    sample_ids.add(image_id)
                groups[key] = group
            for item in records.values():
                group = groups.get(item["group_key"])
                if not group or group["split"] != item["split"]:
                    raise ValueError("Unaudited real source group")
                members[item["group_key"]] += 1
            if any(members[key] != group["count"] for key, group in groups.items()):
                raise ValueError("Audit group count differs from manifest")
        except HTTPException:
            raise
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise HTTPException(409, "Real source import or audits failed integrity checks") from None
        with self._lock:
            self._cache = (stamps, records, groups, manifest_sha, audits_sha)
        return metadata, records, groups, manifest_sha, audits_sha

    def _verified(self, item, *, geometry=False):
        root = self._root()
        try:
            image = _inside(root, item["image"])
            if image.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                raise ValueError("Unexpected image suffix")
            raw_image = image.read_bytes()
            if len(raw_image) > 25 * 1024 * 1024 or _digest(raw_image) != item["source_sha256"]:
                raise ValueError("Image bytes changed")
            with Image.open(io.BytesIO(raw_image)) as raster:
                dimensions = raster.size
                raster.load()
            raw_geometry = _inside(root, item["geometry"]).read_bytes()
            if _digest(raw_geometry) != item["geometry_sha256"]:
                raise ValueError("Geometry changed")
            if _digest(_inside(root, item["source_label"]).read_bytes()) != item["source_label_sha256"]:
                raise ValueError("Native label changed")
            value = json.loads(raw_geometry)
            width, height = value["width"], value["height"]
            if (type(width) is not int or type(height) is not int or (width, height) != dimensions
                    or width <= 0 or height <= 0 or not isinstance(value.get("lanes"), list)):
                raise ValueError("Geometry dimensions differ from image")
            slots = set()
            for lane in value["lanes"]:
                slot, points = lane.get("slot"), lane.get("points")
                if (type(slot) is not int or slot not in (1, 2, 3, 4) or slot in slots
                        or not isinstance(points, list) or len(points) < 2):
                    raise ValueError("Invalid lane slot or points")
                slots.add(slot)
                for point in points:
                    if (not isinstance(point, list) or len(point) < 2
                            or any(type(v) not in (int, float) or not math.isfinite(v) for v in point[:2])
                            or not 0 <= point[0] < width or not 0 <= point[1] < height):
                        raise ValueError("Lane point leaves the image")
            return value if geometry else image
        except HTTPException:
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(409, "Source photo or labels changed; repair this import") from None

    def status(self):
        metadata, records, groups, manifest_sha, audits_sha = self.snapshot()
        progress = {}
        for name in ("import_status.json", "status.json"):
            value, _ = self._json(name, missing=True)
            if value:
                progress = value
                break
        decisions = self.decisions() if groups else {}
        audit_groups = self._groups(records, groups, manifest_sha, audits_sha, decisions, verify_samples=True)
        if self.source == "pandaset_plus":
            # PandaSet+ native polylines have no visible-paint or boundary-type
            # semantics. A sample audit can triage a sequence, but it cannot
            # turn every projected curve in that sequence into paint truth.
            verified = Counter(item["group_key"] for item in records.values()
                               if item["split"] == "train" and not item["qc_reasons"]
                               and item.get("paint_semantics_verified") is True)
            for group in audit_groups:
                group["eligible_train_images"] = (verified[group["group_key"]]
                                                  if group["state"] == "eligible" else 0)
        counts = Counter({"imported": len(records), "samples": sum(len(g["sample_ids"]) for g in groups.values()),
                          "approved": 0, "excluded": 0, "skipped": 0, "pending": 0,
                          "groups": len(groups), "groups_eligible": 0, "groups_blocked": 0,
                          "eligible_train_candidates": 0, "eligible_group_train_candidates": 0,
                          "individually_cleared_held": 0, "group_cleared_held": 0,
                          "held_uncleared": 0})
        counts.update(item["split"] for item in records.values())
        for group in audit_groups:
            for key in ("approved", "excluded", "skipped", "pending"):
                counts[key] += group[key]
            counts["groups_eligible"] += group["state"] == "eligible"
            counts["groups_blocked"] += group["state"] == "blocked"
            counts["eligible_group_train_candidates"] += group["eligible_train_images"]
        corrections = self.corrections()
        clearances = valid_clearances(self, records, groups, manifest_sha, audits_sha,
                                      decisions, corrections)
        for item in records.values():
            if self._individual_held_eligible(item, manifest_sha, audits_sha, decisions, corrections):
                counts["individually_cleared_held"] += 1
            elif self._group_held_eligible(item, clearances, decisions, corrections):
                counts["group_cleared_held"] += 1
        counts["eligible_train_candidates"] = (counts["eligible_group_train_candidates"]
                                               + counts["individually_cleared_held"]
                                               + counts["group_cleared_held"])
        counts["held_uncleared"] = (counts["hold"] - counts["individually_cleared_held"]
                                    - counts["group_cleared_held"])
        return {"schema": "real_lane_review:v1", "source": self.source,
                "license": SOURCES[self.source][1], "metadata": metadata, "progress": progress,
                "audits_ready": bool(groups),
                "counts": dict(counts), "groups": audit_groups,
                "manifest_sha256": manifest_sha, "audits_sha256": audits_sha,
                "auto_promote": False, "original_reviews_unchanged": True,
                "policy": "Approve two or three examples per group only when converted lines match visible longitudinal lane paint. Road edges and hidden or projected lines are separate. A skip holds the group; an exclusion blocks it. PandaSet+ also needs per-image paint verification before a candidate can enter training."}

    def detail(self, image_id, *, snapshot=None):
        if not isinstance(image_id, str) or not _ID.fullmatch(image_id) or not image_id.startswith(SOURCES[self.source][2]):
            raise HTTPException(400, "Invalid real source photo identifier")
        _, records, groups, manifest_sha, audits_sha = snapshot or self.snapshot()
        item = records.get(image_id)
        if not item or not any(image_id in group["sample_ids"] for group in groups.values()):
            raise HTTPException(404, "Photo is not a real source audit sample")
        source_geometry = self._verified(item, geometry=True)
        correction = self._active_correction(item, manifest_sha, audits_sha)
        value = self._correction_geometry(correction, source_geometry) if correction else source_geometry
        fingerprint = self.effective_fingerprint(item, manifest_sha, audits_sha,
                                                 {image_id: correction} if correction else {})
        decision = self.decisions().get(image_id)
        effective = decision if decision and decision["fingerprint"] == fingerprint else None
        return {"image_id": image_id, "split": item["split"], "intended_split": item.get("intended_split"),
                "session_id": item["session_id"],
                "source": self.source, "synthetic": False, "label_origin": item.get("label_origin", "native_source"),
                "group_key": item["group_key"], "group_images": groups[item["group_key"]]["count"],
                **self._warning_info(item, value if correction else None),
                "annotation_policy_risks": item.get("annotation_policy_risks", []),
                "geometry": value, "source_geometry": source_geometry,
                "correction": {"reviewer": correction["reviewer"],
                               "geometry_sha256": correction["geometry_sha256"],
                               "updated_at": correction["updated_at"]} if correction else None,
                "image_url": API + "/" + self.source + "/samples/" + image_id + "/photo",
                "fingerprint": fingerprint, "source_fingerprint": self.fingerprint(item, manifest_sha, audits_sha),
                "manifest_sha256": manifest_sha, "audits_sha256": audits_sha,
                **{key: item[key] for key in ("source_sha256", "geometry_sha256", "source_label_sha256")},
                "decision": effective, "previous_decision_stale": bool(decision and not effective),
                "revision": decision["revision"] if decision else 0,
                "original_reviews_unchanged": True, "auto_promote": False}

    def queue(self, split="all", status="pending", limit=1, after=""):
        if split not in _SPLITS | {"all"} or status not in {"pending", "all", "approve", "exclude", "skip"}:
            raise HTTPException(400, "Invalid audit filter")
        if not 1 <= limit <= 12:
            raise HTTPException(422, "Request between one and twelve samples")
        if after and (not _ID.fullmatch(after) or not after.startswith(SOURCES[self.source][2])):
            raise HTTPException(400, "Invalid cursor")
        snapshot = self.snapshot()
        _, records, groups, manifest_sha, audits_sha = snapshot
        decisions = self.decisions() if groups else {}
        selected = []
        split_order = {"train": 0, "val": 1, "test": 2, "hold": 3}
        ordered = sorted({image_id for g in groups.values() for image_id in g["sample_ids"]},
                         key=lambda image_id: (split_order[records[image_id]["split"]],
                                               records[image_id]["group_key"], image_id))
        if after and after not in ordered:
            raise HTTPException(404, "Audit cursor is not a sample")
        start = ordered.index(after) + 1 if after else 0
        for image_id in ordered[start:]:
            item = records[image_id]
            if split != "all" and item["split"] != split:
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
        _, records, groups, _, _ = self.snapshot()
        if image_id not in records or not any(image_id in g["sample_ids"] for g in groups.values()):
            raise HTTPException(404, "Real source audit photo not found")
        return self._verified(records[image_id])

    def eligible_records(self):
        """Verified TRAIN inventory for an explicitly requested converted merge.

        An approved corrected held photo can stand on its own. A separate sealed
        proof may reuse unchanged sample warning confirmations in their exact
        cohort. Original split/warnings remain in provenance in either case.
        """
        _, records, groups, manifest_sha, audits_sha = self.snapshot(fresh=True)
        decisions = self.decisions()
        audited = self._groups(records, groups, manifest_sha, audits_sha,
                               decisions, verify_samples=True)
        approved_groups = {group["group_key"] for group in audited
                           if group["state"] == "eligible" and group["split"] == "train"}
        result = []
        corrections = self.corrections()
        clearances = valid_clearances(self, records, groups, manifest_sha, audits_sha,
                                      decisions, corrections)
        for image_id, item in sorted(records.items()):
            decision = decisions.get(image_id)
            individual = self._individual_held_eligible(item, manifest_sha, audits_sha, decisions, corrections)
            group_clearance = not individual and self._group_held_eligible(item, clearances, decisions, corrections)
            if not individual and not group_clearance and (item["group_key"] not in approved_groups
                                  or self._effective_qc_reasons(item, manifest_sha, audits_sha, corrections)):
                continue
            if self.source == "pandaset_plus" and item.get("paint_semantics_verified") is not True:
                continue
            eligible = self._eligible_record(item, manifest_sha, audits_sha, corrections)
            if individual:
                eligible.update(split="train", original_split="hold", clearance={
                    "type": "individual_human_approval", "decision": dict(decision),
                    "source_split": "hold", "intended_split": "train"})
            elif group_clearance:
                proof = clearances[item["group_key"]]
                eligible.update(split="train", original_split="hold",
                    source_qc_reasons=list(item["qc_reasons"]), qc_reasons=[],
                    resolved_qc_reasons=list(item["qc_reasons"]), clearance={
                        "type": "sampled_unchanged_warning_cohort", "group_key": item["group_key"],
                        "proof_sha256": clearance_hash(proof),
                        "sample_decisions": proof["sample_decisions"],
                        "resolved_qc_reasons": proof["resolved_qc_reasons"],
                        "geometry_unchanged": True, "human_decisions_created": 0,
                        "source_split": "hold", "intended_split": "train"})
            else:
                eligible.update(original_split=item["split"], clearance={
                    "type": "sampled_cohort", "group_key": item["group_key"],
                    "sample_decisions": [dict(decisions[sample]) for sample in groups[item["group_key"]]["sample_ids"]]})
            eligible.update(source_manifest_sha256=manifest_sha, source_audits_sha256=audits_sha)
            result.append(eligible)
        return {"schema": "real_lane_eligible:v1", "source": self.source,
                "records": result, "train_images": len(result), "val_images_used": 0,
                "test_images_used": 0, "manifest_sha256": manifest_sha,
                "audits_sha256": audits_sha, "auto_promote": False}

    def _group_held_eligible(self, item, clearances, decisions, corrections):
        """Reuse only confirmed ordinary warnings, never another photo's edits."""
        proof = clearances.get(item["group_key"])
        if (not proof or self.source != "college_station_lane_marking"
                or item["split"] != "hold" or item.get("intended_split") != "train"
                or not item["qc_reasons"]
                or not set(item["qc_reasons"]).issubset(proof["resolved_qc_reasons"])):
            return False
        # Samples with an approved correction use the individual path above.
        # Any other personal history takes precedence over cohort clearance.
        if item["image_id"] in corrections or item["image_id"] in decisions:
            return False
        try:
            raw = _inside(self._root(), item["geometry"]).read_bytes()
            if _digest(raw) != item["geometry_sha256"]:
                return False
            geometry = json.loads(raw)
            self._validate_lanes(geometry["lanes"], geometry["width"], geometry["height"],
                                 source_metadata=True)
            # A converter may omit a malformed native line entirely. Sample
            # confirmation of a warning cannot establish a no-paint negative
            # for every unsampled photograph in that cohort.
            return has_observed_training_span(geometry) and all(
                lane.get("physical", True) is True
                and lane.get("kind") not in {"road_edge", "curb", "pavement_edge"}
                for lane in geometry["lanes"])
        except (HTTPException, OSError, ValueError, KeyError, TypeError, AttributeError):
            return False

    def _individual_held_eligible(self, item, manifest_sha, audits_sha, decisions, corrections):
        """Count the same approved held overlays in status and converted export."""
        if (item["split"] != "hold" or item.get("intended_split") != "train"
                or self.source == "pandaset_plus" and item.get("paint_semantics_verified") is not True):
            return False
        correction = self._active_correction(item, manifest_sha, audits_sha, corrections)
        decision = decisions.get(item["image_id"])
        if (not correction or not decision or decision["action"] != "approve"
                or decision["fingerprint"] != self.effective_fingerprint(item, manifest_sha, audits_sha, corrections)):
            return False
        try:
            geometry = self._correction_geometry(correction, self._verified(item, geometry=True))
            return not self._warning_info(item, geometry)["qc_reasons"]
        except HTTPException:
            return False


def create_real_source_router(data_root: Path, require_session: Callable) -> APIRouter:
    router = APIRouter(prefix=API, tags=["isolated real lane audits"])
    stores = {source: RealSourceReview(data_root, source) for source in SOURCES}

    def authenticated(session=Depends(require_session)):
        if not isinstance(session, dict) or not session.get("reviewer"):
            raise HTTPException(401, "Pair this device before reviewing datasets")
        return session

    def get(source):
        if source not in stores:
            raise HTTPException(404, "Unknown real lane dataset")
        return stores[source]

    @router.get("")
    def all_status(session=Depends(authenticated)):
        return {"sources": {name: store.status() for name, store in stores.items()}}

    @router.get("/{source}")
    def status(source: str, session=Depends(authenticated)):
        return get(source).status()

    @router.get("/{source}/queue")
    def queue(source: str, split: str = "all", status: str = "pending",
              limit: int = Query(1, ge=1, le=12), after: str = "", session=Depends(authenticated)):
        return get(source).queue(split, status, limit, after)

    @router.get("/{source}/samples/{image_id}")
    def detail(source: str, image_id: str, session=Depends(authenticated)):
        return get(source).detail(image_id)

    @router.get("/{source}/samples/{image_id}/photo")
    def photo(source: str, image_id: str, session=Depends(authenticated)):
        return FileResponse(get(source).photo(image_id), headers={"Cache-Control": "private, no-store"})

    @router.get("/{source}/eligible")
    def eligible(source: str, session=Depends(authenticated)):
        return get(source).eligible_records()

    @router.post("/{source}/decision")
    def decision(source: str, body: SimulationDecision, session=Depends(authenticated)):
        return get(source).decide(body, session["reviewer"])

    @router.post("/{source}/correction")
    def correction(source: str, body: SimulationCorrection, session=Depends(authenticated)):
        return get(source).correct(body, session["reviewer"])

    return router
