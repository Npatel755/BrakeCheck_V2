"""Stage user-requested sample audits of held source conversions.

These cohorts retain every original warning and unchanged source geometry.
They establish no automatic QC pass. Only actual sample decisions, through
the existing source-validation gate, can release their related conversions.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sqlite3
import time

from .bootstrap import atomic_json
from .conversion_diagnostics import family
from .local import local_settings
from .recheck_flagged_conversions import digest, protected, snapshot
from .review_store import ReviewStore
from .source_validation import select_samples
from .teachers.base import file_sha256

POLICY = "user-requested-held-conversion-audits-v1"
MAXIMUM_GROUP_SIZE = 100
SAMPLES_PER_GROUP = 3


def dependencies():
    package = Path(__file__).resolve().parent
    names = ("held_conversion_audits.py", "conversion_diagnostics.py", "source_validation.py",
             "recheck_flagged_conversions.py", "review_store.py", "bootstrap.py", "db.py",
             "config.py", "teachers/base.py")
    return {str(package / name): file_sha256(package / name) for name in names}


def _seal(settings):
    return {"code_sha256": dependencies(), "config_sha256": digest(settings.cfg),
            "data_root": str(Path(settings.root).resolve()),
            "database_path": str(Path(settings.db_path).resolve())}


def _eligible(con, check, settings):
    if check["state"] != "flagged":
        return None, "not_flagged"
    record = snapshot(con, check, settings)
    image = record["image"]
    if (image["status"] != "curated" or image["pool"] != "commercial"
            or image["source"] not in ("a2d2", "comma10k") or image["split"] not in ("train", "val", "test")):
        return None, "outside_supported_collection"
    if protected(con, image["image_id"]):
        return None, "human_history_claim_or_deferral"
    if record["group"].get("state") == "blocked":
        return None, "previous_group_blocked"
    return record, None


def _fingerprints(record, palette_cache=None):
    check = record["check"]
    result = {}
    for key in ("geometry", "source"):
        annotation = record[key]
        if (not annotation or annotation["id"] != check[key + "_id"]
                or annotation["path"] != check[key + "_path"]
                or key == "geometry" and annotation["model_version"] != check["geometry_version"]):
            raise ValueError("Current annotation no longer matches its conversion binding")
        actual = file_sha256(annotation["path"])
        if actual != check[key + "_sha256"]:
            raise ValueError("Bound source or geometry bytes changed")
        result[annotation["path"]] = actual
    params = json.loads(record["source"]["params"] or "{}")
    if not isinstance(params, dict) or params.get("human") is not True:
        raise ValueError("Source label must be human authored")
    if params.get("palette_path"):
        path = params["palette_path"]
        if not isinstance(path, str) or not isinstance(params.get("palette_sha256"), str):
            raise ValueError("Pinned source palette is invalid")
        # Only the shared palette uses a cache; each image binding is always
        # read independently. Every verification pass starts a fresh cache.
        actual = palette_cache.get(path) if palette_cache is not None else None
        if actual is None:
            actual = file_sha256(path)
            if palette_cache is not None:
                palette_cache[path] = actual
        if actual != params["palette_sha256"]:
            raise ValueError("Pinned source palette bytes changed")
        result[path] = actual
    diagnostics = json.loads(check["diagnostics"])
    if (not isinstance(diagnostics, dict) or not isinstance(diagnostics.get("reasons"), list)
            or any(not isinstance(reason, str) or not reason for reason in diagnostics["reasons"])):
        raise ValueError("Conversion warning reasons are invalid")
    return result


def _cohorts(members):
    buckets = defaultdict(list)
    strata = {}
    metadata = {}
    for member in members:
        image = member["input"]["image"]
        reasons = json.loads(member["input"]["check"]["diagnostics"])["reasons"]
        warning_pattern = json.dumps(sorted(set(reasons)), separators=(",", ":"))
        tags = json.loads(image["tags"] or "{}")
        lighting = tags.get("lighting", {}).get("value", "unknown")
        if not isinstance(lighting, str) or not lighting:
            lighting = "unknown"
        warning_family = family(reasons)
        key = json.dumps([image["source"], image["split"], lighting, warning_family], separators=(",", ":"))
        buckets[key].append(image["image_id"])
        strata[image["image_id"]] = warning_pattern + ":" + str(image["session_id"])
        metadata[key] = {"source": image["source"], "split": image["split"],
                         "lighting": lighting, "family": warning_family}
    cohorts = []
    for key, ids in sorted(buckets.items()):
        ids.sort(key=lambda image_id: digest(POLICY + image_id))
        for offset in range(0, len(ids), MAXIMUM_GROUP_SIZE):
            chunk = ids[offset:offset + MAXIMUM_GROUP_SIZE]
            cohort_key = digest({"stratum": key, "part": offset // MAXIMUM_GROUP_SIZE})[:24]
            selected = select_samples({cohort_key: chunk}, minimum_per_group=SAMPLES_PER_GROUP,
                                      maximum_per_group=SAMPLES_PER_GROUP,
                                      sample_strata=strata, policy=POLICY)[cohort_key]
            counts = Counter(strata[image_id].split(":", 1)[0] for image_id in chunk)
            # Preserve complete warning strings as well as the sampling strata.
            patterns = Counter()
            by_id = {member["image_id"]: member for member in members if member["image_id"] in set(chunk)}
            for image_id in chunk:
                reasons = json.loads(by_id[image_id]["input"]["check"]["diagnostics"])["reasons"]
                patterns[json.dumps(sorted(set(reasons)), separators=(",", ":"))] += 1
            cohorts.append({"key": cohort_key, **metadata[key], "part": offset // MAXIMUM_GROUP_SIZE,
                            "members": chunk, "sample_ids": selected,
                            "warning_patterns": dict(sorted(patterns.items())),
                            "sample_strata": {image_id: strata[image_id] for image_id in chunk}})
    return cohorts


def _identity(seal, members, cohorts):
    return POLICY + ":" + digest({"seal": seal, "members": members, "groups": cohorts})[:20]


def prepare(settings, directory):
    """Freeze eligible current bindings and samples; do not change the registry."""
    directory = Path(directory).resolve()
    if directory.exists():
        raise ValueError("Audit generation already exists")
    seal = _seal(settings)
    con = sqlite3.connect(Path(settings.db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    con.row_factory = sqlite3.Row
    members, excluded = [], Counter()
    palette_cache = {}
    try:
        con.execute("BEGIN")
        checks = con.execute("""SELECT c.* FROM source_conversion_checks c
            JOIN images i USING(image_id) WHERE c.state='flagged' AND i.status='curated'
            ORDER BY c.image_id""").fetchall()
        for check in checks:
            record, reason = _eligible(con, check, settings)
            if record is None:
                excluded[reason] += 1
                continue
            try:
                fingerprints = _fingerprints(record, palette_cache)
            except (OSError, ValueError, TypeError, KeyError):
                excluded["stale_invalid_or_nonhuman_binding"] += 1
                continue
            members.append({"image_id": check["image_id"], "input": record, "fingerprints": fingerprints})
    finally:
        con.close()
    if not members:
        raise ValueError("No eligible held source conversions remain")
    cohorts = _cohorts(members)
    if seal != _seal(settings):
        raise ValueError("Audit code or configuration changed during preparation")
    batch = _identity(seal, members, cohorts)
    groups = [{**group, "group_key": batch + ":" + group["key"]} for group in cohorts]
    samples = sorted(image_id for group in groups for image_id in group["sample_ids"])
    plan = {"policy": POLICY, "batch_id": batch, "seal": seal,
            "member_count": len(members), "group_count": len(groups), "sample_count": len(samples),
            "maximum_group_size": MAXIMUM_GROUP_SIZE, "samples_per_group": SAMPLES_PER_GROUP,
            "members": members, "groups": groups, "sample_ids": samples,
            "excluded_counts": dict(excluded), "raw_flagged_curated_count": len(checks),
            "warning_mode": "manual_human_sampling", "automatic_source_qc_passed": False,
            "human_decisions_created": 0, "cohorts_released": 0, "training_held": True,
            "meaning": "Held source conversions sampled at the user's request; warnings and labels are unchanged.",
            "created_at": time.time()}
    directory.mkdir(parents=True, exist_ok=False)
    atomic_json(directory / "plan.json", plan)
    return plan


def _verify_plan(settings, plan):
    if (plan.get("policy") != POLICY or plan.get("seal") != _seal(settings)
            or plan.get("maximum_group_size") != MAXIMUM_GROUP_SIZE
            or plan.get("samples_per_group") != SAMPLES_PER_GROUP):
        raise ValueError("Audit plan policy, code or configuration changed")
    members = plan["members"]
    if (not isinstance(members, list) or not members
            or len({member["image_id"] for member in members}) != len(members)
            or any(member["image_id"] != member["input"]["image"]["image_id"] for member in members)):
        raise ValueError("Audit plan has invalid or duplicate members")
    cohorts = _cohorts(members)
    batch = _identity(plan["seal"], members, cohorts)
    groups = [{**group, "group_key": batch + ":" + group["key"]} for group in cohorts]
    samples = sorted(image_id for group in groups for image_id in group["sample_ids"])
    if (plan["batch_id"] != batch or plan["groups"] != groups or plan["sample_ids"] != samples
            or plan["member_count"] != len(members) or plan["group_count"] != len(groups)
            or plan["sample_count"] != len(samples)
            or plan.get("warning_mode") != "manual_human_sampling"
            or plan.get("automatic_source_qc_passed") is not False):
        raise ValueError("Audit selection or cohort sampling changed")
    return members, groups, set(samples)


def apply(settings, directory):
    """Atomically stage unchanged conversions behind fresh human sample audits."""
    directory = Path(directory).resolve()
    path = directory / "plan.json"
    plan_sha = file_sha256(path)
    plan = json.loads(path.read_text(encoding="utf-8"))
    if (directory / "applied.json").exists():
        raise ValueError("Audit generation already applied")
    members, groups, samples = _verify_plan(settings, plan)
    backup = ReviewStore(settings).backup()
    con = sqlite3.connect(settings.db_path, timeout=60)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA_STATEMENTS:
            con.execute(statement)
        if con.execute("SELECT 1 FROM held_conversion_audit_batches WHERE batch_id=?", (plan["batch_id"],)).fetchone():
            raise ValueError("Audit batch already applied")
        palette_cache = {}
        for member in members:
            image_id = member["image_id"]
            check = con.execute("SELECT * FROM source_conversion_checks WHERE image_id=?", (image_id,)).fetchone()
            if check is None:
                raise ValueError("Audit member conversion binding disappeared: " + image_id)
            record, reason = _eligible(con, check, settings)
            if record is None or record != member["input"]:
                raise ValueError("Audit member changed or became protected: " + image_id)
            if _fingerprints(record, palette_cache) != member["fingerprints"]:
                raise ValueError("Audit member bound files changed: " + image_id)
        now = time.time()
        group_for = {}
        for group in groups:
            evidence = {"policy": POLICY, "warning_mode": "manual_human_sampling",
                        "automatic_source_qc_passed": False, "plan_path": str(path),
                        "plan_sha256": plan_sha, "maximum_group_size": MAXIMUM_GROUP_SIZE,
                        "members": len(group["members"]), "warning_family": group["family"],
                        "warning_patterns": group["warning_patterns"],
                        "requires_fresh_human_audit": True,
                        "meaning": "User-requested sample audit of held warnings; no automatic QC pass."}
            con.execute("INSERT INTO source_validation_groups VALUES (?,?,?,?,?,?,?)",
                        (group["group_key"], plan["batch_id"], group["key"], json.dumps(group["sample_ids"]),
                         "awaiting_audit", json.dumps(evidence), now))
            group_for.update({image_id: group["group_key"] for image_id in group["members"]})
        for member in members:
            image_id, previous = member["image_id"], member["input"]
            diagnostics = json.loads(previous["check"]["diagnostics"])
            diagnostics["held_conversion_audit"] = {
                "policy": POLICY, "warning_mode": "manual_human_sampling",
                "automatic_source_qc_passed": False, "plan_path": str(path), "plan_sha256": plan_sha,
                "previous_batch_id": previous["check"]["batch_id"], "previous_group_key": previous["check"]["group_key"],
                "requires_fresh_human_audit": True}
            state = "sample" if image_id in samples else "awaiting_audit"
            replacement = {"batch_id": plan["batch_id"], "group_key": group_for[image_id],
                           "state": state, "diagnostics": diagnostics}
            con.execute("""UPDATE source_conversion_checks SET batch_id=?,group_key=?,state=?,diagnostics=?
                WHERE image_id=?""", (replacement["batch_id"], replacement["group_key"], state,
                                       json.dumps(diagnostics), image_id))
            con.execute("INSERT INTO held_conversion_audit_history VALUES (?,?,?,?,?)",
                        (image_id, plan["batch_id"], json.dumps(previous), json.dumps(replacement), now))
            con.execute("INSERT INTO review_versions VALUES (?,1) ON CONFLICT(image_id) DO UPDATE SET revision=revision+1", (image_id,))
        con.execute("UPDATE local_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        receipt = {"policy": POLICY, "batch_id": plan["batch_id"], "seal": plan["seal"],
                   "plan_path": str(path), "plan_sha256": plan_sha, "backup": backup,
                   "installed": len(members), "groups": len(groups), "sample_ids": sorted(samples),
                   "sample_count": len(samples), "awaiting_audit": len(members) - len(samples),
                   "warning_mode": "manual_human_sampling", "automatic_source_qc_passed": False,
                   "human_decisions_created": 0, "cohorts_released": 0, "training_held": True,
                   "created_at": now}
        con.execute("INSERT INTO held_conversion_audit_batches VALUES (?,?,?)", (plan["batch_id"], json.dumps(receipt), now))
        # File content is outside SQLite's lock. Recheck all image bindings and
        # the shared palette with a fresh cache immediately before committing.
        palette_cache = {}
        for member in members:
            if _fingerprints(member["input"], palette_cache) != member["fingerprints"]:
                raise ValueError("Bound audit bytes changed during staging")
        if file_sha256(path) != plan_sha or _seal(settings) != plan["seal"]:
            raise ValueError("Audit plan, code or configuration changed during staging")
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    atomic_json(directory / "applied.json", receipt)
    return receipt


_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS held_conversion_audit_history (
        image_id TEXT NOT NULL,batch_id TEXT NOT NULL,previous_state TEXT NOT NULL,
        replacement TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(image_id,batch_id))""",
    """CREATE TABLE IF NOT EXISTS held_conversion_audit_batches (
        batch_id TEXT PRIMARY KEY,receipt TEXT NOT NULL,created_at REAL NOT NULL)""",
    """CREATE TRIGGER IF NOT EXISTS held_audit_history_no_update BEFORE UPDATE ON held_conversion_audit_history
        BEGIN SELECT RAISE(ABORT,'Immutable held conversion audit history'); END""",
    """CREATE TRIGGER IF NOT EXISTS held_audit_history_no_delete BEFORE DELETE ON held_conversion_audit_history
        BEGIN SELECT RAISE(ABORT,'Immutable held conversion audit history'); END""",
    """CREATE TRIGGER IF NOT EXISTS held_audit_batches_no_update BEFORE UPDATE ON held_conversion_audit_batches
        BEGIN SELECT RAISE(ABORT,'Immutable held conversion audit receipt'); END""",
    """CREATE TRIGGER IF NOT EXISTS held_audit_batches_no_delete BEFORE DELETE ON held_conversion_audit_batches
        BEGIN SELECT RAISE(ABORT,'Immutable held conversion audit receipt'); END""",
)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = apply(local_settings(), args.generation) if args.apply else prepare(local_settings(), args.generation)
    print(json.dumps({key: value for key, value in result.items() if key not in ("members", "groups")
                      or not isinstance(value, list)}, allow_nan=False), flush=True)
