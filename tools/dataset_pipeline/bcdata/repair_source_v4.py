"""Stage source-only V4 repairs behind a new, actual human sample audit.

Preparation writes immutable drafts and a unique plan, never active labels.
Installation delegates the atomic, race-safe review gates to source_repair_audit.
Neither step changes human decisions, source masks or frozen training datasets.
"""
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time
import uuid

from .bootstrap import atomic_json
from .db import Registry
from .local import local_settings
from .qc import check, tier_for
from .repair_source_conversions import conversion_risk
from .source_boundary_maps import load_boundary_maps
from .source_converter_v4 import VERSION, repair_geometry_v4
from .source_repair_audit import requeue_repairs
from .teachers.base import file_sha256


_PACKAGE = Path(__file__).resolve().parent
_DEPENDENCIES = ("source_converter.py", "source_converter_v3.py", "geometry.py",
                 "source_boundary_maps.py", "teachers/source_labels.py", "quality.py",
                 "sources/comma10k.py")


def verify_regression_report(path):
    """Reject stale code, incomplete safety evidence or unresolved known defects."""
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    code = _PACKAGE / "source_converter_v4.py"
    if (report.get("converter_version") != VERSION
            or Path(report.get("converter_code_path", "")).resolve() != code
            or report.get("converter_code_sha256") != file_sha256(code)):
        raise ValueError("Regression report belongs to a different converter")
    checks = report.get("safety_checks", {})
    regressions = checks.get("approved_sample_regressions")
    if (type(regressions) is not int or regressions != 0
            or checks.get("original_annotations_unchanged") is not True):
        raise ValueError("Regression evidence has unresolved annotation regressions")
    if (checks.get("converter_and_dependencies_stable") is not True
            or checks.get("source_fingerprints_verified") is not True):
        raise ValueError("Regression evidence was not verified")
    dependencies = report.get("dependency_sha256")
    if not isinstance(dependencies, dict):
        raise ValueError("Missing converter dependency fingerprints")
    resolved = {str(Path(name).resolve()): digest for name, digest in dependencies.items()}
    if not {str((_PACKAGE / name).resolve()) for name in _DEPENDENCIES}.issubset(resolved):
        raise ValueError("Missing converter dependency fingerprints")
    for dependency, expected in resolved.items():
        if not Path(dependency).is_file() or file_sha256(dependency) != expected:
            raise ValueError("Converter dependency changed after regression checks")
    evaluator_path = report.get("evaluator_code_path")
    if (not isinstance(evaluator_path, str) or not Path(evaluator_path).is_file()
            or file_sha256(evaluator_path) != report.get("evaluator_code_sha256")):
        raise ValueError("Evaluator changed after regression checks")
    known = report.get("known_failure_checks")
    unresolved = report.get("unresolved_known_failures")
    def flagged(item):
        warnings = item.get("warnings")
        return (item.get("flagged") is True and isinstance(warnings, list) and bool(warnings)
                and all(isinstance(warning, str) and bool(warning) for warning in warnings))
    if (checks.get("known_failures_resolved_or_flagged") is not True
            or not isinstance(known, list) or not known
            or any(not isinstance(item, dict) or item.get("status") not in ("passed", "flagged")
                   or (item["status"] == "flagged" and not flagged(item)) for item in known)
            or not isinstance(unresolved, list)
            or any(not isinstance(item, dict) or not flagged(item) for item in unresolved)):
        raise ValueError("Regression evidence has unresolved known failures")
    return report


def _evidence(path):
    report = verify_regression_report(path)
    code = _PACKAGE / "source_converter_v4.py"
    return {"converter_code_path": str(code), "converter_code_sha256": file_sha256(code),
            "regression_report_path": str(Path(path).resolve()),
            "regression_report_sha256": file_sha256(path),
            "dependency_sha256": report["dependency_sha256"],
            "evaluator_code_path": report["evaluator_code_path"],
            "evaluator_code_sha256": report["evaluator_code_sha256"]}


