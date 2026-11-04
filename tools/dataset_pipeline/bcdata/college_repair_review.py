"""Fresh sample audits of isolated College repair candidates.

Original imports and votes are read-only. Passing a group audit records approval
of that candidate cohort; model proposals never become original human labels.
No endpoint starts training or promotes a candidate into the cleared base.
"""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from .college_group_clearance import _hash, _physical_lanes
from .real_source_review import RealSourceReview
from .simulation_review import SimulationCorrection, SimulationDecision, _digest, _inside

API = "/api/studio/college-repairs"
COLLECTION = "college_held_repairs-20261022"
SOURCE = "college_station_lane_marking"


class CollegeRepairReview(RealSourceReview):
    def __init__(self, data_root: Path):
        super().__init__(data_root, SOURCE)
        self.root = self.data_root / "experiments" / COLLECTION
        self.original = RealSourceReview(data_root, SOURCE)
        self._boundary_edit = ContextVar("college_boundary_edit", default=None)

    def _metadata(self):
        metadata = super()._metadata()
        if metadata and (metadata.get("collection") != COLLECTION
                         or metadata.get("candidate_only") is not True
                         or metadata.get("training_eligible") is not False
                         or not isinstance(metadata.get("source_snapshot"), dict)):
            raise HTTPException(409, "Repair candidates lack their isolated source binding")
        return metadata

    def snapshot(self, *, fresh=False):
        result = super().snapshot(fresh=fresh)
        _, records, groups, _, _ = result
        if any(group["count"] > 100 for group in groups.values()):
            raise HTTPException(409, "Repair audit groups must contain at most 100 photos")
        originals = self.original.snapshot(fresh=fresh)[1] if records else {}
        for image_id, item in records.items():
            original = originals.get(image_id)
            if (not original or item["split"] != "train" or item.get("intended_split") != "train"
                    or item["session_id"] != original["session_id"]
                    or item.get("original_split") != "hold" or original["split"] != "hold"):
                raise HTTPException(409, "Repair audit crossed an original source identity or split")
            if item.get("audit_mode", "ready") not in {"ready", "needs_repair"}:
                raise HTTPException(409, "Unknown repair audit mode")
        for key in groups:
            members = [item for item in records.values() if item["group_key"] == key]
            if (len({item.get("audit_mode", "ready") for item in members}) != 1
                    or len({item.get("candidate_origin") for item in members}) != 1):
                raise HTTPException(409, "Repair audit groups must share one candidate origin and audit mode")
        return result

    def _warning_info(self, item, correction_geometry=None):
        result = super()._warning_info(item, correction_geometry)
        if item.get("audit_mode") == "needs_repair":
            # A manual edit of one diagnostic sample cannot repair its cohort.
            # Keep this separate from ordinary warning acknowledgements so both
            # direct approval and Save & approve retain the group hold.
            for field in ("source_qc_reasons", "qc_reasons"):
                result[field] = list(dict.fromkeys([*result[field], "repair_review_required"]))
        return result

    def _verified(self, item, *, geometry=False):
        metadata = self._metadata()
        _, records, _, manifest_sha, audits_sha = self.original.snapshot()
        original = records.get(item["image_id"])
        try:
            import_sha = _digest((self.original.root / "import.json").read_bytes())
            original_raw = _inside(self._root(), item["original_geometry"]).read_bytes()
        except (OSError, KeyError, TypeError):
            raise HTTPException(409, "Original repair evidence is unavailable") from None
        expected = {"manifest_sha256": manifest_sha, "audits_sha256": audits_sha,
                    "import_sha256": import_sha}
        if (not original or metadata["source_snapshot"] != expected
                or item.get("original_geometry_sha256") != original["geometry_sha256"]
                or item.get("original_qc_reasons") != original["qc_reasons"]
                or item.get("source_sha256") != original["source_sha256"]
                or item.get("source_label_sha256") != original["source_label_sha256"]
                or item.get("original_split") != "hold" or original["split"] != "hold"
                or original.get("intended_split") != "train"
                or item.get("candidate_origin") not in {"source_repair", "model_proposal"}):
            raise HTTPException(409, "Repair candidate no longer matches its original held photo")
        self.original._verified(original, geometry=True)
        if _digest(original_raw) != original["geometry_sha256"]:
            raise HTTPException(409, "Original geometry evidence changed")
        try:
            for asset in item.get("evidence_files", []):
                if _digest(_inside(self._root(), asset["path"]).read_bytes()) != asset["sha256"]:
                    raise HTTPException(409, "Repair proposal evidence changed")
        except (OSError, KeyError, TypeError):
            raise HTTPException(409, "Repair proposal evidence is unavailable") from None
        value = super()._verified(item, geometry=geometry)
        if geometry and (value.get("candidate_only") is not True
                         or value.get("training_eligible") is not False
                         or value.get("human_complete_annotation") is True):
            raise HTTPException(409, "A proposal cannot claim a complete human annotation")
        return value

    def detail(self, image_id, *, snapshot=None):
        result = super().detail(image_id, snapshot=snapshot)
        _, records, _, _, _ = snapshot or self.snapshot()
        item = records[image_id]
        original = self.original.snapshot()[1][image_id]
        result.update(source="college_held_repairs", image_url=API + "/samples/" + image_id + "/photo",
                      original_geometry=self.original._verified(original, geometry=True),
                      original_qc_reasons=item["original_qc_reasons"],
                      candidate_origin=item["candidate_origin"], candidate_only=True,
                      label_origin=item["candidate_origin"], training_eligible=False,
                      repair_description=item.get("repair_description", "Inspect the proposed repair against all visible paint."))
        result["audit_mode"] = item.get("audit_mode", "ready")
        from .college_middle_boundary import apply_saved_connection
        result = apply_saved_connection(self, item, result)
        if self._boundary_edit.get() == image_id and result.get("boundary_completion"):
            # The shared correction writer copies this view into its immutable
            # overlay. Keep the prior completion as history, without claiming
            # that its gap counts still describe the user's new geometry.
            result["geometry"] = deepcopy(result["geometry"])
            prior = result["geometry"].pop("boundary_completion")
            result["geometry"].setdefault("boundary_completion_history", []).append(
                {**prior, "superseded_by_manual_edit": True})
            result.pop("boundary_completion", None)
            result.pop("saved_annotation", None)
        return result

    def correct(self, body: SimulationCorrection, reviewer: str):
        # Opening a connected, already approved boundary and saving without any
        # edits should preserve the existing vote rather than create a new one.
        current = self.detail(body.image_id, snapshot=self.snapshot(fresh=True))
        # Accept an intentional two-endpoint replacement from an already open
        # editor, too. Older clients omitted the section on a freshly redrawn
        # slot, which made the shared validator reject this valid replacement.
        previous = {lane["slot"]: lane for lane in current["geometry"]["lanes"]}
        redrawn = []
        for lane in body.lanes:
            old = previous.get(lane.get("slot"))
            if old and "segments" in old and "segments" not in lane and len(lane.get("points", [])) == 2:
                lane = {**lane, "segments": [lane["points"]]}
            redrawn.append(lane)
        body = body.model_copy(update={"lanes": redrawn})
        if current.get("boundary_completion") and (current.get("decision") or {}).get("action") == "approve":
            if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 80:
                raise HTTPException(400, "A reviewer name is required")
            if current["revision"] != body.revision or current["fingerprint"] != body.fingerprint:
                raise HTTPException(409, "Another reviewer changed this sample; reload it")
            size = current["geometry"]
            submitted = self._validate_lanes(body.lanes, size["width"], size["height"])
            saved = self._validate_lanes(size["lanes"], size["width"], size["height"], source_metadata=True)
            if (submitted == saved and not body.resolved_qc_reasons and not body.complete_annotation
                    and not body.no_visible_lane_paint and not current["qc_reasons"]):
                return current
        token = self._boundary_edit.set(body.image_id)
        try:
            return super().correct(body, reviewer)
        finally:
            self._boundary_edit.reset(token)

    def _groups(self, records, groups, manifest_sha, audits_sha, decisions, *, verify_samples=False):
        result = super()._groups(records, groups, manifest_sha, audits_sha, decisions,
                                 verify_samples=verify_samples)
        corrections = self.corrections()
        for group in result:
            group["audit_mode"] = records[group["sample_ids"][0]].get("audit_mode", "ready")
            changed = []
            for image_id in group["sample_ids"]:
                item = records[image_id]
                correction = self._active_correction(item, manifest_sha, audits_sha, corrections)
                if not correction:
                    continue
                try:
                    candidate = self._verified(item, geometry=True)
                    corrected = self._correction_geometry(correction, candidate)
                    if _hash(_physical_lanes(self, candidate)) != _hash(_physical_lanes(self, corrected)):
                        changed.append(image_id)
                except HTTPException:
                    changed.append(image_id)
            group["edited_sample_ids"] = changed
            if changed:
                if group["state"] != "blocked":
                    group["state"] = "needs_repair"
                group["eligible_train_images"] = 0
        return result

    def status(self):
        result = super().status()
        counts = result["counts"]
        counts["audit_passed_photos"] = counts["eligible_train_candidates"]
        counts["eligible_train_candidates"] = 0
        counts["pending_problem_samples"] = sum(group["pending"] for group in result["groups"]
                                                if group["audit_mode"] == "needs_repair")
        counts["pending"] -= counts["pending_problem_samples"]
        counts["problem_photos"] = sum(group["images"] for group in result["groups"]
                                       if group["audit_mode"] == "needs_repair")
        counts["ready_candidate_photos"] = counts["imported"] - counts["problem_photos"]
        counts["groups_needing_repair"] = sum(group["state"] == "needs_repair" or
                                               group["audit_mode"] == "needs_repair" for group in result["groups"])
        metadata = result["metadata"]
        counts["remaining_original_holds"] = metadata.get("original_held_count", 3833)
        counts["not_staged"] = max(0, counts["remaining_original_holds"] - counts["imported"])
        result.update(source="college_held_repairs", candidate_only=True, training_eligible=False,
            policy="Check three spread-out examples per repair group. Approve only when every visible painted boundary and lane slot is correct. A skip holds the group; an exclusion blocks it. Editing a sample holds its whole group for another repair. Model proposals remain separate from original human labels. Training uses separately integrated cleared snapshots.")
        return result

    def queue(self, split="all", status="pending", limit=1, after=""):
        if status not in {"pending", "needs_repair", "all", "approve", "exclude", "skip"}:
            raise HTTPException(400, "Invalid repair audit queue")
        if status not in {"pending", "needs_repair"}:
            return super().queue(split, status, limit, after)
        if split not in {"all", "train", "val", "test", "hold"} or not 1 <= limit <= 12:
            raise HTTPException(400, "Invalid repair audit filters")
        snapshot = self.snapshot()
        _, records, groups, manifest_sha, audits_sha = snapshot
        decisions, corrections = self.decisions(), self.corrections()
        ordered = sorted({image_id for group in groups.values() for image_id in group["sample_ids"]},
                         key=lambda image_id: (records[image_id]["group_key"], image_id))
        if after and after not in ordered:
            raise HTTPException(404, "Repair audit cursor is not a sample")
        start = ordered.index(after) + 1 if after else 0
        selected = []
        for image_id in ordered[start:]:
            item = records[image_id]
            if (item.get("audit_mode", "ready") == "needs_repair") != (status == "needs_repair"):
                continue
            if split != "all" and item["split"] != split:
                continue
            decision = decisions.get(image_id)
            if decision and decision["fingerprint"] == self.effective_fingerprint(item, manifest_sha, audits_sha, corrections):
                continue
            selected.append(self.detail(image_id, snapshot=snapshot))
            if len(selected) >= limit:
                break
        return {"items": selected, "split": split, "status": status,
                "next_after": selected[-1]["image_id"] if selected else None,
                "audits_ready": bool(groups), "auto_promote": False}

    def eligible_records(self):
        return {"schema": "college_repair_eligible:v1", "records": [], "train_images": 0,
                "candidate_only": True, "training_eligible": False, "auto_promote": False,
                "meaning": "Group audits record candidate quality; training integration lives in a separate converted snapshot."}


def create_college_repair_router(data_root: Path, require_session: Callable) -> APIRouter:
    router = APIRouter(prefix=API, tags=["isolated College repair audits"])
    store = CollegeRepairReview(data_root)

    def authenticated(session=Depends(require_session)):
        if not isinstance(session, dict) or not session.get("reviewer"):
            raise HTTPException(401, "Pair this device before checking repair samples")
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

    @router.get("/eligible")
    def eligible(session=Depends(authenticated)):
        return store.eligible_records()

    @router.post("/decision")
    def decision(body: SimulationDecision, session=Depends(authenticated)):
        return store.decide(body, session["reviewer"])

    @router.post("/correction")
    def correction(body: SimulationCorrection, session=Depends(authenticated)):
        return store.correct(body, session["reviewer"])

    return router
