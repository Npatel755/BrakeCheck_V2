"""Stable, representative conversion-cohort audits; never training clearance."""
from __future__ import annotations

from collections import defaultdict
import json

from .zod_mirror import CLASSES, encoded, sha

POLICY = "zod-conversion-cohorts-v1"


def schema(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS zod_audit_plan (policy TEXT PRIMARY KEY, frame_count INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS zod_audit_groups (
        group_key TEXT PRIMARY KEY, description TEXT NOT NULL, member_count INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS zod_audit_members (
        image_id TEXT PRIMARY KEY, group_key TEXT NOT NULL, geometry_sha TEXT NOT NULL,
        is_sample INTEGER NOT NULL, repair_required INTEGER NOT NULL DEFAULT 0);
      CREATE INDEX IF NOT EXISTS zod_group_samples ON zod_audit_members(is_sample,image_id);
      CREATE INDEX IF NOT EXISTS zod_group_members ON zod_audit_members(group_key);
      CREATE TABLE IF NOT EXISTS zod_conversion_findings (
        image_id TEXT PRIMARY KEY, finding TEXT NOT NULL, origin TEXT NOT NULL);
    """)


def ensure_plan(db):
    schema(db)
    total = db.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    previous = db.execute("SELECT frame_count FROM zod_audit_plan WHERE policy=?", (POLICY,)).fetchone()
    if previous and previous[0] == total:
        return
    migrated = db.execute("SELECT 1 FROM sqlite_master WHERE name='zod_clearance_plan'").fetchone()
    if migrated and db.execute('SELECT 1 FROM zod_clearance_plan').fetchone():
        raise ValueError('Published cause-separated audits cannot be silently replanned after an import')
    # Freeze membership before presenting samples. Exact warning signatures must
    # not let ordinary geometry stand in for missing/invalid source annotations.
    groups = defaultdict(list)
    for row in db.execute("SELECT image_id,geometry_json,geometry_sha,qc_json,action,correction_json FROM frames ORDER BY image_id"):
        geometry = json.loads(row["geometry_json"])
        classes = sorted(k for k, v in geometry.get("source_class_counts", {}).items() if v and k in CLASSES)
        signature = encoded([geometry.get("version"), geometry.get("coordinate_transform"),
                             classes, len(geometry["lanes"]), sorted(json.loads(row["qc_json"]))])
        groups[signature].append((row["image_id"], row["geometry_sha"], bool(row["action"]),
                                  bool(row["correction_json"])))
    db.execute("BEGIN IMMEDIATE")
    db.execute("DELETE FROM zod_audit_members")
    db.execute("DELETE FROM zod_audit_groups")
    for signature, members in sorted(groups.items()):
        key = "conversion/" + sha(signature.encode())[:20]
        values = json.loads(signature)
        description = f"{', '.join(values[2]) or 'no lane paint classes'}; {values[3]} slots; " + (", ".join(values[4]) or "no converter warnings")
        # Preserve existing human decisions as audit evidence, including samples
        # inspected before this plan existed. Normally first/middle/last = 3.
        selected = {member[0] for member in members if member[2] or member[3]}
        for index in (0, len(members) // 2, len(members) - 1):
            if len(selected) >= min(3, len(members)):
                break
            selected.add(members[index][0])
        db.execute("INSERT INTO zod_audit_groups VALUES(?,?,?)", (key, description, len(members)))
        db.executemany("INSERT INTO zod_audit_members VALUES(?,?,?,?,?)",
                       [(image_id, key, digest, int(image_id in selected), int(corrected))
                        for image_id, digest, reviewed, corrected in members])
    db.execute("INSERT OR REPLACE INTO zod_audit_plan VALUES(?,?)", (POLICY, total))
    db.execute("UPDATE zod_audit_members SET repair_required=1 WHERE image_id IN (SELECT image_id FROM zod_conversion_findings)")
    db.commit()


def record_finding(db, image_id, finding, origin):
    """Record a diagnosed issue without inventing a human approval/exclusion."""
    ensure_plan(db)
    if not db.execute("SELECT 1 FROM zod_audit_members WHERE image_id=?", (image_id,)).fetchone():
        raise ValueError("Unknown ZOD photo")
    db.execute("INSERT OR REPLACE INTO zod_conversion_findings VALUES(?,?,?)", (image_id, finding, origin))
    db.execute("UPDATE zod_audit_members SET repair_required=1 WHERE image_id=?", (image_id,))
    db.commit()


def states(db, group_key=None):
    """A failed/edited member holds its entire cohort, including non-samples."""
    result = {}
    query = """
      SELECT m.group_key, m.is_sample, m.repair_required, m.geometry_sha AS planned_sha,
             f.image_id,f.image_ready,f.action,f.geometry_sha,f.native_sha,f.image_sha,
             f.correction_sha,f.decision_fingerprint
      FROM zod_audit_members m JOIN frames f ON f.image_id=m.image_id
      WHERE (m.is_sample=1 OR m.repair_required=1)
    """
    if group_key:
        query += " AND m.group_key=?"
    for row in db.execute(query, (group_key,) if group_key else ()):
        state = result.setdefault(row["group_key"], {"samples": 0, "approved": 0, "blocked": False})
        fingerprint = sha(encoded([row["image_id"], row["native_sha"], row["image_sha"],
                                   row["geometry_sha"], row["correction_sha"]]).encode())
        current = row["decision_fingerprint"] == fingerprint
        if row["repair_required"] or row["geometry_sha"] != row["planned_sha"] or (row["action"] == "exclude" and current):
            state["blocked"] = True
        if row["is_sample"]:
            state["samples"] += 1
            state["approved"] += int(bool(row["image_ready"] and row["action"] == "approve" and current))
    for value in result.values():
        value["state"] = "needs_repair" if value["blocked"] else "audited" if value["samples"] == value["approved"] else "awaiting_audit"
    return result