def _write_immutable(path, value):
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != value:
            raise ValueError("Immutable V4 draft or plan conflict")
    else:
        atomic_json(path, value)


def prepare(settings, regression_report, limit=None):
    """Prepare only currently held, blocked, unreviewed and unclaimed images."""
    if limit is not None and limit < 1:
        raise ValueError("Limit must be positive")
    evidence = _evidence(regression_report)
    output = settings.path("lane_geometry", "source_repairs", VERSION)
    output.mkdir(parents=True, exist_ok=True)
    reg = Registry(settings.db_path)
    drafts, errors = [], []
    try:
        rows = reg.conn.execute("""SELECT c.* FROM source_conversion_checks c
            JOIN source_validation_groups g USING(group_key) JOIN images i ON c.image_id=i.image_id
            WHERE c.state='awaiting_audit' AND g.state='blocked' AND i.status='curated'
            AND i.source='comma10k' AND i.locked=0
            AND i.review_status NOT IN ('approved','corrected','rejected')
            AND NOT EXISTS(SELECT 1 FROM review_events e WHERE e.image_id=c.image_id)
            AND NOT EXISTS(SELECT 1 FROM annotations a WHERE a.image_id=c.image_id AND a.kind='human')
            AND NOT EXISTS(SELECT 1 FROM review_claims r WHERE r.image_id=c.image_id AND r.expires>?)
            ORDER BY c.image_id""", (time.time(),)).fetchall()
        if limit is not None:
            rows = rows[:limit]
        for index, row in enumerate(rows):
            previous = dict(row)
            image_id = previous["image_id"]
            try:
                source = reg.annotation(image_id, "source_label")
                geometry = reg.annotation(image_id, "geometry", "geometry")
                for ann, name in ((source, "source"), (geometry, "geometry")):
                    if (ann is None or ann["id"] != previous[name + "_id"]
                            or ann["path"] != previous[name + "_path"]
                            or ann["created_at"] > previous["created_at"]
                            or file_sha256(ann["path"]) != previous[name + "_sha256"]):
                        raise ValueError(name.title() + " changed after audit")
                if json.loads(source["params"] or "{}").get("human") is not True:
                    raise ValueError("Source label is not human annotated")
                original = json.loads(Path(previous["geometry_path"]).read_text(encoding="utf-8"))
                maps = load_boundary_maps(source)
                if any(maps[name].shape != (original["height"], original["width"])
                       for name in ("lane", "my_car", "movable")):
                    raise ValueError("Source dimensions changed")
                repaired = repair_geometry_v4(deepcopy(original), maps["lane"], maps["my_car"], maps["movable"])
                repaired.update(image_id=image_id, mode="source_label")
                version_hash = hashlib.sha256(json.dumps(
                    {"previous_geometry": previous["geometry_sha256"], "source": previous["source_sha256"],
                     "evidence": evidence}, sort_keys=True).encode()).hexdigest()[:20]
                repaired["version"] = VERSION + ":" + version_hash
                diagnostics = conversion_risk(original, repaired)
                metadata = repaired.get("source_converter", {})
                diagnostics.update(risk_family=metadata.get("risk_family", "unclassified"),
                                   converter_evidence=evidence)
                repaired["conversion_repair"] = diagnostics
                score, issues = check(repaired, {"mode": "source_label"}, maps["lane"], None, settings)
                issues = list(issues)
                warnings = metadata.get("warnings", [])
                if not isinstance(warnings, list) or any(not isinstance(warning, str) for warning in warnings):
                    raise ValueError("Invalid converter warning metadata")
                for warning in warnings:
                    if not any(issue.get("code") == "repair_" + warning for issue in issues):
                        issues.append({"code": "repair_" + warning})
                if diagnostics["risk_family"] == "unclassified":
                    issues.append({"code": "repair_unclassified_cause"})
                path = output / (image_id + "-" + version_hash + ".json")
                _write_immutable(path, repaired)
                drafts.append({"image_id": image_id, "previous_check": previous,
                               "geometry_path": str(path), "geometry_version": repaired["version"],
                               "geometry_sha256": file_sha256(path), "diagnostics": diagnostics,
                               "qc": {"score": score, "tier": tier_for(score, issues, "source_label", settings),
                                      "priority": 1-score, "issues": issues, "qc_sample": False}})
            except (OSError, ValueError, KeyError, TypeError) as error:
                errors.append({"image_id": image_id, "error": str(error)})
            if (index + 1) % 100 == 0:
                print(json.dumps({"phase": "preparing_source_v4", "processed": index+1,
                                  "total": len(rows), "errors": len(errors)}), flush=True)
    finally:
        reg.close()
    if _evidence(regression_report) != evidence:
        raise ValueError("Converter evidence changed during plan preparation")
    report = requeue_repairs(settings, drafts, apply=False)
    plan = {"converter": VERSION, "converter_evidence": evidence, "repairs": drafts,
            "errors": errors}
    plan_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:10]
    plan_path = settings.path("reports", "source_v4_repairs", plan_id + ".plan.json")
    _write_immutable(plan_path, plan)
    report.update(converter=VERSION, plan_path=str(plan_path), errors=errors,
                  risk_families=dict(Counter(d["diagnostics"]["risk_family"] for d in drafts)),
                  geometry_changed=sum(d["diagnostics"]["risk"] != "unchanged" for d in drafts))
    atomic_json(plan_path.with_suffix(".report.json"), report)
    atomic_json(settings.path("reports", "source_v4_repairs.json"), report)
    return report


