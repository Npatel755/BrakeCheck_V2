"""Transactional human review, immutable correction files, and reversible decisions."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from pathlib import Path
import copy
import hashlib
import json
import math
import os
import sqlite3
import time
import uuid

from .bootstrap import atomic_json
from .cvat import _geometry_shift, _lane_from_points
from .db import Registry
from .geometry import make_crop
from .label_policy import LABELING_POLICY, validate_road_edges, with_label_policy
from .report_reader import optional_report
from .lanes import current_geometry
from .source_validation import queue_clause


REPAIR_SAMPLE_CLAUSE = """EXISTS(SELECT 1 FROM conversion_diagnostic_samples d
    JOIN source_conversion_checks c ON c.image_id=d.image_id
    WHERE d.image_id=i.image_id AND c.state='flagged'
      AND c.geometry_id=d.geometry_id AND c.source_id=d.source_id)""" + " AND " + queue_clause(state='flagged')


LABEL_CHECK_PENDING_CLAUSE = "EXISTS(SELECT 1 FROM label_review_checks l WHERE l.image_id=i.image_id AND l.state='pending')"
REVIEW_LATER_ELIGIBLE_CLAUSE = "(i.review_status NOT IN ('approved','corrected','rejected') OR " + LABEL_CHECK_PENDING_CLAUSE + ")"


class ReviewError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class ReviewStore:
    def __init__(self, settings):
        self.settings = settings
        with self.registry() as reg:
            reg.conn.executescript("""
                CREATE TABLE IF NOT EXISTS local_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO local_meta VALUES ('revision','0');
                CREATE TABLE IF NOT EXISTS review_versions(image_id TEXT PRIMARY KEY REFERENCES images(image_id), revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS review_events(event_id TEXT PRIMARY KEY, image_id TEXT NOT NULL REFERENCES images(image_id), action TEXT NOT NULL, reviewer TEXT NOT NULL, before_state TEXT NOT NULL, after_state TEXT NOT NULL, created_at REAL NOT NULL, undone_event TEXT);
                CREATE INDEX IF NOT EXISTS review_events_image_time ON review_events(image_id,created_at);
                CREATE TABLE IF NOT EXISTS review_claims(image_id TEXT PRIMARY KEY REFERENCES images(image_id), owner TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS review_deferrals(image_id TEXT PRIMARY KEY REFERENCES images(image_id), reviewer TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS phone_sessions(token_hash TEXT PRIMARY KEY, reviewer TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS conversion_diagnostic_samples(
                    batch_id TEXT NOT NULL,image_id TEXT NOT NULL,family TEXT NOT NULL,
                    geometry_id INTEGER NOT NULL,source_id INTEGER NOT NULL,created_at REAL NOT NULL,
                    PRIMARY KEY(batch_id,image_id));
                CREATE INDEX IF NOT EXISTS conversion_diagnostic_image ON conversion_diagnostic_samples(image_id);
                CREATE TABLE IF NOT EXISTS label_review_checks(
                    image_id TEXT PRIMARY KEY REFERENCES images(image_id),batch_id TEXT NOT NULL,
                    topics TEXT NOT NULL,observation TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','completed')),
                    created_at REAL NOT NULL,completed_at REAL,completed_event TEXT);
                CREATE INDEX IF NOT EXISTS label_review_checks_state ON label_review_checks(state,batch_id);
            """)
            reg.conn.commit()

    @contextmanager
    def registry(self):
        reg = Registry(self.settings.db_path)
        try:
            yield reg
        finally:
            reg.close()

    @staticmethod
    def _label_check(reg, image_id):
        row = reg.conn.execute("SELECT * FROM label_review_checks WHERE image_id=?", (image_id,)).fetchone()
        if row is None:
            return None
        return {"batch_id": row["batch_id"], "topics": json.loads(row["topics"]),
                "observation": row["observation"], "state": row["state"],
                "completed_event": row["completed_event"]}

    def enqueue_label_checks(self, candidates, batch_id):
        """Add inspection tasks without changing annotations or previous decisions.

        Image IDs are stable task identities. Repeating an import, even with a
        different batch name, never resets a completed inspection or its reasons.
        """
        if not isinstance(batch_id, str) or not batch_id.strip() or len(batch_id) > 160:
            raise ReviewError("A label-check batch ID is required")
        if not isinstance(candidates, list) or len(candidates) > 2000:
            raise ReviewError("Label checks must be a list of up to 2000 candidates")
        cleaned = []
        seen = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ReviewError("Invalid label-check candidate")
            image_id = candidate.get("image_id")
            topics = candidate.get("topics")
            observation = candidate.get("observation")
            if not isinstance(image_id, str) or not image_id or image_id in seen:
                raise ReviewError("Label checks require unique existing image IDs")
            if (not isinstance(topics, list) or not topics or len(topics) > 20
                    or any(not isinstance(topic, str) or not topic.strip() or len(topic) > 120 for topic in topics)):
                raise ReviewError("Label checks require a list of inspection topics")
            if not isinstance(observation, str) or not observation.strip() or len(observation) > 4000:
                raise ReviewError("Label checks require an inspection observation")
            seen.add(image_id)
            cleaned.append((image_id, list(dict.fromkeys(topics)), observation))
        inserted = []
        with self.registry() as reg:
            con = reg.conn
            con.execute("BEGIN IMMEDIATE")
            try:
                # Validate every candidate before inserting any task.
                for image_id, _, _ in cleaned:
                    self._check_image(reg, image_id)
                    if current_geometry(reg, image_id) is None:
                        raise ReviewError("The image has no geometry to inspect", 409)
                now = time.time()
                for image_id, topics, observation in cleaned:
                    cursor = con.execute("""INSERT OR IGNORE INTO label_review_checks
                        (image_id,batch_id,topics,observation,state,created_at)
                        VALUES (?,?,?,?,'pending',?)""",
                        (image_id, batch_id, json.dumps(topics), observation, now))
                    if cursor.rowcount:
                        inserted.append(image_id)
                if inserted:
                    con.execute("UPDATE local_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
                con.commit()
            except Exception:
                con.rollback()
                raise
        return {"batch_id": batch_id, "requested": len(cleaned), "enqueued": len(inserted),
                "already_present": len(cleaned) - len(inserted), "image_ids": [item[0] for item in cleaned]}

    def leaderboard(self):
        """Count current human decisions once per photo; deferrals stay separate."""
        with self.registry() as reg:
            rows = reg.conn.execute("""SELECT r.reviewer,i.review_status,count(*) amount
                FROM reviews r JOIN images i USING(image_id)
                WHERE i.status='curated' AND i.locked=1 AND i.review_status IN ('approved','corrected','rejected')
                AND r.id=(SELECT r2.id FROM reviews r2 WHERE r2.image_id=r.image_id ORDER BY r2.created_at DESC,r2.id DESC LIMIT 1)
                GROUP BY lower(trim(r.reviewer)),i.review_status""").fetchall()
            result = {}
            for row in rows:
                name = row['reviewer'].strip() or 'Unnamed'
                item = result.setdefault(name.casefold(), {'reviewer':name,'reviewed':0,'approved':0,'corrected':0,'rejected':0,'review_later':0})
                item[row['review_status']] += row['amount']; item['reviewed'] += row['amount']
            for row in reg.conn.execute("SELECT d.reviewer,count(*) amount FROM review_deferrals d JOIN images i USING(image_id) WHERE i.status='curated' AND " + REVIEW_LATER_ELIGIBLE_CLAUSE + " GROUP BY lower(trim(d.reviewer))"):
                name=row['reviewer'].strip() or 'Unnamed'
                item=result.setdefault(name.casefold(),{'reviewer':name,'reviewed':0,'approved':0,'corrected':0,'rejected':0,'review_later':0})
                item['review_later'] += row['amount']
            return {'reviewers':sorted(result.values(),key=lambda r:(-r['reviewed'],r['reviewer'].casefold())),
                    'meaning':'Current human decisions, one per photo. Skips are separate and are not approvals.'}

    def stats(self):
        with self.registry() as reg:
            rows = reg.images("status='curated'")
            decisions = Counter(r["review_status"] for r in rows)
            splits = {s: dict(Counter(r["review_status"] for r in rows if r["split"] == s)) for s in ("test", "val", "train")}
            deferred = reg.count("SELECT count(*) FROM review_deferrals d JOIN images i USING(image_id) WHERE i.status='curated' AND " + REVIEW_LATER_ELIGIBLE_CLAUSE)
            label_checks_total = reg.count("SELECT count(*) FROM images i WHERE i.status='curated' AND " + LABEL_CHECK_PENDING_CLAUSE)
            label_checks = reg.count("SELECT count(*) FROM images i WHERE i.status='curated' AND " + LABEL_CHECK_PENDING_CLAUSE
                                     + " AND NOT EXISTS(SELECT 1 FROM review_deferrals d WHERE d.image_id=i.image_id)")
            deferred_samples = reg.count("SELECT count(*) FROM review_deferrals d JOIN images i USING(image_id) WHERE i.status='curated' AND i.review_status NOT IN ('approved','corrected','rejected') AND " + queue_clause(state="sample"))
            accepted = decisions["approved"] + decisions["corrected"]
            reviewed = accepted + decisions["rejected"]
            awaiting = reg.count("SELECT count(*) FROM images i WHERE i.status='curated' AND i.review_status NOT IN ('approved','corrected','rejected') AND " + queue_clause(state="awaiting_audit"))
            source_validated = reg.count("SELECT count(*) FROM images i WHERE i.status='curated' AND i.review_status NOT IN ('approved','corrected','rejected') AND " + queue_clause(state="validated"))
            samples = reg.count("SELECT count(*) FROM images i WHERE i.status='curated' AND i.review_status NOT IN ('approved','corrected','rejected') AND " + queue_clause(state="sample"))
            flagged = reg.count("SELECT count(*) FROM images i WHERE i.status='curated' AND i.review_status NOT IN ('approved','corrected','rejected') AND " + queue_clause(state="flagged"))
            repair_samples = reg.count("SELECT count(*) FROM images i WHERE i.status='curated' AND i.review_status NOT IN ('approved','corrected','rejected') AND NOT EXISTS(SELECT 1 FROM review_deferrals d WHERE d.image_id=i.image_id) AND " + REPAIR_SAMPLE_CLAUSE)
            pending = len(rows) - reviewed - awaiting - source_validated
            repair_held = reg.count("SELECT count(*) FROM images i WHERE i.status='curated' AND i.review_status NOT IN ('approved','corrected','rejected') AND "
                                   + queue_clause(state="awaiting_audit")
                                   + " AND EXISTS (SELECT 1 FROM source_conversion_checks c JOIN source_validation_groups g USING(group_key) WHERE c.image_id=i.image_id AND g.state='blocked')")
            # Superseded repair generations remain in the registry as evidence.
            # Show warnings only for groups that still own current unreviewed
            # source checks, rather than historical blocked sample decisions.
            active_groups = [dict(group) for group in reg.conn.execute("""
                SELECT g.stratum,g.state,g.batch_id,count(*) AS unreviewed
                FROM source_validation_groups g
                JOIN source_conversion_checks c ON c.group_key=g.group_key
                JOIN images i ON i.image_id=c.image_id
                WHERE i.status='curated' AND i.review_status NOT IN ('approved','corrected','rejected')
                  AND c.state IN ('sample','awaiting_audit','validated')
                  AND c.geometry_id=(SELECT a.id FROM annotations a WHERE a.image_id=i.image_id AND a.kind='geometry' AND a.producer='geometry' ORDER BY a.created_at DESC,a.id DESC LIMIT 1)
                  AND c.source_id=(SELECT a.id FROM annotations a WHERE a.image_id=i.image_id AND a.kind='source_label' ORDER BY a.created_at DESC,a.id DESC LIMIT 1)
                  AND c.created_at >= (SELECT a.created_at FROM annotations a WHERE a.id=c.geometry_id)
                  AND c.created_at >= (SELECT a.created_at FROM annotations a WHERE a.id=c.source_id)
                GROUP BY g.group_key ORDER BY g.group_key
            """)]
            report = self.settings.path("reports", "preparation.json")
            export = self.settings.path("reports", "reviewed_export.json")
            prep = optional_report(report, {"state": "not_prepared"})
            model_report = self.settings.path("reports", "model_status.json")
            ufld_path = self.settings.path("reports", "ufld_training.json")
            ufld = optional_report(ufld_path, {"phase":"status_unavailable"}) if ufld_path.exists() else None
            queue_path = self.settings.path("reports", "ufld_queue.json")
            ufld_queue = optional_report(queue_path)
            result = {
                "registered": reg.count("SELECT count(*) FROM images"), "curated": len(rows),
                "excluded_by_source_qc": reg.count("SELECT count(*) FROM images WHERE status!='curated'"),
                "approved": decisions["approved"], "corrected": decisions["corrected"], "rejected": decisions["rejected"],
                "accepted": accepted, "reviewed": reviewed, "pending": pending,
                "unreviewed_total": len(rows) - reviewed, "awaiting_source_audit": awaiting, "source_validated": source_validated,
                "source_audit_samples": samples, "active_source_audit_samples": samples - deferred_samples,
                "review_later": deferred, "deferred_source_audit_samples": deferred_samples,
                "label_checks_pending": label_checks, "label_checks_total_pending": label_checks_total,
                "flagged_source_conversions": flagged,
                "conversion_diagnostic_samples": repair_samples,
                "other_manual_pending": max(0, pending - samples - flagged),
                "held_source_repairs": repair_held,
                "source_audit_groups": active_groups,
                "splits": splits, "revision": int(reg.conn.execute("SELECT value FROM local_meta WHERE key='revision'").fetchone()[0]),
                "preparation": prep,
                "export": optional_report(export, {"state": "not_built", "exported": 0}),
                "training": ufld.get("phase", "deferred") if ufld else "deferred", "ufld_training": ufld, "data_root": str(self.settings.root),
                "ufld_queue": ufld_queue,
                "models": optional_report(model_report),
                "paint_proposals": optional_report(self.settings.path("reports", "paint_proposals.json")),
                "dinov3_proposals": optional_report(self.settings.path("reports", "dinov3_proposals.json")),
                "labeling_policy": copy.deepcopy(LABELING_POLICY),
            }
            return result

    @staticmethod
    def _revision(reg, image_id):
        r = reg.conn.execute("SELECT revision FROM review_versions WHERE image_id=?", (image_id,)).fetchone()
        return r[0] if r else 0

    @staticmethod
    def _check_image(reg, image_id):
        row = reg.image(image_id)
        if row is None:
            raise ReviewError("Image not found", 404)
        if row["status"] != "curated":
            raise ReviewError("This image was excluded by source quality checks")
        return row

    def detail(self, image_id):
        with self.registry() as reg:
            row = self._check_image(reg, image_id)
            cur = current_geometry(reg, image_id)
            if cur is None:
                raise ReviewError("No lane proposal exists for this image", 409)
            origin, geometry = cur
            geometry = with_label_policy(geometry)
            teacher_path = self.settings.path("imported", "BrakeCheck_Dataset", "lane_geometry", "teachers", "comma_segnet", image_id + ".json")
            teacher = json.loads(teacher_path.read_text(encoding="utf-8")) if teacher_path.exists() else None
            options = []
            for name, label in (("source_refined", "Source paint · recovered fragments"), ("comma_segnet_fresh", "Road-marking model · fresh"), ("dinov3_seg", "DINOv3 7B · trained decoder"), ("sam3", "SAM 3"), ("sam2_large", "SAM 2.1 Hiera Large · source guided")):
                path = self.settings.path("lane_geometry", "teachers", name, image_id + ".json")
                if path.exists():
                    options.append({"name": name, "label": label, "geometry": json.loads(path.read_text(encoding="utf-8"))})
            if teacher:
                options.append({"name": "comma_segnet", "label": "comma_segnet · archived", "geometry": teacher})
            for option in options:
                candidate = option["geometry"]
                model_meta = candidate.get("model_meta", {})
                edge_records = candidate.get("road_edges")
                source_guided = option["name"] == "source_refined" or candidate.get("source_guided", option["name"] == "sam2_large")
                painted_count = sum(bool(lane.get("slot") and lane.get("points")) for lane in candidate.get("lanes", []))
                edge_count = sum(edge.get("status") == "visible" and len(edge.get("points", [])) >= 2
                                 for edge in edge_records.values()) if isinstance(edge_records, dict) else 0
                empty_reason = candidate.get("empty_reason") or model_meta.get("empty_reason")
                if option["name"] == "sam2_large" and model_meta.get("components") == 0:
                    empty_reason = "No source paint to refine; independent lane inference was not run."
                option.update(version=candidate.get("teacher_version"),
                              capabilities={"painted_lanes": True, "road_edges": isinstance(edge_records, dict) and not source_guided},
                              summary={"painted_lines": painted_count, "road_edges": edge_count,
                                       "inference": "source_guided" if source_guided else "independent", "empty_reason": empty_reason})
            teacher = options[0]["geometry"] if options else None
            q = reg.conn.execute("SELECT * FROM qc WHERE image_id=?", (image_id,)).fetchone()
            crop = make_crop(self.settings.get("export.ufld.crop", {}).get(row["source"], {}), row["width"], row["height"], 1640, 590)
            check = reg.conn.execute("SELECT state,diagnostics FROM source_conversion_checks WHERE image_id=?", (image_id,)).fetchone()
            if check and not reg.conn.execute("SELECT 1 FROM images i WHERE i.image_id=? AND " + queue_clause(state=check["state"]), (image_id,)).fetchone():
                check = {"state": "stale", "diagnostics": check["diagnostics"]}
            return {
                "image_id": image_id, "width": row["width"], "height": row["height"], "split": row["split"],
                "source": row["source"], "source_ref": row["source_ref"], "session_id": row["session_id"],
                "review_later": reg.conn.execute("SELECT 1 FROM review_deferrals WHERE image_id=?", (image_id,)).fetchone() is not None,
                "review_status": row["review_status"], "revision": self._revision(reg, image_id),
                "label_check": self._label_check(reg, image_id),
                "geometry": geometry, "origin": "human" if origin == "human" else "source_derived_proposal",
                "labeling_policy": copy.deepcopy(LABELING_POLICY),
                "teacher": teacher, "teacher_options": options, "tags": json.loads(row["tags"] or "{}"),
                "qc": {**dict(q), "issues": json.loads(q["issues"] or "[]")} if q else None,
                "crop": {"x0": crop.x0, "y0": crop.y0, "x1": crop.x1, "y1": crop.y1},
                "image_url": f"/api/images/{image_id}/photo", "mask_url": f"/api/images/{image_id}/mask",
                "source_paint_available": reg.annotation(image_id, "fused", "fusion") is not None,
                "source_conversion": {"state": check["state"], **json.loads(check["diagnostics"])} if check else None,
            }

    def defer(self, image_id, revision, reviewer, owner, deferred=True):
        """Persist queue routing without making a human annotation decision."""
        with self.registry() as reg:
            con = reg.conn
            con.execute("BEGIN IMMEDIATE")
            try:
                row = self._check_image(reg, image_id)
                label_check = self._label_check(reg, image_id)
                if row["review_status"] in ("approved", "corrected", "rejected") and not (label_check and label_check["state"] == "pending"):
                    raise ReviewError("This photo already has a saved decision", 409)
                if self._revision(reg, image_id) != revision:
                    raise ReviewError("This photo changed. Refresh it before moving it.", 409)
                claim = con.execute("SELECT owner,expires FROM review_claims WHERE image_id=?", (image_id,)).fetchone()
                if claim and claim["owner"] != owner and claim["expires"] > time.time():
                    raise ReviewError("Another reviewer is working on this photo", 409)
                if deferred:
                    con.execute("INSERT INTO review_deferrals VALUES (?,?,?) ON CONFLICT(image_id) DO NOTHING", (image_id, reviewer, time.time()))
                else:
                    con.execute("DELETE FROM review_deferrals WHERE image_id=?", (image_id,))
                con.execute("DELETE FROM review_claims WHERE image_id=?", (image_id,))
                con.execute("UPDATE local_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
                con.commit()
            except Exception:
                con.rollback()
                raise
        return {"image_id": image_id, "review_later": deferred, "revision": revision}

    def renew_claim(self, image_id, revision, owner, *, release=False, replace_previous=False):
        with self.registry() as reg:
            con = reg.conn
            con.execute('BEGIN IMMEDIATE')
            try:
                self._check_image(reg, image_id)
                if release:
                    con.execute('DELETE FROM review_claims WHERE image_id=? AND owner=?', (image_id, owner))
                else:
                    if self._revision(reg, image_id) != revision:
                        raise ReviewError('Another reviewer saved this photo. Your draft is kept; load the latest labels before saving.', 409)
                    claim = con.execute('SELECT * FROM review_claims WHERE image_id=?', (image_id,)).fetchone()
                    if claim and claim['owner'] != owner and claim['expires'] > time.time():
                        raise ReviewError('Another reviewer is working on this photo. Your draft is kept; choose another photo.', 409)
                    if not replace_previous and con.execute('SELECT 1 FROM review_claims WHERE owner=? AND image_id!=? AND expires>?', (owner, image_id, time.time())).fetchone():
                        raise ReviewError('This review has moved to another photo.', 409)
                    if replace_previous:
                        con.execute('DELETE FROM review_claims WHERE owner=? AND image_id!=?', (owner, image_id))
                    con.execute('INSERT OR REPLACE INTO review_claims VALUES(?,?,?)', (image_id, owner, time.time() + 600))
                con.commit()
            except Exception:
                con.rollback()
                raise
        return {'image_id': image_id, 'released': release}

    def next_image(self, owner, split="all", status="pending", exclude=None):
        if split not in ("all", "train", "val", "test") or status not in ("pending", "approved", "corrected", "rejected", "all", "audit_sample", "repair_sample", "flagged", "awaiting_audit", "source_validated", "label_check", "later"):
            raise ReviewError("Invalid queue filter")
        exclude = list(exclude or [])[:2000]
        with self.registry() as reg:
            con = reg.conn
            con.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                con.execute("DELETE FROM review_claims WHERE expires<? OR owner=?", (now, owner))
                clauses = ["i.status='curated'"]
                params = []
                if split != "all":
                    clauses.append("i.split=?")
                    params.append(split)
                if status in ("pending", "audit_sample", "repair_sample", "flagged", "awaiting_audit", "source_validated", "label_check"):
                    clauses.append("NOT EXISTS(SELECT 1 FROM review_deferrals d WHERE d.image_id=i.image_id)")
                if status == "later":
                    clauses += [REVIEW_LATER_ELIGIBLE_CLAUSE, "EXISTS(SELECT 1 FROM review_deferrals d WHERE d.image_id=i.image_id)",
                                "NOT EXISTS(SELECT 1 FROM review_claims c WHERE c.image_id=i.image_id AND c.owner!=? AND c.expires>?)"]
                    params += [owner, now]
                elif status == "label_check":
                    clauses += [LABEL_CHECK_PENDING_CLAUSE,
                                "NOT EXISTS(SELECT 1 FROM review_claims c WHERE c.image_id=i.image_id AND c.owner!=? AND c.expires>?)"]
                    params += [owner, now]
                elif status == "pending":
                    clauses.append("i.review_status NOT IN ('approved','corrected','rejected')")
                    clauses.append(queue_clause())
                    clauses.append("NOT EXISTS(SELECT 1 FROM review_claims c WHERE c.image_id=i.image_id AND c.owner!=? AND c.expires>?)")
                    params += [owner, now]
                elif status in ("audit_sample", "repair_sample", "flagged", "awaiting_audit", "source_validated"):
                    clauses.append("i.review_status NOT IN ('approved','corrected','rejected')")
                    clauses.append(queue_clause(state={"audit_sample": "sample", "source_validated": "validated"}.get(status, status)))
                    if status == "repair_sample":
                        clauses[-1] = REPAIR_SAMPLE_CLAUSE
                    if status in ("audit_sample", "repair_sample", "flagged"):
                        clauses.append("NOT EXISTS(SELECT 1 FROM review_claims c WHERE c.image_id=i.image_id AND c.owner!=? AND c.expires>?)")
                        params += [owner, now]
                elif status != "all":
                    clauses.append("i.review_status=?")
                    params.append(status)
                if exclude:
                    clauses.append(f"i.image_id NOT IN ({','.join('?' for _ in exclude)})")
                    params += exclude
                row = con.execute("SELECT i.image_id FROM images i LEFT JOIN qc q ON i.image_id=q.image_id WHERE " +
                                  " AND ".join(clauses) + " ORDER BY CASE i.split WHEN 'test' THEN 0 WHEN 'val' THEN 1 ELSE 2 END, COALESCE(q.priority,0) DESC, i.image_id LIMIT 1", params).fetchone()
                if row and status in ("pending", "audit_sample", "repair_sample", "flagged", "label_check", "later"):
                    con.execute("INSERT OR REPLACE INTO review_claims VALUES (?,?,?)", (row[0], owner, now + 300))
                con.commit()
            except Exception:
                con.rollback()
                raise
        return self.detail(row[0]) if row else None

    def batch(self, owner, split="all", limit=8, exclude=None, status="pending"):
        if split not in ("all", "train", "val", "test") or not 1 <= limit <= 12 or status not in ("pending", "audit_sample", "repair_sample", "flagged", "label_check", "later"):
            raise ReviewError("Invalid batch filter")
        exclude = list(exclude or [])[:2000]
        with self.registry() as reg:
            con = reg.conn
            con.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                con.execute("DELETE FROM review_claims WHERE expires<?", (now,))
                clauses = ["i.status='curated'",
                           "1=1" if status == "label_check" else REVIEW_LATER_ELIGIBLE_CLAUSE if status == "later"
                           else "i.review_status NOT IN ('approved','corrected','rejected')",
                           (LABEL_CHECK_PENDING_CLAUSE if status == "label_check" else "EXISTS(SELECT 1 FROM review_deferrals d WHERE d.image_id=i.image_id)" if status == "later" else
                            queue_clause() if status == "pending" else REPAIR_SAMPLE_CLAUSE if status == "repair_sample"
                            else queue_clause(state="sample" if status == "audit_sample" else "flagged")),
                           "1=1" if status == "later" else "NOT EXISTS(SELECT 1 FROM review_deferrals d WHERE d.image_id=i.image_id)",
                           "NOT EXISTS(SELECT 1 FROM review_claims c WHERE c.image_id=i.image_id AND c.owner!=? AND c.expires>?)"]
                params = [owner, now]
                if split != "all":
                    clauses.append("i.split=?")
                    params.append(split)
                if exclude:
                    clauses.append(f"i.image_id NOT IN ({','.join('?' for _ in exclude)})")
                    params.extend(exclude)
                rows = con.execute("SELECT i.image_id FROM images i LEFT JOIN qc q ON q.image_id=i.image_id WHERE " +
                                   " AND ".join(clauses) + " ORDER BY CASE i.split WHEN 'test' THEN 0 WHEN 'val' THEN 1 ELSE 2 END, COALESCE(q.priority,0) DESC,i.image_id LIMIT ?",
                                   [*params, limit]).fetchall()
                for row in rows:
                    con.execute("INSERT OR REPLACE INTO review_claims VALUES (?,?,?)", (row[0], owner, now + 600))
                con.commit()
            except Exception:
                con.rollback()
                raise
        return [self.detail(row[0]) for row in rows]

    def _human_geometry(self, source, submitted):
        if not isinstance(submitted, list) or len(submitted) > 4:
            raise ReviewError("Use up to four lane boundaries, or no lanes")
        lanes, slots = [], set()
        for index, item in enumerate(submitted):
            slot = item.get("slot")
            if type(slot) is not int or slot not in (1, 2, 3, 4) or slot in slots:
                raise ReviewError("Each lane needs a different slot from 1 to 4")
            slots.add(slot)
            pts = item.get("points")
            if not isinstance(pts, list) or not 2 <= len(pts) <= 500:
                raise ReviewError("Each lane needs between 2 and 500 points")
            clean = []
            for point in pts:
                if not isinstance(point, (list, tuple)) or len(point) < 2:
                    raise ReviewError("Invalid lane point")
                x, y = point[:2]
                if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                    raise ReviewError("Lane points must be numbers")
                if not math.isfinite(x) or not math.isfinite(y) or not (0 <= x < source["width"] and 0 <= y < source["height"]):
                    raise ReviewError("Lane points must lie inside the image")
                clean.append((float(x), float(y)))
            if len({round(y, 4) for _, y in clean}) != len(clean) or max(y for _, y in clean) - min(y for _, y in clean) < 2:
                raise ReviewError("A lane must span distinct image rows; remove duplicate points")
            kind = item.get("kind", "solid")
            if kind not in ("solid", "dashed", "double"):
                raise ReviewError("Invalid marking type")
            lane = _lane_from_points(clean, {"slot": str(slot), "kind": kind, "physical": "true"}, source["width"], source["height"])
            lane.update(id=index, x_ref=clean[max(range(len(clean)), key=lambda i: clean[i][1])][0], mean_confidence=1.0)
            lanes.append(lane)
        # Preserve the source coordinate system and exact hand-drawn polylines.
        result = copy.deepcopy(source)
        result.update(lanes=lanes, origin="human", mode="human", tags={**source.get("tags", {}), "lane_count": len(lanes)})
        return result

    @staticmethod
    def _snapshot(reg, row):
        ann = reg.annotation(row["image_id"], "human") if row["locked"] and row["review_status"] in ("approved", "corrected") else None
        q = reg.conn.execute("SELECT * FROM qc WHERE image_id=?", (row["image_id"],)).fetchone()
        return {"review_status": row["review_status"], "locked": row["locked"], "human_path": ann["path"] if ann else None,
                "human_licenses": json.loads(ann["license_ids"] or "[]") if ann else [],
                "tags": row["tags"], "qc": dict(q) if q else None,
                "label_check": ReviewStore._label_check(reg, row["image_id"])}

    def decide(self, image_id, action, reviewer, revision, event_id, lanes=None, notes="", seconds=None, owner=None, candidate=None, road_edges=None,
               road_edge_candidate=None, road_edge_candidate_version=None, candidate_version=None, assist_ids=None):
        if action not in ("approved", "rejected", "corrected"):
            raise ReviewError("Choose approved, rejected, or corrected")
        try:
            uuid.UUID(event_id)
        except (ValueError, TypeError, AttributeError):
            raise ReviewError("A valid request ID is required")
        if not reviewer.strip() or len(reviewer) > 80 or len(notes) > 4000:
            raise ReviewError("Invalid reviewer name or notes")
        if seconds is not None and (not math.isfinite(seconds) or not 0 <= seconds <= 86400):
            raise ReviewError("Invalid review duration")
        if candidate_version is not None and (action != "corrected" or not candidate or not isinstance(candidate_version, str) or not candidate_version):
            raise ReviewError("A painted model candidate is required for its version")
        if road_edge_candidate_version is not None and road_edge_candidate is None:
            raise ReviewError("A road-edge model candidate is required for its version")
        if road_edge_candidate is not None:
            if action != "corrected" or road_edges is None:
                raise ReviewError("Save a correction with explicit road edges to adopt a road-edge candidate")
            if road_edge_candidate not in ("sam3", "comma_segnet_fresh", "dinov3_seg"):
                raise ReviewError("Unknown road-edge model candidate")
            if not isinstance(road_edge_candidate_version, str) or not road_edge_candidate_version:
                raise ReviewError("A road-edge model candidate version is required")
        if assist_ids is not None and (action != "corrected" or not isinstance(assist_ids, list) or len(assist_ids) > 50):
            raise ReviewError("Invalid assisted-trace references")
        request_content = {"image_id": image_id, "action": action, "reviewer": reviewer, "revision": revision,
                           "lanes": lanes, "notes": notes, "candidate": candidate}
        # Preserve safe retries of decisions saved by the previous server version.
        legacy_digest = hashlib.sha256(json.dumps(request_content, sort_keys=True).encode()).hexdigest()
        request_content["road_edges"] = road_edges
        # Omitted fields retain fingerprints of requests already saved by older
        # versions, including requests waiting for a phone's network retry.
        for key, value in (("candidate_version", candidate_version), ("road_edge_candidate", road_edge_candidate),
                           ("road_edge_candidate_version", road_edge_candidate_version)):
            if value is not None:
                request_content[key] = value
        if assist_ids:
            request_content["assist_ids"] = assist_ids
        request_digest = hashlib.sha256(json.dumps(request_content, sort_keys=True).encode()).hexdigest()
        with self.registry() as reg:
            con = reg.conn
            con.execute("BEGIN IMMEDIATE")
            try:
                existing = con.execute("SELECT image_id,action,after_state FROM review_events WHERE event_id=?", (event_id,)).fetchone()
                if existing:
                    if existing["image_id"] != image_id or existing["action"] != action:
                        raise ReviewError("Request ID was already used for another decision", 409)
                    original_digest = json.loads(existing["after_state"]).get("request_digest")
                    allowed_digests = {request_digest}
                    if road_edges is None and candidate_version is None and road_edge_candidate is None and road_edge_candidate_version is None and not assist_ids:
                        allowed_digests.add(legacy_digest)
                    if original_digest and original_digest not in allowed_digests:
                        raise ReviewError("This request was already saved with different content. Reload the saved image before changing it.", 409)
                    con.rollback()
                    return {"event_id": event_id, "image_id": image_id, "decision": action, "idempotent": True}
                row = self._check_image(reg, image_id)
                current_revision = self._revision(reg, image_id)
                if revision != current_revision:
                    raise ReviewError("Another reviewer changed this image. Reload before saving.", 409)
                claim = con.execute("SELECT owner,expires FROM review_claims WHERE image_id=?", (image_id,)).fetchone()
                if owner is not None and claim and claim["owner"] != owner and claim["expires"] > time.time():
                    raise ReviewError("Another reviewer is working on this image", 409)
                before = self._snapshot(reg, row)
                cur = current_geometry(reg, image_id)
                if cur is None:
                    raise ReviewError("The image has no geometry to review", 409)
                geo = copy.deepcopy(cur[1])
                if action == "corrected":
                    geo = self._human_geometry(geo, lanes)
                    if road_edges is not None:
                        try:
                            geo["road_edges"] = validate_road_edges(road_edges, row["width"], row["height"])
                        except ValueError as exc:
                            raise ReviewError(str(exc)) from exc
                    if candidate:
                        if candidate not in ("source_refined", "sam3", "sam2_large", "comma_segnet", "comma_segnet_fresh", "dinov3_seg"):
                            raise ReviewError("Unknown model candidate")
                        path = self.settings.path("lane_geometry", "teachers", candidate, image_id + ".json")
                        if candidate == "comma_segnet":
                            path = self.settings.path("imported", "BrakeCheck_Dataset", "lane_geometry", "teachers", candidate, image_id + ".json")
                        if not path.exists():
                            raise ReviewError("This model has no candidate for this image")
                        parent = json.loads(path.read_text(encoding="utf-8"))
                        if candidate_version is not None and parent.get("teacher_version") != candidate_version:
                            raise ReviewError("The painted model candidate changed. Reload it before saving.", 409)
                        meta = parent.get("model_meta", {})
                        geo["candidate_provenance"] = {"name": candidate, "version": parent.get("teacher_version"), "source_guided": parent.get("source_guided", False),
                                                       "repository_revision": meta.get("repository_revision"), "license_sha256": meta.get("license_sha256")}
                        geo["candidate_provenance"].update({key: meta[key] for key in ("checkpoint_sha256", "backbone_revision", "source_sha256", "geometry_sha256", "source_geometry_version") if meta.get(key)})
                        dependencies = set() if candidate == "source_refined" else {"dinov3", "dinov3_seg"} if candidate == "dinov3_seg" else {"sam2" if candidate == "sam2_large" else "comma_segnet" if candidate == "comma_segnet_fresh" else candidate}
                        geo["model_licenses"] = sorted(set(geo.get("model_licenses", [])) | dependencies)
                    if road_edge_candidate is not None:
                        path = self.settings.path("lane_geometry", "teachers", road_edge_candidate, image_id + ".json")
                        if not path.exists():
                            raise ReviewError("This model has no road-edge candidate for this image")
                        parent = json.loads(path.read_text(encoding="utf-8"))
                        if parent.get("teacher_version") != road_edge_candidate_version:
                            raise ReviewError("The road-edge model candidate changed. Reload it before saving.", 409)
                        if not isinstance(parent.get("road_edges"), dict):
                            raise ReviewError("This model candidate has no road-edge labels")
                        try:
                            validate_road_edges(parent["road_edges"], row["width"], row["height"])
                        except ValueError as exc:
                            raise ReviewError("The model road-edge candidate is invalid: " + str(exc)) from exc
                        meta = parent.get("model_meta", {})
                        geo["road_edge_candidate_provenance"] = {
                            "name": road_edge_candidate, "version": parent.get("teacher_version"),
                            "source_guided": parent.get("source_guided", False),
                            "repository_revision": meta.get("repository_revision"), "license_sha256": meta.get("license_sha256"),
                        }
                        geo["road_edge_candidate_provenance"].update({key: meta[key] for key in ("checkpoint_sha256", "backbone_revision") if meta.get(key)})
                        dependencies = {"dinov3", "dinov3_seg"} if road_edge_candidate == "dinov3_seg" else {"comma_segnet" if road_edge_candidate == "comma_segnet_fresh" else road_edge_candidate}
                        geo["model_licenses"] = sorted(set(geo.get("model_licenses", [])) | dependencies)
                    if assist_ids:
                        from .trace_assist import validate_assists
                        assistance, dependencies = validate_assists(self.settings, image_id, revision, owner, assist_ids)
                        geo["assist_provenance"] = geo.get("assist_provenance", []) + assistance
                        geo["model_licenses"] = sorted(set(geo.get("model_licenses", [])) | dependencies)
                human_path = None
                changed, shift = (0, 0.0)
                if action != "rejected":
                    geo = with_label_policy(geo)
                    if action == "corrected":
                        changed, shift = _geometry_shift(cur[1], geo["lanes"])
                    geo.update(image_id=image_id, version="human:" + event_id, reviewer=reviewer, decision=action, reviewed_at=time.time())
                    human_path = self.settings.path("annotations", "reviewed", image_id, event_id + ".json")
                    atomic_json(human_path, geo)
                    con.execute("INSERT INTO annotations (image_id,kind,producer,model_version,license_ids,path,params,created_at) VALUES (?,'human',?,?,?,?,?,?)",
                                (image_id, reviewer, geo["version"], json.dumps([row["license_id"], *geo.get("model_licenses", [])]), str(human_path), json.dumps({"decision": action}), time.time()))
                con.execute("INSERT INTO reviews(image_id,reviewer,decision,seconds,batch,changed_lanes,mean_shift_px,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (image_id, reviewer, action, seconds, "phone-review", changed, shift, notes, time.time()))
                con.execute("UPDATE images SET review_status=?,locked=1,updated_at=? WHERE image_id=?", (action, time.time(), image_id))
                if action != "rejected":
                    tags = json.loads(row["tags"] or "{}")
                    tags["lane_count"] = {"value": sum(bool(l.get("slot")) for l in geo["lanes"]), "origin": "human"}
                    con.execute("UPDATE images SET tags=? WHERE image_id=?", (json.dumps(tags), image_id))
                    con.execute("UPDATE qc SET tier='human',score=1.0,priority=0,issues='[]',geometry_version=?,updated_at=? WHERE image_id=?", (geo["version"], time.time(), image_id))
                con.execute("INSERT INTO review_versions VALUES (?,?) ON CONFLICT(image_id) DO UPDATE SET revision=excluded.revision", (image_id, current_revision + 1))
                con.execute("UPDATE local_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
                con.execute("""UPDATE label_review_checks SET state='completed',completed_at=?,completed_event=?
                    WHERE image_id=? AND state='pending'""", (time.time(), event_id, image_id))
                after = self._snapshot(reg, reg.image(image_id))
                after["request_digest"] = request_digest
                con.execute("INSERT INTO review_events VALUES (?,?,?,?,?,?,?,NULL)", (event_id, image_id, action, reviewer, json.dumps(before), json.dumps(after), time.time()))
                con.execute("DELETE FROM review_deferrals WHERE image_id=?", (image_id,))
                con.execute("DELETE FROM review_claims WHERE image_id=?", (image_id,))
                con.commit()
            except Exception:
                con.rollback()
                raise
        self.sync_category(image_id)
        return {"event_id": event_id, "image_id": image_id, "decision": action, "revision": current_revision + 1}

    def requeue_empty_lanes(self, reviewer="Nick"):
        """Reopen accepted zero-lane photos, retaining geometry and immutable history."""
        reopened = []
        with self.registry() as reg:
            con = reg.conn
            con.execute("BEGIN IMMEDIATE")
            try:
                for row in reg.images("status='curated' AND locked=1 AND review_status IN ('approved','corrected')"):
                    cur = current_geometry(reg, row["image_id"])
                    if cur is None or cur[0] != "human":
                        raise ReviewError("Accepted image has no saved human geometry", 409)
                    if any(lane.get("slot") in (1, 2, 3, 4) for lane in cur[1].get("lanes", [])):
                        continue
                    image_id = row["image_id"]
                    before = self._snapshot(reg, row)
                    event_id = str(uuid.uuid4())
                    geometry = copy.deepcopy(cur[1])
                    geometry.update(version="requeue:" + event_id,
                                    requeue_reason="Recheck zero painted lines; source paint alone may have been mistaken for training labels",
                                    previous_human_path=before["human_path"])
                    path = self.settings.path("lane_geometry", "requeued", image_id, event_id + ".json")
                    atomic_json(path, geometry)
                    # Leave original human files/annotations untouched. The
                    # pending proposal carries the same road-edge corrections.
                    # Write directly while still locked: this is an explicit
                    # user-requested reopen, not a model replacing human labels.
                    con.execute("INSERT INTO annotations(image_id,kind,producer,model_version,license_ids,path,params,created_at) VALUES (?,'geometry','geometry',?,?,?,?,?)",
                                (image_id, geometry["version"], json.dumps(before["human_licenses"]), str(path), json.dumps({"requeue": True}), time.time()))
                    con.execute("UPDATE images SET review_status='pending',locked=0,updated_at=? WHERE image_id=?", (time.time(), image_id))
                    con.execute("UPDATE qc SET tier='needs_review',priority=2,issues=?,geometry_version=?,updated_at=? WHERE image_id=?",
                                (json.dumps([{"code": "recheck_zero_lanes", "message": geometry["requeue_reason"]}]), geometry["version"], time.time(), image_id))
                    con.execute("INSERT INTO review_versions VALUES (?,1) ON CONFLICT(image_id) DO UPDATE SET revision=revision+1", (image_id,))
                    after = self._snapshot(reg, reg.image(image_id))
                    con.execute("INSERT INTO review_events VALUES (?,?,?,?,?,?,?,NULL)",
                                (event_id, image_id, "requeue", reviewer, json.dumps(before), json.dumps(after), time.time()))
                    con.execute("DELETE FROM review_claims WHERE image_id=?", (image_id,))
                    reopened.append({"image_id": image_id, "event_id": event_id})
                if reopened:
                    con.execute("UPDATE local_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
                con.commit()
            except Exception:
                con.rollback()
                raise
        for item in reopened:
            self.sync_category(item["image_id"])
        return reopened

    def undo(self, event_id, reviewer):
        new_event = str(uuid.uuid4())
        with self.registry() as reg:
            con = reg.conn
            con.execute("BEGIN IMMEDIATE")
            try:
                event = con.execute("SELECT * FROM review_events WHERE event_id=?", (event_id,)).fetchone()
                if event is None:
                    raise ReviewError("Decision not found", 404)
                latest = con.execute("SELECT event_id FROM review_events WHERE image_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (event["image_id"],)).fetchone()
                if latest[0] != event_id or event["action"] == "undo":
                    raise ReviewError("A newer decision exists; reload this image before changing it", 409)
                if reviewer != event["reviewer"]:
                    raise ReviewError("Only the reviewer who made this decision can undo it", 403)
                row = reg.image(event["image_id"])
                before = self._snapshot(reg, row)
                restore = json.loads(event["before_state"])
                con.execute("UPDATE images SET review_status=?,locked=?,tags=?,updated_at=? WHERE image_id=?", (restore["review_status"], restore["locked"], restore["tags"], time.time(), row["image_id"]))
                if restore["human_path"]:
                    con.execute("INSERT INTO annotations (image_id,kind,producer,model_version,license_ids,path,params,created_at) VALUES (?,'human',?,?,?,?,?,?)",
                                (row["image_id"], reviewer, "human:undo:" + new_event, json.dumps(restore.get("human_licenses", [row["license_id"]])), restore["human_path"], json.dumps({"undo_of": event_id}), time.time()))
                if restore["qc"]:
                    q = restore["qc"]
                    con.execute("UPDATE qc SET score=?,tier=?,priority=?,issues=?,qc_sample=?,geometry_version=?,updated_at=? WHERE image_id=?", (q["score"], q["tier"], q["priority"], q["issues"], q["qc_sample"], q["geometry_version"], time.time(), row["image_id"]))
                con.execute("INSERT INTO review_versions VALUES (?,1) ON CONFLICT(image_id) DO UPDATE SET revision=revision+1", (row["image_id"],))
                con.execute("UPDATE local_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
                # Only reopen the task this exact event completed. Older events
                # have no label_check snapshot and cannot affect newer tasks.
                label_check = restore.get("label_check")
                if label_check and label_check["state"] == "pending":
                    con.execute("""UPDATE label_review_checks SET state='pending',completed_at=NULL,completed_event=NULL
                        WHERE image_id=? AND batch_id=? AND state='completed' AND completed_event=?""",
                        (row["image_id"], label_check["batch_id"], event_id))
                con.execute("INSERT INTO review_events VALUES (?,?,?,?,?,?,?,?)", (new_event, row["image_id"], "undo", reviewer, json.dumps(before), json.dumps(restore), time.time(), event_id))
                con.execute("DELETE FROM review_claims WHERE image_id=?", (row["image_id"],))
                con.commit()
            except Exception:
                con.rollback()
                raise
        self.sync_category(row["image_id"])
        return {"event_id": new_event, "image_id": row["image_id"], "decision": restore["review_status"]}

    def sync_category(self, image_id):
        """Convenient PC folders, derived from SQLite; safe to rebuild after interruption."""
        with self.registry() as reg:
            row = reg.image(image_id)
            for category in ("human_approved", "human_rejected"):
                split_folder = row['split'] if row['split'] in ('train', 'val', 'test') else 'unassigned'
                folder = self.settings.path("reviewed", category, split_folder)
                stem = folder / image_id
                wanted = (category == "human_approved" and row["review_status"] in ("approved", "corrected")) or (category == "human_rejected" and row["review_status"] == "rejected")
                if wanted:
                    folder.mkdir(parents=True, exist_ok=True)
                    photo = stem.with_suffix(Path(row["path"]).suffix)
                    if not photo.exists():
                        try:
                            os.link(row["path"], photo)
                        except OSError:
                            import shutil
                            shutil.copy2(row["path"], photo)
                    cur = current_geometry(reg, image_id)
                    atomic_json(stem.with_suffix(".json"), {"image_id": image_id, "decision": row["review_status"], "split": row["split"], "source_ref": row["source_ref"], "geometry": cur[1] if cur and category == "human_approved" else None})
                else:
                    for suffix in (".png", ".jpg", ".json"):
                        stem.with_suffix(suffix).unlink(missing_ok=True)

    def backup(self):
        folder = self.settings.path("backups", time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
        folder.mkdir(parents=True)
        with self.registry() as reg:
            dest = sqlite3.connect(folder / "metadata.sqlite")
            try:
                reg.conn.backup(dest)
                dest.row_factory = sqlite3.Row
                events = [dict(r) for r in dest.execute("SELECT * FROM review_events ORDER BY created_at")]
                label_paths = {r[0] for r in dest.execute("SELECT path FROM annotations WHERE kind='human'")}
            finally:
                dest.close()
            atomic_json(folder / "review_events.json", events)
        import zipfile
        with zipfile.ZipFile(folder / "human_labels.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in sorted(label_paths):
                path = Path(item)
                archive.write(path, path.relative_to(self.settings.root))
        return str(folder)
