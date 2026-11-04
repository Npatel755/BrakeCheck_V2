"""Sampled ZOD conversion audits with immutable assets and separate human edits."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import time
import uuid
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import Field

from .simulation_review import SimulationCorrection, SimulationDecision, SimulationReview, _inside
from .zod_mirror import ID, connect, encoded, sha
from .zod_audit_groups import ensure_plan, states

API = "/api/studio/zod"
COMPLETE = {"missing_or_invalid_native_lane_annotations", "no_converted_lane_requires_whole_photo_check",
            "paint_polygon_needs_manual_trace", "invalid_native_paint_shape",
            "invalid_or_out_of_bounds_native_polygon", "more_than_four_boundary_groups",
            "boundary_exceeds_editor_point_limit", "sparse_native_paint_needs_review"}


class ZodCorrection(SimulationCorrection):
    event_id: str | None = Field(default=None, min_length=8, max_length=160)
    notes: str = Field(default="", max_length=4000)
    candidate: str | None = None
    candidate_version: str | None = None


class ZodDecision(SimulationDecision):
    notes: str = Field(default="", max_length=4000)


class ZodMirrorReview:
    def __init__(self, data_root, *, prefer_repair=True, batch=None):
        self.root = Path(data_root).resolve() / "experiments/zod_mirror"
        self.prefer_repair = prefer_repair
        self.batch = batch

    def _db(self):
        from .zod_active_batch import active_batch
        selected = self.root / self.batch if self.batch else active_batch(self.root) if self.prefer_repair else self.root
        db = connect(selected)
        db.execute("CREATE TABLE IF NOT EXISTS zod_model_proposals (image_id TEXT,model TEXT,version TEXT,geometry_json TEXT,geometry_sha TEXT,source_binding TEXT,PRIMARY KEY(image_id,model))")
        db.commit()
        ensure_plan(db)
        db.execute('CREATE TABLE IF NOT EXISTS zod_review_claims(image_id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL)')
        db.commit()
        return db

    def claim(self, image_id, owner, *, revision=None, fingerprint=None, replace_previous=True):
        """Reserve atomically, releasing this browser's previous photo only on success."""
        db = self._db()
        try:
            db.execute('BEGIN IMMEDIATE')
            row = self._record(db, image_id)
            if revision is not None and (row['revision'] != revision or self.fingerprint(row) != fingerprint):
                raise HTTPException(409, 'Another reviewer saved this photo. Your draft is kept; load the latest labels before saving.')
            now = time.time()
            claim = db.execute('SELECT * FROM zod_review_claims WHERE image_id=?', (image_id,)).fetchone()
            if claim and claim['owner'] != owner and claim['expires'] > now:
                raise HTTPException(409, 'Another reviewer is working on this photo. Your draft is kept; choose another photo.')
            if not replace_previous and db.execute('SELECT 1 FROM zod_review_claims WHERE owner=? AND image_id!=? AND expires>?', (owner, image_id, now)).fetchone():
                raise HTTPException(409, 'This review has moved to another photo.')
            db.execute('DELETE FROM zod_review_claims WHERE expires<=?', (now,))
            if replace_previous:
                db.execute('DELETE FROM zod_review_claims WHERE owner=? AND image_id!=?', (owner, image_id))
            db.execute('INSERT OR REPLACE INTO zod_review_claims VALUES(?,?,?)', (image_id, owner, now + 600))
            db.commit()
            return {'image_id': image_id, 'expires': now + 600}
        finally:
            db.close()

    def release(self, image_id, owner):
        db = self._db()
        try:
            db.execute('DELETE FROM zod_review_claims WHERE image_id=? AND owner=?', (image_id, owner))
            db.commit()
        finally:
            db.close()

    def _record(self, db, image_id):
        if not isinstance(image_id, str) or not ID.fullmatch(image_id):
            raise HTTPException(400, "Invalid ZOD frame identifier")
        row = db.execute("SELECT * FROM frames WHERE image_id=?", (image_id,)).fetchone()
        if row is None or not row["image_ready"]:
            raise HTTPException(404, "This ZOD photo is still downloading or indexing")
        return row

    @staticmethod
    def fingerprint(row):
        return sha(encoded([row["image_id"], row["native_sha"], row["image_sha"],
                            row["geometry_sha"], row["correction_sha"]]).encode())

    def _verified(self, row):
        path = _inside(self.root, row["image_path"])
        if not path.is_file() or sha(path.read_bytes()) != row["image_sha"]:
            raise HTTPException(409, "ZOD source photo changed")
        if sha(row["native_json"].encode()) != row["native_sha"] or sha(row["geometry_json"].encode()) != row["geometry_sha"]:
            raise HTTPException(409, "ZOD source annotation or conversion changed")
        if row["correction_json"] and sha(row["correction_json"].encode()) != row["correction_sha"]:
            raise HTTPException(409, "Saved human correction changed")
        return path

    def _detail(self, row, db):
        self._verified(row)
        original = json.loads(row["geometry_json"])
        geometry = json.loads(row["correction_json"] or row["geometry_json"])
        warnings = json.loads(row["qc_json"])
        resolved = json.loads(row["resolved_json"])
        fingerprint = self.fingerprint(row)
        current = row["action"] and row["decision_fingerprint"] == fingerprint
        group = db.execute("SELECT g.* FROM zod_audit_groups g JOIN zod_audit_members m USING(group_key) WHERE m.image_id=?", (row["image_id"],)).fetchone()
        group_state = states(db, group["group_key"])[group["group_key"]]
        findings = [dict(value) for value in db.execute("SELECT finding,origin FROM zod_conversion_findings WHERE image_id IN (SELECT image_id FROM zod_audit_members WHERE group_key=?)", (group["group_key"],))]
        proposals = []
        for candidate in db.execute("SELECT * FROM zod_model_proposals WHERE image_id=? ORDER BY model", (row["image_id"],)):
            if sha(candidate["geometry_json"].encode()) != candidate["geometry_sha"] or candidate["source_binding"] != sha(encoded([row["native_sha"], row["image_sha"]]).encode()):
                raise HTTPException(409, "Model proposal changed or belongs to different source bytes")
            model_geometry = json.loads(candidate["geometry_json"])
            proposals.append({"name": candidate["model"], "label": model_geometry["model_label"],
                              "version": candidate["version"], "geometry": model_geometry,
                              "capabilities": {"painted_lanes": True, "road_edges": False},
                              "summary": {"painted_lines": len(model_geometry["lanes"]), "road_edges": 0, "inference": "independent"}})
        return {"image_id": row["image_id"], "source": "zod_mirror", "split": "hold",
                "session_id": "ZOD frame " + row["frame_id"] + " · recording split unassigned",
                "group_key": group["group_key"], "group_images": group["member_count"],
                "group_state": group_state["state"], "group_description": group["description"],
                "group_samples": group_state["samples"],
                "conversion_findings": findings,
                "teacher_options": proposals,
                "geometry": geometry, "source_geometry": original,
                "source_polygons": original.get("source_polygons", []),
                "source_qc_reasons": warnings, "resolved_qc_reasons": resolved,
                "qc_reasons": [warning for warning in warnings if warning not in resolved],
                "repairable_qc_reasons": [warning for warning in warnings if warning not in COMPLETE],
                "complete_annotation_qc_reasons": [warning for warning in warnings if warning in COMPLETE],
                "human_complete_annotation": geometry.get("human_complete_annotation", False),
                "no_visible_lane_paint": geometry.get("no_visible_lane_paint", False),
                "annotation_policy_risks": ["Provisional source-coordinate scaling and dashed-piece grouping: check the source polygons and all visible boundaries"],
                "fingerprint": fingerprint, "revision": row["revision"],
                "decision": {"action": row["action"], "reviewer": row["reviewer"]} if current else None,
                "previous_decision_stale": bool(row["action"] and not current),
                "correction": {"reviewer": row["correction_reviewer"]} if row["correction_json"] else None,
                "image_url": API + "/samples/" + row["image_id"] + "/photo",
                "auto_promote": False, "pool": "share_alike", "training_eligible": False}

    def detail(self, image_id):
        db = self._db()
        try:
            return self._detail(self._record(db, image_id), db)
        finally:
            db.close()

    def status(self):
        db = self._db()
        try:
            counts = {"imported": db.execute("SELECT COUNT(*) FROM frames WHERE image_ready=1").fetchone()[0],
                      "indexed": db.execute("SELECT COUNT(*) FROM frames").fetchone()[0],
                      "approved": 0, "excluded": 0, "skipped": 0, "eligible_train_candidates": 0,
                      "groups_eligible": 0, "groups_blocked": 0}
            for action, count in db.execute("SELECT f.action,COUNT(*) FROM frames f JOIN zod_audit_members m USING(image_id) WHERE f.image_ready=1 AND m.is_sample=1 GROUP BY f.action"):
                if action in ("approve", "exclude", "skip"):
                    counts[{"approve": "approved", "exclude": "excluded", "skip": "skipped"}[action]] = count
            counts["samples"] = db.execute("SELECT COUNT(*) FROM zod_audit_members WHERE is_sample=1").fetchone()[0]
            counts["pending"] = counts["samples"] - counts["approved"] - counts["excluded"] - counts["skipped"]
            counts["audits_remaining"] = counts["pending"]
            counts["awaiting_photos"] = counts["indexed"] - counts["imported"]
            groups = states(db)
            blocked = [key for key, value in groups.items() if value["state"] == "needs_repair"]
            if blocked:
                counts["samples_held_for_repair"] = db.execute("SELECT COUNT(*) FROM zod_audit_members WHERE is_sample=1 AND group_key IN (" + ",".join("?" for _ in blocked) + ")", blocked).fetchone()[0]
                counts["pending"] = db.execute("SELECT COUNT(*) FROM zod_audit_members m JOIN frames f USING(image_id) WHERE m.is_sample=1 AND f.image_ready=1 AND f.action IS NULL AND m.group_key NOT IN (" + ",".join("?" for _ in blocked) + ")", blocked).fetchone()[0]
                counts["audits_remaining"] = counts["pending"]
            counts.update(groups=len(groups), hold=counts["imported"],
                          groups_blocked=sum(g["state"] == "needs_repair" for g in groups.values()),
                          groups_audited=sum(g["state"] == "audited" for g in groups.values()))
            counts["photos_in_repair_groups"] = sum(row["member_count"] for row in db.execute("SELECT * FROM zod_audit_groups") if groups[row["group_key"]]["state"] == "needs_repair")
            from .zod_clearance import clearance_counts
            counts.update(clearance_counts(db, groups))
            counts['model_proposals_ready'] = db.execute('SELECT COUNT(*) FROM zod_model_proposals p JOIN zod_audit_members m USING(image_id) WHERE m.is_sample=1').fetchone()[0]
            counts['model_proposals_total'] = 2 * counts['samples']
        finally:
            db.close()
        progress = {}
        for name in ("import_status.json", "download_status.json"):
            path = self.root / name
            if path.exists():
                progress[name.removesuffix(".json")] = json.loads(path.read_text())
        from .zod_active_batch import active_batch
        for name in ("repair_status", "proposal_status"):
            path = active_batch(self.root) / (name + ".json")
            if path.exists():
                progress[name] = json.loads(path.read_text())
        proposal_status = progress.get("proposal_status", {})
        # Cached proposals for the old plan are preserved, but newly selected
        # samples must not inherit the old plan's "complete" progress receipt.
        from .report_reader import optional_report
        preparation = optional_report(self.root.parent.parent / 'reports/zod_training_preparation.json', {})
        prepared = preparation.get('phase') == 'prepared'
        counts['prepared_zod_images'] = preparation.get('accepted_zod', 0) if prepared else 0
        counts['prepared_train_images'] = preparation.get('train_images', 0) if prepared else 0
        download = progress.get("download_status", {})
        imported = progress.get("import_status", {})
        gb = download.get("downloaded_bytes", 0) / 1e9
        total = download.get("total_bytes", 0) / 1e9
        return {"source": "zod_mirror", "counts": counts, "audits_ready": bool(counts["imported"]),
                "groups": [], "metadata": {"state": imported.get("state", "downloading")},
                "progress": {**imported, "state": imported.get("state", download.get("phase", "starting")),
                             "download": download, "indexed": counts["indexed"]},
                "auto_promote": False, "license": "CC-BY-SA-4.0", "pool": "share_alike",
                "policy": f"{counts['indexed']:,} photos covered by {counts['groups']:,} conversion groups; {counts['samples']:,} audit samples (up to 3 per group). {counts['label_cleared']:,} labels cleared, including {counts['individually_approved']:,} individual approvals. Machine findings are separated by repair cause after explicit audit-plan migration. An actual sample geometry change holds its source cohort; unchanged whole-photo confirmations are valid audit evidence. Original photos, polygons and human decisions are preserved. Label clearance is separate from frozen training admission; these share-alike photos are not in the existing training snapshot."}

    def queue(self, split="all", status="pending", limit=1, after=""):
        if split not in ("all", "train", "val", "test", "hold") or status not in ("pending", "approve", "exclude", "skip", "all", "needs_repair"):
            raise HTTPException(400, "Unknown ZOD audit queue")
        if after and not ID.fullmatch(after):
            raise HTTPException(400, "Invalid queue position")
        db = self._db()
        try:
            where, params = "f.image_ready=1 AND m.is_sample=1", []
            if split not in ("all", "hold"):
                return {"items": [], "audits_ready": True}
            if status == "pending":
                where += " AND f.action IS NULL"
                blocked = [key for key, value in states(db).items() if value["state"] == "needs_repair"]
                if blocked:
                    where += " AND m.group_key NOT IN (" + ",".join("?" for _ in blocked) + ")"
                    params.extend(blocked)
            elif status == "needs_repair":
                blocked = [key for key, value in states(db).items() if value["state"] == "needs_repair"]
                if not blocked:
                    return {"items": [], "audits_ready": True}
                where += " AND m.group_key IN (" + ",".join("?" for _ in blocked) + ")"
                params.extend(blocked)
            elif status != "all":
                where += " AND f.action=?"
                params.append(status)
            if after:
                where += " AND f.image_id>?"
                params.append(after)
            rows = db.execute("SELECT f.* FROM zod_audit_members m JOIN frames f USING(image_id) WHERE " + where + " ORDER BY f.image_id LIMIT ?", [*params, limit]).fetchall()
            ready = db.execute("SELECT 1 FROM frames WHERE image_ready=1 LIMIT 1").fetchone() is not None
            return {"items": [self._detail(row, db) for row in rows], "audits_ready": ready}
        finally:
            db.close()

    def decide(self, body, reviewer, owner=None):
        if body.action not in ("approve", "exclude", "skip") or not re.fullmatch(r"[A-Za-z0-9_.-]{8,160}", body.event_id):
            raise HTTPException(422, "Invalid audit action or event")
        return self._mutate(body, reviewer, correction=False, owner=owner)

    def correct(self, body, reviewer, owner=None):
        return self._mutate(body, reviewer, correction=True, owner=owner)

    def undo(self, event_id, reviewer, owner=None, image_id=None):
        from .zod_review_undo import undo
        return undo(self, event_id, reviewer, owner, image_id)

    def _mutate(self, body, reviewer, *, correction, owner=None):
        db = self._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = self._record(db, body.image_id)
            self._verified(row)
            event_id = getattr(body, "event_id", None)
            request_digest = sha(encoded(body.model_dump()).encode())
            if event_id:
                if not re.fullmatch(r"[A-Za-z0-9_.-]{8,160}", event_id):
                    raise HTTPException(422, "Invalid audit event")
                event = db.execute("SELECT payload FROM audit_events WHERE event_id=?", (event_id,)).fetchone()
                if event:
                    payload = json.loads(event[0])
                    if payload["image_id"] != body.image_id or payload.get("request_digest") != request_digest or payload["reviewer"] != reviewer:
                        raise HTTPException(409, "Event ID already belongs to another audit")
                    return self._detail(row, db)
            if row["revision"] != body.revision or self.fingerprint(row) != body.fingerprint:
                raise HTTPException(409, "Another reviewer saved this photo. Your draft is kept; load the latest labels before saving.")
            claim = db.execute('SELECT * FROM zod_review_claims WHERE image_id=?', (body.image_id,)).fetchone()
            if claim and claim['expires'] > time.time() and owner != claim['owner']:
                raise HTTPException(409, 'Another reviewer is working on this photo. Your draft is kept; choose another photo.')
            from .zod_review_undo import snapshot
            before_state = snapshot(db, row)
            warnings = set(json.loads(row["qc_json"]))
            resolved = set(json.loads(row["resolved_json"]))
            action = body.action if not correction else None
            if correction:
                lanes = SimulationReview._validate_lanes(body.lanes, 1280, 720)
                from .lane_boundary_deduplication import coincident_outer_slots
                if coincident_outer_slots(lanes):
                    raise HTTPException(422, 'An outer label repeats a main lane boundary. Keep one boundary label for paired or double paint, or move the outer label onto a separate boundary.')
                if lanes and body.no_visible_lane_paint:
                    raise HTTPException(422, "The no-visible-paint confirmation conflicts with these lane labels")
                if len(encoded(lanes)) > 512000:
                    raise HTTPException(422, "Correction is too large")
                if not lanes and not (body.complete_annotation and body.no_visible_lane_paint):
                    raise HTTPException(422, "Confirm a whole-photo check and no visible lane paint before saving empty labels")
                proposed = set(body.resolved_qc_reasons)
                if not proposed.issubset(warnings):
                    raise HTTPException(422, "Unknown warning resolution")
                if proposed & COMPLETE:
                    if not body.complete_annotation:
                        raise HTTPException(422, "Confirm the whole photo to resolve missing or incomplete labels")
                    if body.no_visible_lane_paint != (not lanes):
                        raise HTTPException(422, "Confirm explicitly when the whole photo has no visible lane paint")
                source_geometry = json.loads(row["geometry_json"])
                evidence_geometry = source_geometry
                candidate_name = getattr(body, "candidate", None)
                if candidate_name:
                    candidate = db.execute("SELECT * FROM zod_model_proposals WHERE image_id=? AND model=?", (body.image_id, candidate_name)).fetchone()
                    if (not candidate or candidate["version"] != getattr(body, "candidate_version", None)
                            or sha(candidate["geometry_json"].encode()) != candidate["geometry_sha"]
                            or candidate["source_binding"] != sha(encoded([row["native_sha"], row["image_sha"]]).encode())):
                        raise HTTPException(409, "Selected model proposal changed")
                    evidence_geometry = json.loads(candidate["geometry_json"])
                evidence_by_slot = {lane["slot"]: lane for lane in evidence_geometry["lanes"]}
                saved_lanes = []
                for lane in lanes:
                    evidence = evidence_by_slot.get(lane["slot"])
                    same = evidence and SimulationReview._same_lane_geometry(evidence, lane)
                    saved_lanes.append({**evidence, **lane} if same else lane)
                geometry = {**source_geometry, "lanes": saved_lanes,
                            "origin": "human_audit_correction", "human_complete_annotation": body.complete_annotation,
                            "no_visible_lane_paint": body.no_visible_lane_paint}
                if evidence_geometry.get("boundary_completion"):
                    geometry["boundary_completion"] = {**evidence_geometry["boundary_completion"],
                        "interpolated_gaps": sum(len(lane.get("inferred_sections", [])) for lane in saved_lanes),
                        "human_review_created": False}
                raw = encoded(geometry)
                db.execute("UPDATE frames SET correction_json=?,correction_sha=?,correction_reviewer=?,correction_time=?,resolved_json=? WHERE image_id=?",
                           (raw, sha(raw.encode()), reviewer, time.time(), encoded(sorted(proposed)), body.image_id))
                resolved = proposed
                action = "approve" if body.approve and not warnings - resolved else None
            if action == "approve" and warnings - resolved:
                raise HTTPException(422, "Fix or confirm the flagged conversion before approving")
            if correction:
                from .zod_clearance import confirmation_is_source_evidence
                needs_repair = not confirmation_is_source_evidence(source_geometry, geometry, warnings)
                if needs_repair:
                    db.execute("UPDATE zod_audit_members SET repair_required=1 WHERE image_id=?", (body.image_id,))
            elif action == "exclude":
                db.execute("UPDATE zod_audit_members SET repair_required=1 WHERE image_id=?", (body.image_id,))
            current = db.execute("SELECT * FROM frames WHERE image_id=?", (body.image_id,)).fetchone()
            fingerprint = self.fingerprint(current)
            db.execute("UPDATE frames SET revision=revision+1,action=?,reviewer=?,decision_fingerprint=?,decision_time=? WHERE image_id=?",
                       (action, reviewer, fingerprint if action else None, time.time(), body.image_id))
            payload = {"image_id": body.image_id, "action": action, "reviewer": reviewer,
                       "request_fingerprint": body.fingerprint, "fingerprint": fingerprint,
                       "kind": "correction" if correction else "decision", "request_digest": request_digest,
                       "notes": getattr(body, "notes", ""), "candidate": getattr(body, "candidate", None),
                       "candidate_version": getattr(body, "candidate_version", None)}
            after = db.execute('SELECT * FROM frames WHERE image_id=?', (body.image_id,)).fetchone()
            payload.update(before_state=before_state, after_state=snapshot(db, after))
            if correction:
                payload["geometry"] = json.loads(current["correction_json"])
            db.execute("INSERT INTO audit_events VALUES(?,?,?,?)",
                       (event_id or uuid.uuid4().hex, body.image_id, encoded(payload), time.time()))
            db.execute('DELETE FROM zod_review_claims WHERE image_id=?', (body.image_id,))
            db.commit()
            return self._detail(db.execute("SELECT * FROM frames WHERE image_id=?", (body.image_id,)).fetchone(), db)
        finally:
            db.close()


