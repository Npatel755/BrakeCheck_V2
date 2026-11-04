"""Risk-based source conversion review. Never impersonate human approval."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import time

from .bootstrap import atomic_json
from .db import Registry
from .local import local_settings
from .teachers.base import file_sha256

POLICY = "comma-source-conversion-v1"
A2D2_POLICY = "a2d2-source-conversion-v1"
A2D2_REPAIR_POLICY = "a2d2-source-repair-audit-v1"
MIN_SAMPLES_PER_GROUP = 3
MAX_SAMPLES_PER_GROUP = 3
HELD_STATES = ("awaiting_audit", "validated")


def queue_clause(alias="i", state=None):
    """Stale geometry/source records return to manual review automatically."""
    state_clause = "c.state IN ('awaiting_audit','validated')" if state is None else f"c.state='{state}'"
    exists = f"""EXISTS(SELECT 1 FROM source_conversion_checks c WHERE c.image_id={alias}.image_id AND {state_clause}
       AND c.geometry_id=(SELECT a.id FROM annotations a WHERE a.image_id={alias}.image_id AND a.kind='geometry' AND a.producer='geometry' ORDER BY a.created_at DESC,a.id DESC LIMIT 1)
       AND c.source_id=(SELECT a.id FROM annotations a WHERE a.image_id={alias}.image_id AND a.kind='source_label' ORDER BY a.created_at DESC,a.id DESC LIMIT 1)
       AND c.created_at >= (SELECT a.created_at FROM annotations a WHERE a.id=c.geometry_id)
       AND c.created_at >= (SELECT a.created_at FROM annotations a WHERE a.id=c.source_id))"""
    return exists if state is not None else "NOT " + exists


def classify(geometry, qc_codes, teacher=None):
    reasons = sorted(set(qc_codes) - {"no_lanes"})
    reasons.extend("repair_" + warning for warning in geometry.get("source_converter", {}).get("warnings", []))
    slotted = [lane for lane in geometry.get("lanes", []) if lane.get("slot") in (1, 2, 3, 4)]
    if len({lane["slot"] for lane in slotted}) != len(slotted):
        reasons.append("duplicate_slots")
    width, height = geometry["width"], geometry["height"]
    for lane in slotted:
        pts = lane.get("points", [])
        # Fitted curves can legitimately continue past the image border.
        # Export already clips those spans; do not send every road exit to a human.
        inside = [p for p in pts if math.isfinite(p[0]) and math.isfinite(p[1]) and 0 <= p[0] < width and 0 <= p[1] < height]
        if len(inside) < 2 or any(not math.isfinite(p[0]) or not math.isfinite(p[1]) or p[0] < -.5 * width or p[0] > 1.5 * width or p[1] < 0 or p[1] >= height for p in pts):
            reasons.append("invalid_coordinates")
        reference_x = lane.get("x_ref")
        if reference_x is not None and ((lane["slot"] == 2 and reference_x > .5 * width) or (lane["slot"] == 3 and reference_x < .5 * width)):
            reasons.append("ego_slot_side_uncertain")
    if teacher is None:
        reasons.append("missing_model_comparison")
    else:
        model_count = sum(lane.get("slot") in (1, 2, 3, 4) for lane in teacher.get("lanes", []))
        if (not slotted and model_count) or (slotted and not model_count) or abs(len(slotted) - model_count) >= 2:
            reasons.append("model_source_count_disagreement")
    return sorted(set(reasons))


def strata(row, geometry):
    tags = json.loads(row["tags"] or "{}")
    lighting = tags.get("lighting", {}).get("value", "unknown")
    count = sum(lane.get("slot") in (1, 2, 3, 4) for lane in geometry.get("lanes", []))
    converter = geometry.get("source_converter")
    if converter:
        joined = bool(converter.get("joined_chains"))
        recovered = bool(converter.get("recovered_short_chains"))
        risk = "multiple" if joined and recovered else "lane_added" if recovered else "extent_changed" if joined else "unchanged"
        if converter.get('local_continuation_required') and converter.get('original_risk',{}).get('risky'):
            risk = 'reassociated_dashes'
        return f"{row['split']}:risk-{risk}:source-{'positive' if count else 'zero'}"
    return f"{row['split']}:{lighting}:lanes-{min(count,3)}"


def a2d2_strata(row, geometry):
    """Keep conversion cause, lighting and all four lane counts distinct."""
    lighting = json.loads(row["tags"] or "{}").get("lighting", {}).get("value", "unknown")
    count = sum(lane.get("slot") in (1, 2, 3, 4) for lane in geometry.get("lanes", []))
    cause = geometry.get("source_converter", {}).get("risk_family", "unrepaired")
    if geometry.get("tags", {}).get("crosswalk_or_stop_line") or geometry.get("stats", {}).get("transverse", 0):
        cause += "+transverse_present"
    return f"{row['split']}:cause-{cause}:{lighting}:lanes-{count}"


def a2d2_conversion_reasons(geometry):
    """Transverse source paint is normal; promoting it to a lane is unsafe."""
    for lane in geometry.get("lanes", []):
        if lane.get("slot") not in (1, 2, 3, 4):
            continue
        points = [point for point in lane.get("points", [])
                  if all(math.isfinite(value) for value in point[:2])]
        for first, second in zip(points, points[1:]):
            dx, dy = abs(first[0] - second[0]), abs(first[1] - second[1])
            if dx > 6 * max(1., dy):
                return ["transverse_source_promoted_to_boundary"]
    return []


def select_samples(groups, target=64, sample_strata=None, minimum_per_group=MIN_SAMPLES_PER_GROUP,
                   maximum_per_group=MAX_SAMPLES_PER_GROUP, policy=POLICY):
    """Select three per future group by default, or every image in smaller groups.

    The overall target may fill within the explicit group limits; it never
    causes a large group to demand dozens of additional human sample reviews.
    """
    if minimum_per_group < 2:
        raise ValueError("Audit groups require at least two samples when available")
    if maximum_per_group < minimum_per_group:
        raise ValueError("Maximum samples per group must cover the group minimum")
    ordered = {key: sorted(values, key=lambda value: hashlib.sha256((policy + value).encode()).hexdigest()) for key, values in groups.items()}
    if sample_strata:
        # Risk cohorts may span lighting and lane counts. Cover an unseen
        # sub-stratum before taking another image from one already represented.
        buckets = {key: defaultdict(list) for key in groups}
        for key, values in ordered.items():
            for image_id in values:
                buckets[key][sample_strata[image_id]].append(image_id)
        selected = {key: [] for key in groups}
        taken = {key: Counter() for key in groups}

        def take(key):
            remaining = [name for name, ids in buckets[key].items() if taken[key][name] < len(ids)]
            uncovered = [name for name in remaining if not taken[key][name]]
            name = max(uncovered or remaining, key=lambda name: (len(buckets[key][name]) / (taken[key][name] + 1), name))
            selected[key].append(buckets[key][name][taken[key][name]])
            taken[key][name] += 1

        for key in sorted(groups):
            for _ in range(min(minimum_per_group, len(groups[key]))):
                take(key)
        while sum(map(len, selected.values())) < target:
            remaining = [key for key in groups if len(selected[key]) < min(maximum_per_group, len(groups[key]))]
            if not remaining:
                break
            take(max(remaining, key=lambda key: (len(groups[key]) / (len(selected[key]) + 1), key)))
        return selected
    selected = {key: values[:min(minimum_per_group, len(values))] for key, values in ordered.items()}
    # Cover every split/light/lane-count stratum before filling larger groups.
    while sum(map(len, selected.values())) < target:
        remaining = [key for key in ordered if len(selected[key]) < min(maximum_per_group, len(ordered[key]))]
        if not remaining:
            break
        key = max(remaining, key=lambda key: (len(ordered[key]) / (len(selected[key]) + 1), key))
        selected[key].append(ordered[key][len(selected[key])])
    return selected


def refresh_groups(settings):
    """Only actual human sample decisions can release their source cohort."""
    reg = Registry(settings.db_path)
    changed = 0
    try:
        with reg.tx() as con:
            for group in con.execute("SELECT * FROM source_validation_groups").fetchall():
                samples = json.loads(group["sample_ids"])
                states = [reg.image(image_id) for image_id in samples]
                if any(row is None for row in states):
                    continue
                failed_before = any(con.execute("SELECT 1 FROM review_events e JOIN source_conversion_checks c ON c.image_id=e.image_id WHERE e.image_id=? AND e.action IN ('corrected','rejected') AND e.created_at>=c.created_at LIMIT 1", (image_id,)).fetchone() for image_id in samples)
                if failed_before or any(row["review_status"] in ("corrected", "rejected") for row in states):
                    if group["state"] == "blocked":
                        continue
                    con.execute("UPDATE source_validation_groups SET state='blocked',evidence=?,updated_at=? WHERE group_key=?",
                                (json.dumps({"reason": "A sample needed correction or rejection; fix conversion before releasing this cohort", "samples": samples}), time.time(), group["group_key"]))
                    changed += 1
                    con.execute("UPDATE source_conversion_checks SET state='awaiting_audit' WHERE group_key=? AND state='validated'", (group["group_key"],))
                elif states and all(row["locked"] and row["review_status"] == "approved" for row in states):
                    if group["state"] == "passed":
                        continue
                    # Recheck byte fingerprints before granting source eligibility.
                    checks = con.execute("SELECT * FROM source_conversion_checks WHERE group_key=?", (group["group_key"],)).fetchall()
                    valid = all(Path(c["geometry_path"]).is_file() and file_sha256(c["geometry_path"]) == c["geometry_sha256"]
                                and Path(c["source_path"]).is_file() and file_sha256(c["source_path"]) == c["source_sha256"] for c in checks)
                    if not valid:
                        con.execute("UPDATE source_validation_groups SET state='blocked',evidence=?,updated_at=? WHERE group_key=?",
                                    (json.dumps({"reason": "Source or geometry changed during sample audit"}), time.time(), group["group_key"]))
                        changed += 1
                        continue
                    policies = (A2D2_REPAIR_POLICY, A2D2_POLICY, POLICY,
                                'native-trace-repair-audit-v1', 'native-trace-repair-audit-v2')
                    policy = next((name for name in policies
                                   if group['batch_id'].startswith(name + ':')), POLICY)
                    evidence = {"policy": policy, "human_approved_sample_ids": samples, "sample_count": len(samples),
                                "meaning": "Sample-audited source conversions; unsampled images are not human reviewed"}
                    con.execute("UPDATE source_validation_groups SET state='passed',evidence=?,updated_at=? WHERE group_key=?",
                                (json.dumps(evidence), time.time(), group["group_key"]))
                    con.execute("UPDATE source_conversion_checks SET state='validated' WHERE group_key=? AND state='awaiting_audit'", (group["group_key"],))
                    changed += 1
                elif group["state"] == "passed":
                    con.execute("UPDATE source_validation_groups SET state='awaiting_audit',evidence='{}',updated_at=? WHERE group_key=?", (time.time(), group["group_key"]))
                    con.execute("UPDATE source_conversion_checks SET state='awaiting_audit' WHERE group_key=? AND state='validated'", (group["group_key"],))
                    changed += 1
            if changed:
                con.execute("UPDATE local_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
    finally:
        reg.close()


def validated_record(reg, image_id, verify_files=False):
    check = reg.conn.execute("SELECT c.*,g.state AS group_state,g.evidence FROM source_conversion_checks c JOIN source_validation_groups g USING(group_key) WHERE c.image_id=? AND c.state='validated' AND g.state='passed'", (image_id,)).fetchone()
    if check is None:
        return None
    geometry = reg.annotation(image_id, "geometry", "geometry")
    source = reg.annotation(image_id, "source_label")
    if geometry is None or source is None or geometry["id"] != check["geometry_id"] or source["id"] != check["source_id"]:
        return None
    if geometry["path"] != check["geometry_path"] or source["path"] != check["source_path"] or geometry["created_at"] > check["created_at"] or source["created_at"] > check["created_at"]:
        return None
    if verify_files and (not Path(check["geometry_path"]).is_file() or not Path(check["source_path"]).is_file() or file_sha256(check["geometry_path"]) != check["geometry_sha256"] or file_sha256(check["source_path"]) != check["source_sha256"]):
        return None
    return dict(check)


def _has_human_history(reg, image_id):
    return (reg.conn.execute("SELECT 1 FROM review_events WHERE image_id=? LIMIT 1", (image_id,)).fetchone()
            or reg.conn.execute("SELECT 1 FROM reviews WHERE image_id=? LIMIT 1", (image_id,)).fetchone()
            or reg.conn.execute("SELECT 1 FROM annotations WHERE image_id=? AND kind='human' LIMIT 1", (image_id,)).fetchone())


def _live_claim(reg, image_id):
    return reg.conn.execute("SELECT 1 FROM review_claims WHERE image_id=? AND expires>?", (image_id, time.time())).fetchone()


def _assert_a2d2_candidates_current(reg, candidates):
    """A staging transaction must bind exactly the bytes inspected before it."""
    for candidate in candidates:
        image_id = candidate["row"]["image_id"]
        row = reg.image(image_id)
        if (not row or row["source"] != "a2d2" or row["status"] not in ("ingested", "curated")
                or row["locked"] or row["review_status"] in ("approved", "corrected", "rejected")
                or any(row[name] != candidate["row"][name] for name in
                       ("status", "split", "tags", "width", "height", "pool", "license_id", "sha256"))
                or _has_human_history(reg, image_id) or _live_claim(reg, image_id)
                or reg.conn.execute("SELECT 1 FROM source_conversion_checks WHERE image_id=?", (image_id,)).fetchone()):
            raise RuntimeError(f"Audi staging changed during source audit: {image_id}")
        for kind, producer, key, sha_key in (("geometry", "geometry", "geometry", "geometry_sha"),
                                              ("source_label", None, "source", "source_sha")):
            current = reg.annotation(image_id, kind, producer)
            expected = candidate[key]
            if (current is None or any(current[name] != expected[name] for name in ("id", "path", "created_at", "model_version"))
                    or not Path(current["path"]).is_file() or file_sha256(current["path"]) != candidate[sha_key]):
                raise RuntimeError(f"Audi {kind} changed during source audit: {image_id}")


def run(settings, sample_size=64, apply=False, minimum_per_group=MIN_SAMPLES_PER_GROUP,
        maximum_per_group=MAX_SAMPLES_PER_GROUP, *, source_name="comma10k", image_ids=None,
        include_staged=False):
    """Create future source audit cohorts with three samples per group by default."""
    if source_name not in ("comma10k", "a2d2"):
        raise ValueError("Only supported human source-mask datasets can receive conversion audits")
    if include_staged and (source_name != "a2d2" or image_ids is None):
        raise ValueError("Staged audits require an explicit Audi image selection")
    if source_name == "a2d2" and (minimum_per_group != 3 or maximum_per_group != 3):
        raise ValueError("Audi source audits require three samples per group when available")
    selected_ids = None if image_ids is None else set(image_ids)
    policy = A2D2_POLICY if source_name == "a2d2" else POLICY
    reg = Registry(settings.db_path)
    candidates, groups, failures = [], defaultdict(list), Counter()
    try:
        status_where = "status IN ('ingested','curated')" if include_staged else "status='curated'"
        for row in reg.images(status_where + " AND review_status NOT IN ('approved','corrected','rejected')"):
            tags = reg.tags(row)
            if row["source"] != source_name or "review_batch" not in tags or (selected_ids is not None and row["image_id"] not in selected_ids):
                continue
            # Explicit rechecks and all previous human decisions remain manual.
            if reg.conn.execute("SELECT 1 FROM review_events WHERE image_id=? LIMIT 1", (row["image_id"],)).fetchone():
                continue
            if source_name == "a2d2" and (row["locked"] or _has_human_history(reg, row["image_id"]) or _live_claim(reg, row["image_id"])):
                continue
            if reg.conn.execute("SELECT 1 FROM source_conversion_checks WHERE image_id=?", (row["image_id"],)).fetchone():
                continue
            geometry = reg.annotation(row["image_id"], "geometry", "geometry")
            source = reg.annotation(row["image_id"], "source_label")
            if not geometry or not source or json.loads(source["params"] or "{}").get("human") is not True:
                continue
            geo = json.loads(Path(geometry["path"]).read_text())
            q = reg.conn.execute("SELECT * FROM qc WHERE image_id=?", (row["image_id"],)).fetchone()
            codes = [issue["code"] for issue in json.loads(q["issues"] or "[]")] if q else ["missing_qc"]
            if q and q["geometry_version"] != geometry["model_version"]:
                codes.append("stale_qc")
            teacher_path = settings.path("lane_geometry", "teachers", "comma_segnet_fresh", row["image_id"] + ".json")
            teacher = json.loads(teacher_path.read_text()) if teacher_path.exists() else None
            reasons = classify(geo, codes, teacher)
            if source_name == "a2d2":
                reasons = sorted(set(reasons + a2d2_conversion_reasons(geo)))
            stratum = a2d2_strata(row, geo) if source_name == "a2d2" else strata(row, geo)
            group_key = policy + ":" + stratum
            lighting = tags.get("lighting", {}).get("value", "unknown")
            lane_count = sum(lane.get("slot") in (1, 2, 3, 4) for lane in geo.get("lanes", []))
            record = {"row": dict(row), "geometry": dict(geometry), "source": dict(source), "reasons": reasons,
                      "sample_stratum": f"{lighting}:lanes-{lane_count if source_name == 'a2d2' else min(lane_count,3)}",
                      "group": group_key, "stratum": stratum, "geometry_sha": file_sha256(geometry["path"]), "source_sha": file_sha256(source["path"])}
            candidates.append(record)
            if reasons:
                failures.update(reasons)
            else:
                groups[group_key].append(row["image_id"])
        cohort = policy + ":" + hashlib.sha256("\n".join(sorted(c["row"]["image_id"] for c in candidates)).encode()).hexdigest()[:12]
        groups = {cohort + ":" + key.removeprefix(policy + ":"): values for key, values in groups.items()}
        for c in candidates:
            c["group"] = cohort + ":" + c["stratum"]
        samples = select_samples(groups, sample_size, {c["row"]["image_id"]: c["sample_stratum"] for c in candidates},
                                 minimum_per_group, maximum_per_group, policy=policy)
        sample_ids = {value for values in samples.values() for value in values}
        counts = Counter("flagged" if c["reasons"] else "sample" if c["row"]["image_id"] in sample_ids else "awaiting_audit" for c in candidates)
        report = {"policy": policy, "source": source_name, "cohort": cohort, "applied": apply, "counts": dict(counts), "sample_size": len(sample_ids), "groups": len(groups),
                  "minimum_per_group": minimum_per_group, "maximum_per_group": maximum_per_group,
                  "flag_reasons": dict(failures), "meaning": "Only flagged cases and audit samples need individual review. Others stay out of training until sample approval.",
                  "sample_ids": sorted(sample_ids)}
        if apply:
            # One transaction keeps the queue, counters and audit inventory consistent.
            with reg.tx() as con:
                con.execute("BEGIN IMMEDIATE")
                if source_name == "a2d2":
                    _assert_a2d2_candidates_current(reg, candidates)
                for key, ids in samples.items():
                    con.execute("INSERT OR IGNORE INTO source_validation_groups VALUES (?,?,?,?,?,?,?)",
                                (key, cohort, key.removeprefix(cohort + ":"), json.dumps(ids), "awaiting_audit", "{}", time.time()))
                for c in candidates:
                    row = reg.image(c["row"]["image_id"])
                    if row["locked"] or row["review_status"] in ("approved", "corrected", "rejected"):
                        continue
                    state = "flagged" if c["reasons"] else "sample" if row["image_id"] in sample_ids else "awaiting_audit"
                    con.execute("INSERT INTO source_conversion_checks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (row["image_id"], cohort, c["group"], state, c["geometry"]["id"], c["geometry"]["model_version"], c["geometry"]["path"], c["geometry_sha"],
                                 c["source"]["id"], c["source"]["path"], c["source_sha"], json.dumps({"reasons": c["reasons"], "stratum": c["stratum"]}), time.time()))
                if source_name == "a2d2":
                    con.execute("DELETE FROM review_claims WHERE image_id IN (SELECT image_id FROM source_conversion_checks WHERE state='awaiting_audit' AND batch_id=?)", (cohort,))
                else:
                    con.execute("DELETE FROM review_claims WHERE image_id IN (SELECT image_id FROM source_conversion_checks WHERE state='awaiting_audit')")
                con.execute("UPDATE local_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
            atomic_json(settings.path("reports", "source_conversion_triage.json"), report)
        return report
    finally:
        reg.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--sample-size", type=int, default=64)
    args = parser.parse_args()
    if args.sample_size < 1:
        parser.error("Sample size must be positive")
    report = run(local_settings(), args.sample_size, args.apply)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
