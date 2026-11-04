"""Install repair drafts atomically and require a fresh, risk-specific source audit.

This module never writes a human review, unlocks an image, or releases a cohort.
Existing checks and annotations remain available as immutable repair history.
"""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import time

from .bootstrap import atomic_json
from .db import Registry
from .source_validation import MAX_SAMPLES_PER_GROUP, MIN_SAMPLES_PER_GROUP, classify
from .teachers.base import file_sha256


POLICY = "comma-source-repair-audit-v1"
RISKS = {"unchanged", "lane_added", "extent_changed", "shape_changed", "multiple"}
FINAL = {"approved", "corrected", "rejected"}
HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_conversion_repair_history (
    repair_id TEXT PRIMARY KEY,
    image_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    previous_check TEXT NOT NULL,
    previous_geometry TEXT NOT NULL,
    previous_qc TEXT,
    replacement TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _repair_evidence(diagnostics):
    """Bind optional development evidence without treating it as human approval."""
    family = diagnostics.get("risk_family")
    if family is not None and (not isinstance(family, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", family) is None):
        raise ValueError("Conversion risk family must be a short lowercase slug")
    evidence = diagnostics.get("converter_evidence")
    if evidence is None:
        return
    if not isinstance(evidence, dict):
        raise ValueError("Converter evidence must be an object")
    for name in ("converter_code_sha256", "regression_report_sha256"):
        if not isinstance(evidence.get(name), str) or re.fullmatch(r"[a-f0-9]{64}", evidence[name]) is None:
            raise ValueError("Missing or invalid converter evidence: " + name)
    if not isinstance(evidence.get("regression_report_path"), str) or not evidence["regression_report_path"]:
        raise ValueError("Missing converter evidence: regression_report_path")
    code_path = evidence.get("converter_code_path")
    if code_path is not None and (not isinstance(code_path, str) or not code_path):
        raise ValueError("Invalid converter evidence: converter_code_path")
    code = Path(code_path).resolve() if code_path is not None else Path(__file__).with_name("source_converter.py").resolve()
    if code.parent != Path(__file__).resolve().parent or re.fullmatch(r"source_converter[a-z0-9_]*\.py", code.name) is None:
        raise ValueError("Converter evidence code must be a bcdata/source_converter module")
    report = Path(evidence["regression_report_path"])
    if not code.is_file() or file_sha256(code) != evidence["converter_code_sha256"]:
        raise ValueError("Converter code changed after development evaluation")
    if not report.is_file() or file_sha256(report) != evidence["regression_report_sha256"]:
        raise ValueError("Converter regression report changed after development evaluation")


def _latest(con, image_id, kind, producer=None):
    sql = "SELECT * FROM annotations WHERE image_id=? AND kind=?"
    args = [image_id, kind]
    if producer:
        sql += " AND producer=?"
        args.append(producer)
    row = con.execute(sql + " ORDER BY created_at DESC,id DESC LIMIT 1", args).fetchone()
    return dict(row) if row else None


def _ineligible(con, repair, now):
    image_id, previous = repair["image_id"], repair["previous_check"]
    row = con.execute("SELECT * FROM images WHERE image_id=?", (image_id,)).fetchone()
    if row is None or row["status"] != "curated":
        return "not_curated"
    if row["locked"] or row["review_status"] in FINAL:
        return "human_reviewed"
    if con.execute("SELECT 1 FROM review_events WHERE image_id=? LIMIT 1", (image_id,)).fetchone():
        return "has_review_history"
    if con.execute("SELECT 1 FROM review_claims WHERE image_id=? AND expires>?", (image_id, now)).fetchone():
        return "active_reviewer"
    old = con.execute("SELECT * FROM source_conversion_checks WHERE image_id=?", (image_id,)).fetchone()
    if old is None or dict(old) != previous:
        return "check_changed"
    if old["state"] != "awaiting_audit":
        return "not_held"
    group = con.execute("SELECT state FROM source_validation_groups WHERE group_key=?", (old["group_key"],)).fetchone()
    if group is None or group["state"] != "blocked":
        return "not_blocked"
    geometry, source = _latest(con, image_id, "geometry", "geometry"), _latest(con, image_id, "source_label")
    for ann, prefix in ((geometry, "geometry"), (source, "source")):
        if not ann or ann["id"] != old[prefix + "_id"] or ann["path"] != old[prefix + "_path"] or ann["created_at"] > old["created_at"]:
            return prefix + "_changed"
        if not Path(ann["path"]).is_file() or file_sha256(ann["path"]) != old[prefix + "_sha256"]:
            return prefix + "_bytes_changed"
    if json.loads(source["params"] or "{}").get("human") is not True:
        return "source_not_human"
    return None


def _samples(groups, target, minimum_per_group=MIN_SAMPLES_PER_GROUP, maximum_per_group=MAX_SAMPLES_PER_GROUP):
    """Three per risk cohort by default, balanced across original strata."""
    if minimum_per_group < 2:
        raise ValueError("Audit groups require at least two samples when available")
    if maximum_per_group < minimum_per_group:
        raise ValueError("Maximum samples per group must cover the group minimum")
    buckets = {}
    selected = {key: [] for key in groups}
    for key, records in groups.items():
        by_stratum = defaultdict(list)
        for record in records:
            by_stratum[record["original_stratum"]].append(record["image_id"])
        buckets[key] = {
            name: sorted(ids, key=lambda image_id: hashlib.sha256((POLICY + image_id).encode()).hexdigest())
            for name, ids in by_stratum.items()
        }
    taken = {key: Counter() for key in groups}

    def take(key):
        remaining = [name for name, ids in buckets[key].items() if taken[key][name] < len(ids)]
        uncovered = [name for name in remaining if not taken[key][name]]
        pool = uncovered or remaining
        name = max(pool, key=lambda name: (len(buckets[key][name]) / (taken[key][name] + 1), name))
        image_id = buckets[key][name][taken[key][name]]
        taken[key][name] += 1
        selected[key].append(image_id)

    for key in sorted(groups):
        for _ in range(min(minimum_per_group, len(groups[key]))):
            take(key)
    while sum(map(len, selected.values())) < target:
        remaining = [key for key in groups if len(selected[key]) < min(maximum_per_group, len(groups[key]))]
        if not remaining:
            break
        key = max(remaining, key=lambda key: (len(groups[key]) / (len(selected[key]) + 1), key))
        take(key)
    return selected


def requeue_repairs(settings, repairs, sample_target=64, apply=False, minimum_per_group=MIN_SAMPLES_PER_GROUP,
                    maximum_per_group=MAX_SAMPLES_PER_GROUP):
    """Install immutable drafts and replace held checks in one transaction.

    Each repair contains image_id, previous_check (the complete original row),
    geometry_path, geometry_version, diagnostics.risk, and qc fields score, tier,
    priority, issues and qc_sample. Draft files must be distinct from old files.
    Production plans seal geometry_sha256; a supplied fingerprint must match.
    Optional diagnostics.risk_family separates repair causes, lighting and lane
    counts. Optional converter_evidence binds code and regression report bytes;
    it never replaces fresh actual human approval of each cohort's samples.
    Future groups receive exactly three samples by default (all if smaller),
    regardless of the overall sample target. Existing groups remain unchanged.
    Caller creates drafts; this function owns active annotation/QC registration.
    """
    if sample_target < 1:
        raise ValueError("Sample target must be positive")
    if minimum_per_group < 2:
        raise ValueError("Audit groups require at least two samples when available")
    if maximum_per_group < minimum_per_group:
        raise ValueError("Maximum samples per group must cover the group minimum")
    ids = [repair["image_id"] for repair in repairs]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate repair image IDs")
    reg = Registry(settings.db_path)
    prepared, skipped = [], []
    try:
        # Files are read before holding the write lock, then fingerprints and
        # ownership are checked again inside the atomic installation below.
        for repair in repairs:
            _repair_evidence(repair["diagnostics"])
            risk = repair["diagnostics"]["risk"]
            if risk not in RISKS:
                raise ValueError("Unknown conversion risk: " + str(risk))
            image_id = repair["image_id"]
            reason = _ineligible(reg.conn, repair, time.time())
            if reason:
                skipped.append({"image_id": image_id, "reason": reason})
                continue
            path = Path(repair["geometry_path"])
            if path.resolve() == Path(repair["previous_check"]["geometry_path"]).resolve():
                raise ValueError("Repair drafts must not overwrite original geometry")
            draft_bytes = path.read_bytes()
            geometry_sha256 = hashlib.sha256(draft_bytes).hexdigest()
            expected_sha256 = repair.get("geometry_sha256")
            if expected_sha256 is not None and expected_sha256 != geometry_sha256:
                raise ValueError("Repair draft fingerprint changed: " + image_id)
            geo = json.loads(draft_bytes)
            if geo.get("image_id") != image_id or geo.get("version") != repair["geometry_version"]:
                raise ValueError("Repair geometry identity/version mismatch: " + image_id)
            old_geo = json.loads(Path(repair["previous_check"]["geometry_path"]).read_text(encoding="utf-8"))
            row = reg.image(image_id)
            if geo.get("width") != row["width"] or geo.get("height") != row["height"]:
                raise ValueError("Repair geometry dimensions changed: " + image_id)
            teacher_path = settings.path("lane_geometry", "teachers", "comma_segnet_fresh", image_id + ".json")
            teacher = json.loads(teacher_path.read_text(encoding="utf-8")) if teacher_path.is_file() else None
            reasons = classify(geo, [issue["code"] for issue in repair["qc"]["issues"]], teacher)
            original_diagnostics = json.loads(repair["previous_check"]["diagnostics"])
            lighting = json.loads(row["tags"] or "{}").get("lighting", {}).get("value", "unknown")
            old_count = sum(lane.get("slot") in (1, 2, 3, 4) for lane in old_geo.get("lanes", []))
            original_stratum = original_diagnostics.get("stratum", f"{row['split']}:{lighting}:lanes-{min(old_count,3)}")
            stratum = f"{row['split']}:risk-{risk}:source-{'positive' if old_count else 'zero'}"
            family = repair["diagnostics"].get("risk_family")
            if family is not None:
                stratum += f":family-{family}:{lighting}:lanes-{old_count}"
            prepared.append({**repair, "geometry_sha256": geometry_sha256, "reasons": reasons,
                             "stratum": stratum,
                             "original_stratum": original_stratum})
        cohort_digest = hashlib.sha256(_json(sorted((r["image_id"], r["geometry_sha256"], r["stratum"],
                                                     _json(r["diagnostics"].get("converter_evidence")))
                                                    for r in prepared)).encode()).hexdigest()[:16]
        batch_id = POLICY + ":" + cohort_digest
        applied = []
        if apply:
            reg.conn.executescript(HISTORY_SCHEMA)
            reg.conn.commit()
        with reg.tx() as con:
            if apply:
                con.execute("BEGIN IMMEDIATE")
            ready = []
            for repair in prepared:
                _repair_evidence(repair["diagnostics"])
                reason = _ineligible(con, repair, time.time())
                if not reason and file_sha256(repair["geometry_path"]) != repair["geometry_sha256"]:
                    reason = "draft_bytes_changed"
                if reason:
                    skipped.append({"image_id": repair["image_id"], "reason": reason})
                else:
                    ready.append(repair)
            groups = defaultdict(list)
            for repair in ready:
                repair["group_key"] = batch_id + ":" + repair["stratum"]
                if not repair["reasons"]:
                    groups[repair["group_key"]].append(repair)
            samples = _samples(groups, sample_target, minimum_per_group, maximum_per_group)
            sample_ids = {image_id for values in samples.values() for image_id in values}
            counts = Counter("flagged" if r["reasons"] else "sample" if r["image_id"] in sample_ids else "awaiting_audit" for r in ready)
            if apply:
                now = time.time()
                for key, records in groups.items():
                    evidence = {"policy": POLICY, "requires_fresh_human_audit": True,
                                "original_groups": sorted({r["previous_check"]["group_key"] for r in records}),
                                "original_strata": sorted({r["original_stratum"] for r in records}),
                                "risk": records[0]["diagnostics"]["risk"]}
                    if records[0]["diagnostics"].get("risk_family") is not None:
                        evidence["risk_family"] = records[0]["diagnostics"]["risk_family"]
                    development = sorted({_json(r["diagnostics"]["converter_evidence"]) for r in records
                                          if r["diagnostics"].get("converter_evidence") is not None})
                    if development:
                        evidence["converter_evidence"] = [json.loads(value) for value in development]
                    con.execute("INSERT INTO source_validation_groups VALUES (?,?,?,?,?,?,?)",
                                (key, batch_id, records[0]["stratum"], _json(samples[key]), "awaiting_audit", _json(evidence), now))
                for repair in ready:
                    image_id, previous = repair["image_id"], repair["previous_check"]
                    old_ann = _latest(con, image_id, "geometry", "geometry")
                    old_qc = con.execute("SELECT * FROM qc WHERE image_id=?", (image_id,)).fetchone()
                    params = {"repair_policy": POLICY, "previous_geometry_id": old_ann["id"], "risk": repair["diagnostics"]["risk"]}
                    ann = con.execute("INSERT INTO annotations (image_id,kind,producer,model_version,license_ids,path,params,created_at) VALUES (?,'geometry','geometry',?,?,?,?,?)",
                                      (image_id, repair["geometry_version"], old_ann["license_ids"], str(repair["geometry_path"]), _json(params), now))
                    qc = repair["qc"]
                    con.execute("INSERT INTO qc VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(image_id) DO UPDATE SET score=excluded.score,tier=excluded.tier,priority=excluded.priority,issues=excluded.issues,qc_sample=excluded.qc_sample,geometry_version=excluded.geometry_version,updated_at=excluded.updated_at",
                                (image_id, qc["score"], qc["tier"], qc["priority"], _json(qc["issues"]), int(qc["qc_sample"]), repair["geometry_version"], now))
                    state = "flagged" if repair["reasons"] else "sample" if image_id in sample_ids else "awaiting_audit"
                    diagnostics = {**repair["diagnostics"], "reasons": repair["reasons"], "stratum": repair["stratum"],
                                   "original_stratum": repair["original_stratum"], "previous_group": previous["group_key"],
                                   "repair_policy": POLICY, "requires_fresh_human_audit": True}
                    replacement = {"geometry_id": ann.lastrowid, "geometry_version": repair["geometry_version"],
                                   "geometry_path": str(repair["geometry_path"]), "geometry_sha256": repair["geometry_sha256"],
                                   "state": state, "group_key": repair["group_key"], "diagnostics": diagnostics}
                    con.execute("INSERT INTO source_conversion_repair_history VALUES (?,?,?,?,?,?,?,?)",
                                (batch_id + ":" + image_id, image_id, batch_id, _json(previous), _json(old_ann),
                                 _json(dict(old_qc)) if old_qc else None, _json(replacement), now))
                    con.execute("UPDATE source_conversion_checks SET batch_id=?,group_key=?,state=?,geometry_id=?,geometry_version=?,geometry_path=?,geometry_sha256=?,diagnostics=?,created_at=? WHERE image_id=?",
                                (batch_id, repair["group_key"], state, ann.lastrowid, repair["geometry_version"], str(repair["geometry_path"]),
                                 repair["geometry_sha256"], _json(diagnostics), now, image_id))
                    applied.append(image_id)
                if ready:
                    con.execute("UPDATE local_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
            report = {"policy": POLICY, "batch_id": batch_id, "applied": apply, "counts": dict(counts), "groups": len(groups),
                      "sample_target": sample_target, "minimum_per_group": minimum_per_group, "maximum_per_group": maximum_per_group,
                      "sample_ids": sorted(sample_ids), "eligible": len(ready),
                      "applied_ids": applied, "skipped": skipped,
                      "original_groups": sorted({r["previous_check"]["group_key"] for r in ready}),
                      "meaning": "Fresh actual human audit required; original reviews, checks and source masks preserved; no automatic release."}
        if apply:
            atomic_json(settings.path("reports", "source_repair_audits", cohort_digest + ".json"), report)
        return report
    finally:
        reg.close()