def create_zod_mirror_router(data_root: Path, require_session: Callable):
    router = APIRouter(prefix=API, tags=["ZOD conversion group audits"])
    store = ZodMirrorReview(data_root)
    original = ZodMirrorReview(data_root, prefer_repair=False)
    previous = ZodMirrorReview(data_root, batch="repairs/observed-curves-v2")

    @router.get("/previous/samples/{image_id}")
    def previous_detail(image_id: str, session=Depends(require_session)):
        return previous.detail(image_id)

    @router.get("/original/samples/{image_id}")
    def original_detail(image_id: str, session=Depends(require_session)):
        return original.detail(image_id)

    @router.get("")
    def status(session=Depends(require_session)):
        return store.status()

    @router.get("/queue")
    def queue(split: str = "all", status: str = "pending", limit: int = Query(1, ge=1, le=12),
              after: str = "", session=Depends(require_session)):
        return store.queue(split, status, limit, after)

    @router.get("/samples/{image_id}")
    def detail(image_id: str, session=Depends(require_session)):
        return store.detail(image_id)

    @router.get("/samples/{image_id}/photo")
    def photo(image_id: str, session=Depends(require_session)):
        db = store._db()
        try:
            path = store._verified(store._record(db, image_id))
        finally:
            db.close()
        return FileResponse(path, headers={"Cache-Control": "private, no-store"})

    @router.post("/decision")
    def decide(body: SimulationDecision, session=Depends(require_session)):
        return store.decide(body, session["reviewer"], session['owner'])

    @router.post("/correction")
    def correct(body: ZodCorrection, session=Depends(require_session)):
        return store.correct(body, session["reviewer"], session['owner'])

    return router
