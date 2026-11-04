"""Explicitly freeze approved College repairs for the converted collection.

This adapter is selected only by an explicit converted-build invocation. The
ordinary reviewer remains candidate-only. Original source records, candidates,
human votes and saved boundary derivatives are never written here.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from fastapi import HTTPException

from .college_group_clearance import (
    _current_event, _events, _hash, _physical_lanes, has_observed_training_span,
)
from .college_middle_boundary import INDEX, _index, apply_saved_connection
from .college_repair_review import COLLECTION, CollegeRepairReview, SOURCE
from .proposal_training_policy import training_geometry_hold
from .simulation_review import _digest, _inside

# Dispatch/storage key only: the actual source stays SOURCE so source-session
# split protection still applies to every repair addition.
REPAIR_SOURCE = "college_held_repairs"


def _observed_geometry(geometry):
    """Run automatic ambiguity checks on the retained paint, not inferred gaps."""
    result = deepcopy(geometry)
    for lane in result["lanes"]:
        if "observed_paint_segments" in lane:
            lane["segments"] = deepcopy(lane["observed_paint_segments"])
            lane["points"] = [point for section in lane["segments"]
                              for point in (section.get("points") if isinstance(section, dict) else section)]
    return result


class CollegeRepairTraining(CollegeRepairReview):
    """Read-only, provenance-aware input for an explicitly requested build."""

    def _training_view(self, item, manifest_sha, audits_sha, corrections):
        candidate = self._verified(item, geometry=True)
        correction = self._active_correction(item, manifest_sha, audits_sha, corrections)
        geometry = self._correction_geometry(correction, candidate) if correction else candidate
        # detail() intentionally only exposes audit samples. The saved overlay
        # verifier also supports the fully approved cohort's unsampled members.
        view = apply_saved_connection(self, item, {
            "geometry": geometry, "fingerprint": self.effective_fingerprint(
                item, manifest_sha, audits_sha, corrections),
            "manifest_sha256": manifest_sha, "audits_sha256": audits_sha,
        })
        return view, correction

    def _proof_current(self, item, clearance, records, groups, manifest_sha, audits_sha,
                       decisions, corrections, events, *, verify_geometry=True):
        """A target fingerprint alone cannot authorize its cohort's neighbors."""
        if item.get("audit_mode", "ready") != "ready":
            return False
        kind = clearance.get("type")
        if kind == "individual_human_approval":
            expected = [clearance.get("decision")]
            if (clearance.get("source_split") != "hold"
                    or clearance.get("intended_split") != "train"):
                return False
        elif kind == "sampled_repair_cohort":
            group = groups.get(item["group_key"])
            expected = clearance.get("sample_decisions")
            if (not group or clearance.get("group_key") != item["group_key"]
                    or not isinstance(expected, list)
                    or [decision.get("image_id") for decision in expected] != group["sample_ids"]):
                return False
        else:
            return False
        if not expected:
            return False
        for decision in expected:
            if not isinstance(decision, dict):
                return False
            image_id = decision.get("image_id")
            sample = records.get(image_id)
            if (not sample or sample.get("audit_mode", "ready") != "ready"
                    or decisions.get(image_id) != decision or decision.get("action") != "approve"
                    or decision.get("fingerprint") != self.effective_fingerprint(
                        sample, manifest_sha, audits_sha, corrections)
                    or not _current_event(decision, events)):
                return False
            if kind == "individual_human_approval":
                if image_id != item["image_id"]:
                    return False
            elif sample["group_key"] != item["group_key"]:
                return False
            if not verify_geometry:
                continue
            correction = self._active_correction(sample, manifest_sha, audits_sha, corrections)
            candidate = self._verified(sample, geometry=True)
            effective = self._correction_geometry(correction, candidate) if correction else candidate
            if self._warning_info(sample, effective if correction else None)["qc_reasons"]:
                return False
            if kind == "sampled_repair_cohort" and correction:
                if _hash(_physical_lanes(self, candidate)) != _hash(_physical_lanes(self, effective)):
                    return False
        return True

    def eligible_records(self):
        """Return train-only records cleared by actual current repair approvals."""
        metadata, records, groups, manifest_sha, audits_sha = self.snapshot(fresh=True)
        decisions, corrections = self.decisions(), self.corrections()
        events = _events(self, decisions)
        audited = self._groups(records, groups, manifest_sha, audits_sha, decisions, verify_samples=True)
        passed = {group["group_key"] for group in audited
                  if group["state"] == "eligible" and group["audit_mode"] == "ready"
                  and group["split"] == "train" and not group["edited_sample_ids"]}
        result, rejected, proof_cache = [], [], {}
        for image_id, item in sorted(records.items()):
            if (item.get("audit_mode", "ready") != "ready"
                    or item["split"] != "train" or item.get("intended_split") != "train"
                    or item.get("original_split") != "hold"):
                continue
            if item["group_key"] in passed:
                clearance = {"type": "sampled_repair_cohort", "group_key": item["group_key"],
                    "sample_decisions": [deepcopy(decisions[sample])
                                         for sample in groups[item["group_key"]]["sample_ids"]],
                    "source_split": "hold", "intended_split": "train",
                    "candidate_origin": item["candidate_origin"], "edited_sample_ids": [],
                    "human_decisions_created": 0}
            else:
                decision = decisions.get(image_id)
                if not decision or decision.get("action") != "approve":
                    continue
                clearance = {"type": "individual_human_approval", "decision": deepcopy(decision),
                    "source_split": "hold", "intended_split": "train",
                    "candidate_origin": item["candidate_origin"], "human_decisions_created": 0}
            proof_key = (clearance["type"], item["group_key"] if clearance["type"] == "sampled_repair_cohort" else image_id)
            if proof_key not in proof_cache:
                proof_cache[proof_key] = self._proof_current(
                    item, clearance, records, groups, manifest_sha, audits_sha,
                    decisions, corrections, events)
            if not proof_cache[proof_key]:
                continue
            view, correction = self._training_view(item, manifest_sha, audits_sha, corrections)
            geometry = view["geometry"]
            warnings = self._warning_info(item, geometry if correction else None)
            if warnings["qc_reasons"]:
                continue
            observed = _observed_geometry(geometry)
            negative = bool(correction and not geometry["lanes"]
                            and geometry.get("no_visible_lane_paint") is True)
            policy = training_geometry_hold(observed, geometry["width"], geometry["height"])
            if not negative and (not policy["eligible_for_training"] or not has_observed_training_span(observed)):
                rejected.append({"image_id": image_id, "reason": "geometry_training_hold",
                                 "policy": policy,
                                 "observed_training_span": has_observed_training_span(observed)})
                continue
            row = deepcopy(item)
            if correction:
                row.update(original_geometry=item["geometry"], original_geometry_sha256=item["geometry_sha256"],
                    geometry=correction["geometry_path"], geometry_sha256=correction["geometry_sha256"],
                    correction_reviewer=correction["reviewer"])
            row.update(warnings, fingerprint=view["fingerprint"])
            row.update(source=SOURCE, split="train", original_split="hold", synthetic=False,
                pool="commercial", source_manifest_sha256=manifest_sha, source_audits_sha256=audits_sha,
                repair_collection=COLLECTION, clearance=clearance,
                candidate_origin=item["candidate_origin"],
                candidate_geometry=item["geometry"], candidate_geometry_sha256=item["geometry_sha256"],
                source_original_geometry=item["original_geometry"],
                source_original_geometry_sha256=item["original_geometry_sha256"],
                training_geometry_policy=policy,
                label_origin="human_audit_correction" if correction else item["candidate_origin"],
                geometry_origin="human_audit_correction" if correction else item["candidate_origin"],
                training_authorization="Explicit user request to integrate cleared repairs into converted data and train.")
            if view.get("saved_annotation"):
                annotation = view["saved_annotation"]
                row.update(geometry=annotation["path"], geometry_sha256=annotation["sha256"],
                           geometry_origin="user_directed_boundary_completion",
                           saved_annotation=deepcopy(annotation))
            result.append(row)
        result = {"schema": "college_repair_training_eligible:v1", "source": REPAIR_SOURCE,
                "records": result, "train_images": len(result), "val_images_used": 0,
                "test_images_used": 0, "manifest_sha256": manifest_sha, "audits_sha256": audits_sha,
                "source_snapshot": deepcopy(metadata.get("source_snapshot", {})),
                "rejected": rejected, "auto_promote": False, "human_decisions_created": 0}
        self._training_selection_pin = self.training_pin(result)
        return result

    def training_geometry(self, original, row):
        """Verify live approval and geometry bindings immediately before export."""
        _, records, groups, manifest_sha, audits_sha = self.snapshot()
        item = records.get(original.get("image_id"))
        if (not item or item != original or row.get("image_id") != item.get("image_id")
                or row.get("source") != SOURCE or row.get("repair_collection") != COLLECTION
                or row.get("source_manifest_sha256") != manifest_sha
                or row.get("source_audits_sha256") != audits_sha):
            raise HTTPException(409, "Repair training record lost its source snapshot binding")
        decisions, corrections = self.decisions(), self.corrections()
        if (row.get("fingerprint") != self.effective_fingerprint(item, manifest_sha, audits_sha, corrections)
                or not self._proof_current(item, row.get("clearance", {}), records, groups,
                    manifest_sha, audits_sha, decisions, corrections, _events(self, decisions),
                    verify_geometry=False)):
            raise HTTPException(409, "Repair approval changed while preparing converted training")
        view, correction = self._training_view(item, manifest_sha, audits_sha, corrections)
        expected_path = correction["geometry_path"] if correction else item["geometry"]
        expected_sha = correction["geometry_sha256"] if correction else item["geometry_sha256"]
        if view.get("saved_annotation"):
            expected_path, expected_sha = view["saved_annotation"]["path"], view["saved_annotation"]["sha256"]
        if (row.get("geometry") != expected_path or row.get("geometry_sha256") != expected_sha
                or _digest(_inside(self.root, expected_path).read_bytes()) != expected_sha):
            raise HTTPException(409, "Repair geometry changed while preparing converted training")
        geometry = view["geometry"]
        self._validate_lanes(geometry["lanes"], geometry["width"], geometry["height"], source_metadata=True)
        if self._warning_info(item, geometry if correction else None)["qc_reasons"]:
            raise HTTPException(409, "Repair training record still has unresolved warnings")
        return geometry

    def training_pin(self, eligible=None):
        """Small immutable input receipt; decisions are represented by clearance."""
        eligible = self.eligible_records() if eligible is None else eligible
        middle = _inside(self.root, INDEX)
        return {"manifest_sha256": eligible["manifest_sha256"],
                "audits_sha256": eligible["audits_sha256"],
                "import_sha256": _digest((self.root / "import.json").read_bytes()),
                "middle_boundary_annotations_sha256": _digest(middle.read_bytes()) if middle.is_file() else None,
                "clearance_sha256": _hash(eligible["records"]),
                "source_snapshot": eligible.get("source_snapshot", {}),
                "adapter_sha256": _digest(Path(__file__).read_bytes())}

    def verify_training_selection(self, rows):
        """Recheck frozen approvals; unrelated new reviews do not widen the run.

        Call on the same adapter that supplied eligible_records. Full candidate
        and evidence bytes are verified there and during training_geometry;
        this final gate checks live proof and immutable snapshot bindings.
        """
        expected = getattr(self, "_training_selection_pin", None)
        if expected is None:
            raise ValueError("Select approved repair records before verifying their frozen proof")
        metadata, records, groups, manifest_sha, audits_sha = self.snapshot(fresh=True)
        middle_path = _inside(self.root, INDEX)
        current = {"manifest_sha256": manifest_sha, "audits_sha256": audits_sha,
            "import_sha256": _digest((self.root / "import.json").read_bytes()),
            "middle_boundary_annotations_sha256": _digest(middle_path.read_bytes()) if middle_path.is_file() else None,
            "source_snapshot": metadata.get("source_snapshot", {}),
            "adapter_sha256": _digest(Path(__file__).read_bytes())}
        if any(expected.get(key) != value for key, value in current.items()):
            raise HTTPException(409, "Repair training snapshot changed during preparation")
        _, _, _, original_manifest, original_audits = self.original.snapshot(fresh=True)
        if metadata["source_snapshot"] != {
                "manifest_sha256": original_manifest, "audits_sha256": original_audits,
                "import_sha256": _digest((self.original.root / "import.json").read_bytes())}:
            raise HTTPException(409, "Original source snapshot changed during repair preparation")
        decisions, corrections = self.decisions(), self.corrections()
        events = _events(self, decisions)
        middle = _index(self)
        seen = set()
        for row in rows:
            image_id = row.get("image_id")
            item = records.get(image_id)
            if (not item or image_id in seen or row.get("source") != SOURCE
                    or row.get("repair_collection") != COLLECTION
                    or row.get("source_manifest_sha256") != manifest_sha
                    or row.get("source_audits_sha256") != audits_sha
                    or row.get("fingerprint") != self.effective_fingerprint(item, manifest_sha, audits_sha, corrections)
                    or not self._proof_current(item, row.get("clearance", {}), records, groups,
                        manifest_sha, audits_sha, decisions, corrections, events, verify_geometry=False)):
                raise HTTPException(409, "A selected repair approval changed during preparation")
            seen.add(image_id)
            if row.get("saved_annotation"):
                entry = middle["records"].get(image_id) if middle else None
                if (not entry or row["geometry"] != entry["geometry_path"]
                        or row["geometry_sha256"] != entry["geometry_sha256"]):
                    raise HTTPException(409, "A selected saved boundary changed during preparation")
                proof = middle["proofs"][entry["proof_key"]]
                if any(decisions.get(identifier) != decision
                       or decision["fingerprint"] != self.effective_fingerprint(
                           records[identifier], manifest_sha, audits_sha, corrections)
                       for identifier, decision in proof["decisions"].items()):
                    raise HTTPException(409, "A saved boundary's original approval changed during preparation")
        return {"verified_records": len(rows), "human_decisions_created": 0,
                "unrelated_new_reviews_allowed": True}