def apply_plan(settings, path):
    """Install unchanged sealed drafts; shared gates skip new reviews or claims."""
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    if plan.get("converter") != VERSION or plan.get("errors") != []:
        raise ValueError("Prepared V4 plan has errors or a different converter")
    evidence = plan["converter_evidence"]
    if file_sha256(evidence["regression_report_path"]) != evidence["regression_report_sha256"]:
        raise ValueError("Regression report changed after plan preparation")
    if _evidence(evidence["regression_report_path"]) != evidence:
        raise ValueError("Converter evidence changed after plan preparation")
    for draft in plan["repairs"]:
        if (draft.get("diagnostics", {}).get("converter_evidence") != evidence
                or file_sha256(draft["geometry_path"]) != draft.get("geometry_sha256")):
            raise ValueError("Prepared V4 geometry or evidence changed")
    report = requeue_repairs(settings, plan["repairs"], apply=True)
    applied = set(report["applied_ids"])
    report.update(converter=VERSION, plan_path=str(path), errors=[],
                  risk_families=dict(Counter(d["diagnostics"]["risk_family"] for d in plan["repairs"]
                                             if d["image_id"] in applied)),
                  planned_geometry_changed=sum(d["diagnostics"]["risk"] != "unchanged" for d in plan["repairs"]),
                  geometry_changed=sum(d["diagnostics"]["risk"] != "unchanged" for d in plan["repairs"]
                                       if d["image_id"] in applied))
    atomic_json(Path(path).with_suffix(".applied.json"), report)
    atomic_json(settings.path("reports", "source_v4_repairs.json"), report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regression-report", type=Path)
    parser.add_argument("--plan-file", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("Limit must be positive")
    if args.apply:
        if not args.plan_file:
            parser.error("--apply requires --plan-file")
        report = apply_plan(local_settings(), args.plan_file)
    else:
        if not args.regression_report or args.plan_file:
            parser.error("Preparation requires --regression-report and no --plan-file")
        report = prepare(local_settings(), args.regression_report, args.limit)
    print(json.dumps({key: value for key, value in report.items()
                      if key not in ("applied_ids", "sample_ids", "skipped", "original_groups")}))


if __name__ == "__main__":
    main()
