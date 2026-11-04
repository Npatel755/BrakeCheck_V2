"""Stage Audi source-only conversion repairs and install fresh human audit gates.

Neither preparation nor installation edits a source file or human decision.
Installation compares every input again under one SQLite write transaction.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

import numpy as np

from .bootstrap import atomic_json
from .lanes import params_for
from .geometry import lane_xs
from .local import local_settings
from . import qc
from .paint_proposals import VERSION as COMPARISON_VERSION
from .repair_source_conversions import conversion_risk
from .source_boundary_maps import load_boundary_maps
from .source_validation import a2d2_conversion_reasons, a2d2_strata, classify, select_samples
from .teachers.base import file_sha256


POLICY = "a2d2-source-repair-audit-v1"
PACKAGE = Path(__file__).resolve().parent
DEPENDENCIES = ("source_converter_a2d2.py", "source_converter.py", "source_converter_v3.py",
                "source_converter_v4.py", "geometry.py", "source_boundary_maps.py",
                "teachers/source_labels.py", "sources/a2d2.py", "qc.py", "quality.py")
HISTORY_SCHEMA = """CREATE TABLE IF NOT EXISTS a2d2_conversion_repair_history (
    repair_id TEXT PRIMARY KEY,image_id TEXT NOT NULL,batch_id TEXT NOT NULL,
    previous_check TEXT NOT NULL,previous_geometry TEXT NOT NULL,previous_source TEXT NOT NULL,
    previous_qc TEXT,previous_group TEXT,replacement TEXT NOT NULL,created_at REAL NOT NULL
)"""


def _json(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False)


def _sha(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _converter():
    return importlib.import_module(".source_converter_a2d2",__package__)


def _converter_for_report(report):
    """Resolve only explicitly supported, SHA-bound source converter versions."""
    if Path(report.get("converter_code_path", "")).name == "source_converter_a2d2_v2.py":
        return importlib.import_module(".source_converter_a2d2_v2", __package__)
    return _converter()


def _connect(settings,write=False):
    con = sqlite3.connect(str(settings.db_path) if write else settings.db_path.resolve().as_uri()+"?mode=ro",uri=not write,timeout=60)
    con.row_factory = sqlite3.Row
    return con


def _latest(con,image_id,kind,producer=None):
    query = "SELECT * FROM annotations WHERE image_id=? AND kind=?"
    args = [image_id,kind]
    if producer:
        query += " AND producer=?"
        args.append(producer)
    row = con.execute(query+" ORDER BY created_at DESC,id DESC LIMIT 1",args).fetchone()
    return dict(row) if row else None


def _row(con,table,image_id):
    row = con.execute("SELECT * FROM "+table+" WHERE image_id=?",(image_id,)).fetchone()
    return dict(row) if row else None


def _group(con,key):
    row = con.execute("SELECT * FROM source_validation_groups WHERE group_key=?",(key,)).fetchone()
    return dict(row) if row else None


def _verify_controls(report):
    """A zero-regression development run cannot authorize installation."""
    required = {"development","untouched_evaluation","approved_regression","failed_audit_cases"}
    checks = report.get("safety_checks",{})
    records = report.get("records")
    selections = report.get("selection_sha256")
    fingerprints = report.get("input_fingerprints")
    if (checks.get("complete_required_evaluation") is not True or not required.issubset(report.get("subsets_evaluated",[]))
            or not isinstance(records,list) or not isinstance(selections,dict) or not isinstance(fingerprints,dict)):
        raise ValueError("Audi installation requires complete approved-control and untouched evaluation evidence")
    ids = [record.get("image_id") for record in records]
    if None in ids or len(ids) != len(set(ids)):
        raise ValueError("Audi evaluation records overlap or have missing identities")
    by_subset = {name:[r for r in records if r.get("subset")==name] for name in required}
    if (len(by_subset["approved_regression"])<100 or len(by_subset["untouched_evaluation"])<24
            or checks.get("approved_samples_evaluated") != len(by_subset["approved_regression"])
            or checks.get("untouched_samples_evaluated") != len(by_subset["untouched_evaluation"])
            or report.get("approved_regressions") != []):
        raise ValueError("Audi approved controls are missing, insufficient or regressed")
    frozen_inputs = {str(Path(path).resolve()):digest for path,digest in fingerprints.items()}
    for name,digest in selections.items():
        if not Path(name).is_file() or file_sha256(name) != digest:
            raise ValueError("Audi frozen evaluation selection changed")
    for subset,rows in by_subset.items():
        choices = [Path(name) for name in selections if Path(name).name == subset+".json"]
        if len(choices)!=1:
            raise ValueError("Audi evaluation subset has no uniquely bound manifest: "+subset)
        cases = json.loads(choices[0].read_text(encoding="utf-8")).get("cases")
        if (not isinstance(cases,list) or {case["image_id"] for case in cases}!={r["image_id"] for r in rows}
                or len(cases)!=len(rows) or report.get("summaries",{}).get(subset,{}).get("images")!=len(rows)):
            raise ValueError("Audi actual evaluation cases differ from their frozen manifest")
        for kind in ("image","source","geometry"):
            paths = [str(Path(case[kind+"_path"]).resolve()) for case in cases]
            if len(paths)!=len(set(paths)):
                raise ValueError("Audi evaluation repeats native inputs instead of distinct controls")
            for case,path in zip(cases,paths):
                digest = case[kind+"_sha256"]
                if frozen_inputs.get(path)!=digest or not Path(path).is_file() or file_sha256(path)!=digest:
                    raise ValueError("Audi evaluation native input is unbound or changed")
        if subset=="approved_regression":
            if any(case.get("review_status")!="approved" or case.get("locked")!=1 or case.get("human_history") is not True for case in cases):
                raise ValueError("Audi regression manifest does not contain actual approved controls")
    human_paths = []
    for record in by_subset["approved_regression"]:
        path = record.get("human_path")
        digest = record.get("human_sha256")
        if (record.get("decision")!="approved" or record.get("approved_regression_reasons")!=[]
                or not isinstance(record.get("raster_scores"),dict) or not isinstance(path,str)):
            raise ValueError("Audi approved control has no actual human comparison")
        path = str(Path(path).resolve())
        if frozen_inputs.get(path)!=digest or not Path(path).is_file() or file_sha256(path)!=digest:
            raise ValueError("Audi approved human annotation is unbound or changed")
        human_paths.append(path)
    if len(human_paths)!=len(set(human_paths)):
        raise ValueError("Audi approved controls repeat a human annotation")


def verify_regression_report(path):
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    converter = _converter_for_report(report)
    code = Path(converter.__file__).resolve()
    if (report.get("converter_version") != converter.VERSION
            or Path(report.get("converter_code_path","")).resolve() != code
            or report.get("converter_code_sha256") != file_sha256(code)):
        raise ValueError("Audi regression report belongs to a different converter")
    checks = report.get("safety_checks",{})
    if type(checks.get("approved_sample_regressions")) is not int or checks["approved_sample_regressions"] != 0:
        raise ValueError("Audi regression evidence has approved-control regressions")
    _verify_controls(report)
    for key in ("original_annotations_unchanged","converter_and_dependencies_stable",
                "source_fingerprints_verified","known_failures_resolved_or_flagged"):
        if checks.get(key) is not True:
            raise ValueError("Audi regression evidence is incomplete: "+key)
    dependencies = report.get("dependency_sha256")
    if not isinstance(dependencies,dict):
        raise ValueError("Audi regression dependencies are missing")
    dependencies = {str(Path(name).resolve()):digest for name,digest in dependencies.items()}
    required = {str((PACKAGE/name).resolve()) for name in DEPENDENCIES} | {str(code)}
    if not required.issubset(dependencies):
        raise ValueError("Audi regression dependency fingerprints are incomplete")
    for name,digest in dependencies.items():
        if not Path(name).is_file() or file_sha256(name) != digest:
            raise ValueError("Audi converter dependency changed: "+name)
    evaluator = report.get("evaluator_code_path")
    if not isinstance(evaluator,str) or not Path(evaluator).is_file() or file_sha256(evaluator) != report.get("evaluator_code_sha256"):
        raise ValueError("Audi evaluator code changed")
    known,unresolved = report.get("known_failure_checks"),report.get("unresolved_known_failures")
    def flagged(item):
        return (isinstance(item,dict) and item.get("flagged") is True and isinstance(item.get("warnings"),list)
                and bool(item["warnings"]) and all(isinstance(w,str) and w for w in item["warnings"]))
    if (not isinstance(known,list) or not known or not isinstance(unresolved,list)
            or any(not isinstance(item,dict) or item.get("status") not in ("passed","flagged")
                   or (item["status"] == "flagged" and not flagged(item)) for item in known)
            or any(not flagged(item) for item in unresolved)):
        raise ValueError("Audi known defects are neither resolved nor explicitly flagged")
    return report


def _evidence(settings,path):
    report = verify_regression_report(path)
    return {"converter_version":report["converter_version"],"converter_code_path":report["converter_code_path"],
            "converter_code_sha256":report["converter_code_sha256"],
            "regression_report_path":str(Path(path).resolve()),"regression_report_sha256":file_sha256(path),
            "dependency_sha256":report["dependency_sha256"],"evaluator_code_path":report["evaluator_code_path"],
            "evaluator_code_sha256":report["evaluator_code_sha256"],"repair_code_sha256":file_sha256(__file__),
            "config_sha256":_sha(settings.cfg)}


def _ineligible(con,previous,expected=None):
    image_id = previous["image_id"]
    row = _row(con,"images",image_id)
    if not row or row["source"] != "a2d2" or row["status"] != "curated":
        return "not_curated_audi"
    if row["locked"] or row["review_status"] in ("approved","corrected","rejected"):
        return "human_reviewed"
    for table in ("review_events","reviews"):
        if con.execute("SELECT 1 FROM "+table+" WHERE image_id=? LIMIT 1",(image_id,)).fetchone():
            return "has_review_history"
    if con.execute("SELECT 1 FROM annotations WHERE image_id=? AND kind='human' LIMIT 1",(image_id,)).fetchone():
        return "has_human_annotation"
    if con.execute("SELECT 1 FROM review_claims WHERE image_id=? AND expires>?",(image_id,time.time())).fetchone():
        return "active_reviewer"
    current = _row(con,"source_conversion_checks",image_id)
    if current != previous:
        return "source_check_changed"
    group = _group(con,previous["group_key"])
    if previous["state"] != "flagged" and not (previous["state"] == "awaiting_audit" and group and group["state"] == "blocked"):
        return "not_flagged_or_blocked"
    for kind,producer,key in (("geometry","geometry","geometry"),("source_label",None,"source")):
        ann = _latest(con,image_id,kind,producer)
        if (not ann or ann["id"] != previous[key+"_id"] or ann["path"] != previous[key+"_path"]
                or ann["created_at"] > previous["created_at"]):
            return key+"_changed"
        if not Path(ann["path"]).is_file() or file_sha256(ann["path"]) != previous[key+"_sha256"]:
            return key+"_bytes_changed"
        if expected and ann != expected["previous_"+key]:
            return key+"_record_changed"
        if key == "source":
            params = json.loads(ann["params"] or "{}")
            if params.get("human") is not True:
                return "source_not_human"
            if params.get("format") == "a2d2_rgb":
                palette = params.get("palette_path")
                if not palette or not Path(palette).is_file() or file_sha256(palette) != params.get("palette_sha256"):
                    return "source_palette_changed"
    if expected:
        if row != expected["previous_image"] or _row(con,"qc",image_id) != expected["previous_qc"]:
            return "image_or_qc_changed"
        if group != expected["previous_group"]:
            return "previous_group_changed"
        teacher = Path(expected["teacher_path"])
        if not teacher.is_file() or file_sha256(teacher) != expected["teacher_sha256"]:
            return "independent_comparison_changed"
        if _latest(con,image_id,"teacher","comma_segnet_fresh") != expected["previous_teacher"]:
            return "independent_comparison_record_changed"
    return None


def _immutable(path,value):
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != value:
            raise ValueError("Immutable Audi repair artifact conflict")
    else:
        atomic_json(path,value)


def _known_warnings(report,image_id,reasons,geometry):
    actual = set(reasons) | set(geometry.get("source_converter",{}).get("warnings",[]))
    for item in report["known_failure_checks"] + report["unresolved_known_failures"]:
        if item.get("image_id") == image_id and item.get("status", "flagged") == "flagged":
            if not set(item["warnings"]).issubset(actual):
                raise ValueError("Known unresolved Audi defect lost its actual warning: "+image_id)


def _boundary_content(geometry):
    """Diagnostic flags and producer metadata are not a repaired boundary."""
    return {"horizon_y":geometry.get("horizon_y"),"valid_bottom":geometry.get("valid_bottom"),
            "lanes":[{key:lane.get(key) for key in ("slot","kind","physical","fit","points","y_top","y_bottom")}
                     for lane in geometry.get("lanes",[])]}


def _unsafe_topology(codes):
    explicit = {"invalid_coordinates","crossing","crossing_severe","slot_conflict","source_slot_disjoint",
                "transverse_source_promoted_to_boundary","duplicate_slots","ego_slot_side_uncertain"}
    return {code for code in codes if code in explicit or
            (code.startswith("repair_") and any(word in code for word in ("crossing","duplicate","association","topology","invalid","ambiguous")))}


def _alignment(geometry,paint):
    coverage = qc.a2d2_source_coverage(geometry,paint)["coverage"]
    _,on_paint = qc.mask_polyline_consistency(geometry,paint)
    return {"source_coverage":coverage,"observed_points_on_paint":on_paint}


def _worse_alignment(before,after):
    return (after["observed_points_on_paint"] < before["observed_points_on_paint"]-1e-6
            or (before["source_coverage"] is not None and
                (after["source_coverage"] is None or after["source_coverage"] < before["source_coverage"]-1e-6)))


def boundary_retention(original,candidate,paint):
    """Keep every previously observed physical boundary, independent of slots.

    Only actual paint-supported old observations count. Source row width and
    double-line spacing allow a curve to move to the centre of a broad stroke;
    a candidate cannot match a point outside its measured vertical extent.
    """
    row_runs = {}
    height,width = paint.shape
    proposed = [lane for lane in candidate.get("lanes",[]) if lane.get("slot") in (1,2,3,4)]
    results = []
    for lane in original.get("lanes",[]):
        if lane.get("slot") not in (1,2,3,4):
            continue
        supported = retained = 0
        lost = []
        half_sep = max(0.,float(lane.get("double_half_sep",0.)))
        for point in lane.get("points",[]):
            if len(point)>2 and not point[2]:
                continue
            x,y = map(float,point[:2])
            if not np.isfinite([x,y]).all():
                continue
            yi = int(round(y))
            if not (0<=yi<height and 0<=x<width):
                continue
            if yi not in row_runs:
                padded = np.r_[0,(paint[yi]>=.5).astype(np.int8),0]
                delta = np.diff(padded)
                row_runs[yi] = list(zip(np.flatnonzero(delta==1),np.flatnonzero(delta==-1)))
            runs = row_runs[yi]
            if not runs:
                continue
            distance = lambda run:max(float(run[0])-x,x-float(run[1]-1),0.)
            start,stop = min(runs,key=distance)
            if distance((start,stop))>half_sep+4.:
                continue
            supported += 1
            tolerance = .5*max(1,int(stop)-int(start)-1)+half_sep+4.
            matched = False
            for new in proposed:
                if not float(new["y_top"])<=y<=float(new["y_bottom"]):
                    continue
                fit = new.get("fit",{})
                if fit.get("basis")=="interp" and not min(fit["ys"])<=y<=max(fit["ys"]):
                    continue
                prediction = float(lane_xs(new,y))
                if np.isfinite(prediction) and abs(prediction-x)<=tolerance:
                    matched = True
                    break
            retained += int(matched)
            if not matched and len(lost)<12:
                lost.append([x,y])
        results.append({"original_slot":lane["slot"],"source_supported_points":supported,
                        "retained_points":retained,"retained_fraction":retained/supported if supported else None,
                        "lost_point_examples":lost})
    return results


def prepare(settings,regression_report,limit=None,*,image_ids=None):
    if limit is not None and limit < 1:
        raise ValueError("Limit must be positive")
    if image_ids is not None and (not isinstance(image_ids,list) or not image_ids
                                  or any(not isinstance(value,str) or not value for value in image_ids)
                                  or len(set(image_ids)) != len(image_ids)):
        raise ValueError("Explicit Audi selection must contain distinct image IDs")
    evidence = _evidence(settings,regression_report)
    development = verify_regression_report(regression_report)
    converter = _converter_for_report(development)
    output = settings.path("lane_geometry","source_repairs",converter.VERSION)
    output.mkdir(parents=True,exist_ok=True)
    con = _connect(settings)
    drafts,skipped,errors = [],[],[]
    try:
        checks = con.execute("""SELECT c.* FROM source_conversion_checks c JOIN images i USING(image_id)
            LEFT JOIN source_validation_groups g USING(group_key)
            WHERE i.source='a2d2' AND i.status='curated' AND (c.state='flagged' OR (c.state='awaiting_audit' AND g.state='blocked'))
            ORDER BY c.image_id""").fetchall()
        if image_ids is not None:
            selected = set(image_ids)
            checks = [row for row in checks if row["image_id"] in selected]
        if limit is not None:
            checks = checks[:limit]
        for index,item in enumerate(checks):
            if index%50 == 0:
                print(_json({"phase":"preparing_audi_repairs","processed":index,"total":len(checks),"drafts":len(drafts),"errors":len(errors)}),flush=True)
            previous = dict(item)
            image_id = previous["image_id"]
            reason = _ineligible(con,previous)
            if reason:
                skipped.append({"image_id":image_id,"reason":reason})
                continue
            try:
                row = _row(con,"images",image_id)
                source,geometry = _latest(con,image_id,"source_label"),_latest(con,image_id,"geometry","geometry")
                old_qc = _row(con,"qc",image_id)
                if old_qc is None:
                    raise ValueError("Original Audi QC record is missing")
                teacher_path = settings.path("lane_geometry","teachers","comma_segnet_fresh",image_id+".json")
                teacher = json.loads(teacher_path.read_text(encoding="utf-8"))
                teacher_ann = _latest(con,image_id,"teacher","comma_segnet_fresh")
                if teacher_ann is None or not Path(teacher_ann["path"]).is_file():
                    raise ValueError("Independent Audi model annotation is missing")
                if teacher_ann["model_version"] != COMPARISON_VERSION or teacher.get("teacher_version") != COMPARISON_VERSION or teacher.get("source_guided") is True:
                    raise ValueError("Independent Audi comparison is not the pinned source-unguided model")
                original = json.loads(Path(geometry["path"]).read_text(encoding="utf-8"))
                maps = load_boundary_maps(source)
                if any(value.shape != (row["height"],row["width"]) for value in maps.values()):
                    raise ValueError("Audi source label dimensions changed")
                repaired = converter.repair_geometry_a2d2(deepcopy(original),maps["lane"],maps.get("my_car"),maps.get("movable"),params=params_for(settings),
                                                         solid_mask=maps.get("solid"),dashed_mask=maps.get("dashed"))
                repaired.update(image_id=image_id,mode="source_label")
                metadata = repaired.get("source_converter",{})
                warnings = metadata.get("warnings")
                if not isinstance(warnings,list) or any(not isinstance(w,str) or not w for w in warnings):
                    raise ValueError("Audi converter warning metadata is missing or invalid")
                family = metadata.get("risk_family")
                if not isinstance(family,str) or not family:
                    raise ValueError("Audi converter risk cause is missing")
                score,issues = qc.check(repaired,{"mode":"source_label"},maps["lane"],None,settings,source="a2d2")
                issues = list(issues)
                for warning in warnings:
                    if not any(issue["code"] == "repair_"+warning for issue in issues):
                        issues.append({"code":"repair_"+warning})
                reasons = sorted(set(classify(repaired,[issue["code"] for issue in issues],teacher)+a2d2_conversion_reasons(repaired)))
                _known_warnings(development,image_id,reasons,repaired)
                _,baseline_issues = qc.check(original,{"mode":"source_label"},maps["lane"],None,settings,source="a2d2")
                baseline_reasons = set(classify(original,[issue["code"] for issue in baseline_issues],teacher)+a2d2_conversion_reasons(original))
                before_alignment,after_alignment = _alignment(original,maps["lane"]),_alignment(repaired,maps["lane"])
                retention = boundary_retention(original,repaired,maps["lane"])
                if any(item["retained_fraction"] is not None and item["retained_fraction"]<.95-1e-9 for item in retention):
                    skipped.append({"image_id":image_id,"reason":"source_supported_boundary_span_lost","boundaries":retention})
                    continue
                if _worse_alignment(before_alignment,after_alignment):
                    skipped.append({"image_id":image_id,"reason":"source_alignment_regressed",
                                    "before":before_alignment,"after":after_alignment})
                    continue
                if _unsafe_topology(reasons)-_unsafe_topology(baseline_reasons):
                    skipped.append({"image_id":image_id,"reason":"new_unsafe_topology","codes":sorted(_unsafe_topology(reasons)-_unsafe_topology(baseline_reasons))})
                    continue
                geometry_changed = _sha(_boundary_content(original)) != _sha(_boundary_content(repaired))
                quality = {"score":score,"tier":qc.tier_for(score,issues,"source_label",settings),
                           "priority":1-score,"issues":issues,"qc_sample":False}
                qc_changed = (abs(score-old_qc["score"]) > 1e-9 or quality["tier"] != old_qc["tier"]
                              or sorted(map(_json,issues)) != sorted(map(_json,json.loads(old_qc["issues"]))))
                if previous["state"] == "awaiting_audit" and not geometry_changed:
                    skipped.append({"image_id":image_id,"reason":"blocked_boundary_geometry_unchanged"})
                    continue
                old_reasons = set(classify(original,[issue["code"] for issue in json.loads(old_qc["issues"])],teacher)+a2d2_conversion_reasons(original))
                if not geometry_changed and not set(reasons) < old_reasons:
                    skipped.append({"image_id":image_id,"reason":"no_substantive_geometry_or_qc_improvement"})
                    continue
                if not geometry_changed and not qc_changed:
                    skipped.append({"image_id":image_id,"reason":"unchanged_geometry_and_qc"})
                    continue
                version = converter.VERSION+":"+_sha({"previous":previous,"evidence":evidence})[:20]
                repaired["version"] = version
                diagnostics = conversion_risk(original,repaired)
                diagnostics.update(risk_family=family,geometry_changed=geometry_changed,qc_changed=qc_changed,
                                   converter_evidence=evidence,
                                   independently_measured_safety={"before":before_alignment,"after":after_alignment,
                                                                "new_unsafe_topology":[],"baseline_reasons":sorted(baseline_reasons),
                                                                "per_boundary_source_retention":retention})
                repaired["conversion_repair"] = diagnostics
                path = output/(image_id+"-"+version.rsplit(":",1)[1]+".json")
                quality_path = path.with_suffix(".qc.json")
                _immutable(path,repaired); _immutable(quality_path,quality)
                drafts.append({"image_id":image_id,"previous_check":previous,"previous_image":row,
                               "previous_geometry":geometry,"previous_source":source,"previous_qc":old_qc,
                               "previous_group":_group(con,previous["group_key"]),"previous_teacher":teacher_ann,
                               "teacher_path":str(teacher_path),"teacher_sha256":file_sha256(teacher_path),
                               "geometry_path":str(path),"geometry_version":version,"geometry_sha256":file_sha256(path),
                               "qc_path":str(quality_path),"qc_sha256":file_sha256(quality_path),
                               "reasons":reasons,"stratum":a2d2_strata(row,repaired),"diagnostics":diagnostics})
            except (OSError,ValueError,TypeError,KeyError) as error:
                errors.append({"image_id":image_id,"error":str(error)})
            if (index+1)%50 == 0:
                print(_json({"phase":"preparing_audi_repairs","processed":index+1,"total":len(checks),"drafts":len(drafts),"errors":len(errors)}),flush=True)
    finally:
        con.close()
    if _evidence(settings,regression_report) != evidence:
        raise ValueError("Audi converter or regression evidence changed during preparation")
    plan = {"schema":"a2d2_conversion_repair_plan:v1","policy":POLICY,"converter":converter.VERSION,
            "selected_image_ids":sorted(image_ids) if image_ids is not None else None,
            "evidence":evidence,"repairs":drafts,"skipped":skipped,"errors":errors,"created_at":time.time()}
    plan["plan_sha256"] = _sha(plan)
    plan_id = time.strftime("%Y%m%d-%H%M%S",time.gmtime())+"-"+uuid.uuid4().hex[:10]
    plan_path = settings.path("reports","a2d2_conversion_repairs",plan_id+".plan.json")
    _immutable(plan_path,plan)
    report = {"policy":POLICY,"plan_path":str(plan_path),"plan_sha256":plan["plan_sha256"],"drafts":len(drafts),
              "geometry_changed":sum(d["diagnostics"]["geometry_changed"] for d in drafts),
              "still_flagged":sum(bool(d["reasons"]) for d in drafts),"errors":errors,"skipped":skipped,"applied":False}
    atomic_json(plan_path.with_suffix(".report.json"),report)
    return report


def apply_plan(settings,path):
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    if (plan.get("schema") != "a2d2_conversion_repair_plan:v1" or plan.get("policy") != POLICY
            or plan.get("errors") != [] or _sha({k:v for k,v in plan.items() if k != "plan_sha256"}) != plan.get("plan_sha256")):
        raise ValueError("Audi repair plan is unsealed, invalid or has preparation errors")
    evidence = plan["evidence"]
    if _evidence(settings,evidence["regression_report_path"]) != evidence:
        raise ValueError("Audi converter, QC, configuration or regression evidence changed")
    development = verify_regression_report(evidence["regression_report_path"])
    drafts = plan["repairs"]
    if len({draft["image_id"] for draft in drafts}) != len(drafts):
        raise ValueError("Audi repair plan contains duplicate image IDs")
    for draft in drafts:
        for key in ("geometry","qc"):
            if file_sha256(draft[key+"_path"]) != draft[key+"_sha256"]:
                raise ValueError("Audi staged "+key+" bytes changed")
        if Path(draft["geometry_path"]).resolve() == Path(draft["previous_geometry"]["path"]).resolve():
            raise ValueError("Audi repairs must preserve original geometry files")
        geo = json.loads(Path(draft["geometry_path"]).read_text())
        quality = json.loads(Path(draft["qc_path"]).read_text())
        if geo.get("image_id") != draft["image_id"] or geo.get("version") != draft["geometry_version"] or geo.get("mode") != "source_label":
            raise ValueError("Audi staged geometry identity changed")
        teacher = json.loads(Path(draft["teacher_path"]).read_text())
        reasons = sorted(set(classify(geo,[issue["code"] for issue in quality["issues"]],teacher)+a2d2_conversion_reasons(geo)))
        if reasons != draft["reasons"] or draft["stratum"] != a2d2_strata(draft["previous_image"],geo):
            raise ValueError("Audi staged diagnostics changed")
        _known_warnings(development,draft["image_id"],reasons,geo)
    con = _connect(settings,write=True)
    ready,skipped = [],[]
    try:
        con.execute("BEGIN IMMEDIATE")
        for draft in drafts:
            reason = _ineligible(con,draft["previous_check"],draft)
            if reason:
                skipped.append({"image_id":draft["image_id"],"reason":reason})
                continue
            if any(file_sha256(draft[key+"_path"]) != draft[key+"_sha256"] for key in ("geometry","qc")):
                raise ValueError("Audi staged bytes changed during installation")
            ready.append(draft)
        if _evidence(settings,evidence["regression_report_path"]) != evidence:
            raise ValueError("Audi code or regression evidence changed during installation")
        batch = POLICY+":"+_sha({"plan":plan["plan_sha256"],"ids":sorted(d["image_id"] for d in ready)})[:16]
        groups = defaultdict(list)
        for draft in ready:
            if not draft["reasons"]:
                groups[batch+":"+draft["stratum"]].append(draft["image_id"])
        samples = select_samples(groups,minimum_per_group=3,maximum_per_group=3,policy=POLICY)
        sample_ids = {image_id for ids in samples.values() for image_id in ids}
        now = time.time()
        if ready:
            con.execute(HISTORY_SCHEMA)
        for group,ids in groups.items():
            group_evidence = {"policy":POLICY,"requires_fresh_human_audit":True,"converter_evidence":evidence,
                              "meaning":"New source conversion generation; actual human sample approval is required"}
            con.execute("INSERT INTO source_validation_groups VALUES (?,?,?,?,?,?,?)",
                        (group,batch,group.removeprefix(batch+":"),_json(samples[group]),"awaiting_audit",_json(group_evidence),now))
        counts = Counter()
        for draft in ready:
            image_id = draft["image_id"]
            state = "flagged" if draft["reasons"] else "sample" if image_id in sample_ids else "awaiting_audit"
            counts[state] += 1
            group = batch+":"+draft["stratum"]
            params = {"repair_policy":POLICY,"previous_geometry_id":draft["previous_geometry"]["id"],"evidence":evidence}
            cursor = con.execute("INSERT INTO annotations (image_id,kind,producer,model_version,license_ids,path,params,created_at) VALUES (?,'geometry','geometry',?,?,?,?,?)",
                                 (image_id,draft["geometry_version"],draft["previous_geometry"]["license_ids"],draft["geometry_path"],_json(params),now))
            quality = json.loads(Path(draft["qc_path"]).read_text())
            con.execute("UPDATE qc SET score=?,tier=?,priority=?,issues=?,qc_sample=0,geometry_version=?,updated_at=? WHERE image_id=?",
                        (quality["score"],quality["tier"],quality["priority"],_json(quality["issues"]),draft["geometry_version"],now,image_id))
            diagnostics = {**draft["diagnostics"],"reasons":draft["reasons"],"stratum":draft["stratum"],
                           "previous_group":draft["previous_check"]["group_key"],"repair_policy":POLICY,"requires_fresh_human_audit":True}
            replacement = {"geometry_id":cursor.lastrowid,"geometry_path":draft["geometry_path"],"geometry_sha256":draft["geometry_sha256"],
                           "qc_path":draft["qc_path"],"qc_sha256":draft["qc_sha256"],"state":state,"group_key":group,"diagnostics":diagnostics}
            con.execute("INSERT INTO a2d2_conversion_repair_history VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (batch+":"+image_id,image_id,batch,_json(draft["previous_check"]),_json(draft["previous_geometry"]),
                         _json(draft["previous_source"]),_json(draft["previous_qc"]),_json(draft["previous_group"]),_json(replacement),now))
            con.execute("UPDATE source_conversion_checks SET batch_id=?,group_key=?,state=?,geometry_id=?,geometry_version=?,geometry_path=?,geometry_sha256=?,diagnostics=?,created_at=? WHERE image_id=?",
                        (batch,group,state,cursor.lastrowid,draft["geometry_version"],draft["geometry_path"],draft["geometry_sha256"],_json(diagnostics),now,image_id))
        if ready:
            if evidence["converter_version"] == "a2d2-pairwise-source-v2":
                # An expired claim must not allow an old open phone tab to
                # approve a newly replaced proposal without first seeing it.
                for draft in ready:
                    con.execute("INSERT INTO review_versions VALUES (?,1) ON CONFLICT(image_id) DO UPDATE SET revision=revision+1", (draft["image_id"],))
            con.execute("UPDATE local_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        # SQLite prevents concurrent review changes; files and code need their
        # own final comparison before committing the new annotation IDs.
        if _evidence(settings,evidence["regression_report_path"]) != evidence:
            raise ValueError("Audi evidence changed before commit")
        for draft in ready:
            for name in ("geometry","qc"):
                if file_sha256(draft[name+"_path"]) != draft[name+"_sha256"]:
                    raise ValueError("Audi staged bytes changed before commit")
            for name in ("geometry","source"):
                if file_sha256(draft["previous_"+name]["path"]) != draft["previous_check"][name+"_sha256"]:
                    raise ValueError("Audi original bytes changed before commit")
            if file_sha256(draft["teacher_path"]) != draft["teacher_sha256"]:
                raise ValueError("Audi independent comparison changed before commit")
        con.commit()
        report = {"policy":POLICY,"plan_path":str(Path(path).resolve()),"plan_sha256":plan["plan_sha256"],"batch_id":batch,
                  "applied":True,"applied_ids":[d["image_id"] for d in ready],"counts":dict(counts),"groups":len(groups),
                  "sample_ids":sorted(sample_ids),"skipped":skipped,"errors":[],
                  "meaning":"Fresh actual human audit required; no automatic approvals or source validation."}
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    result_path = Path(path).with_suffix(".applied-"+uuid.uuid4().hex[:10]+".json")
    atomic_json(result_path,report)
    atomic_json(settings.path("reports","a2d2_conversion_repairs.json"),report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare",action="store_true")
    mode.add_argument("--apply",action="store_true")
    parser.add_argument("--regression-report",type=Path)
    parser.add_argument("--plan-file",type=Path)
    parser.add_argument("--data-root",type=Path)
    parser.add_argument("--limit",type=int)
    parser.add_argument("--image-ids-file",type=Path,help="Explicit disjoint selection for offline parallel staging")
    args = parser.parse_args()
    settings = local_settings(args.data_root)
    if args.prepare:
        if not args.regression_report or args.plan_file:
            parser.error("--prepare requires --regression-report")
        image_ids = json.loads(args.image_ids_file.read_text()) if args.image_ids_file else None
        report = prepare(settings,args.regression_report,args.limit,image_ids=image_ids)
    else:
        if not args.plan_file or args.regression_report or args.limit or args.image_ids_file:
            parser.error("--apply requires only --plan-file")
        report = apply_plan(settings,args.plan_file)
    print(_json(report))


if __name__ == "__main__":
    main()
