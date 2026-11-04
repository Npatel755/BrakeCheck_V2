"""Audi repairs stage immutable drafts and never imitate human review."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
import uuid

import numpy as np
import pytest

from bcdata import repair_a2d2_conversions as repair
from bcdata.paint_proposals import VERSION as COMPARISON_VERSION
from bcdata.source_validation import refresh_groups
from bcdata.teachers.base import file_sha256
from tests.test_a2d2_pipeline import audi_fixture


def snapshot(store):
    with store.registry() as reg:
        return {table:[dict(row) for row in reg.conn.execute("SELECT * FROM "+table+" ORDER BY rowid")]
                for table in ("images","annotations","qc","review_events","reviews","review_claims",
                              "source_conversion_checks","source_validation_groups")}


def control_evidence(root,report):
    """Synthetic report inputs exercise binding; these are not dataset reviews."""
    selections,inputs,records,summaries = {},{},{},{}
    rows = []
    for subset,count in (("development",1),("untouched_evaluation",24),("approved_regression",103),("failed_audit_cases",1)):
        cases = []
        for index in range(count):
            image_id = "disposable-control-"+subset+"-"+str(index)
            case = {"image_id":image_id}
            for kind in ("image","source","geometry"):
                path = root/(image_id+"-"+kind+".fixture")
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text("disposable native fixture "+kind+" "+image_id)
                case[kind+"_path"],case[kind+"_sha256"] = str(path),file_sha256(path)
                inputs[str(path.resolve())] = file_sha256(path)
            row = {"image_id":image_id,"subset":subset}
            if subset=="approved_regression":
                case.update(review_status="approved",locked=1,human_history=True)
                human = root/(image_id+"-human.fixture")
                human.write_text("disposable synthetic comparison "+image_id)
                inputs[str(human.resolve())] = file_sha256(human)
                row.update(decision="approved",approved_regression_reasons=[],raster_scores={"old":{},"candidate":{}},
                           human_path=str(human),human_sha256=file_sha256(human))
            cases.append(case); rows.append(row)
        manifest = root/(subset+".json")
        manifest.write_text(json.dumps({"cases":cases}))
        selections[str(manifest.resolve())] = file_sha256(manifest)
        summaries[subset] = {"images":count}
    report.update(selection_sha256=selections,input_fingerprints=inputs,records=rows,summaries=summaries,
                  subsets_evaluated=list(summaries),approved_regressions=[])
    report["safety_checks"].update(complete_required_evaluation=True,approved_samples_evaluated=103,untouched_samples_evaluated=24)


def fixture(tmp_path,monkeypatch):
    settings,store,ids = audi_fixture(tmp_path)
    package = tmp_path/"disposable-converter-package"
    for name in repair.DEPENDENCIES:
        path = package/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text("disposable code "+name)
    evaluator = tmp_path/"disposable-evaluator.py"
    evaluator.write_text("# disposable regression evaluator")
    behavior = {"warnings":{}}
    def convert(original,*args,**kwargs):
        geo = deepcopy(original)
        for lane in geo["lanes"]:
            for point in lane["points"]:
                point[0] -= 5
            lane["fit"]["xs"] = [x-5 for x in lane["fit"]["xs"]]
            lane["x_ref"] -= 5
        geo["source_converter"] = {"risk_family":"native_paint_recovery","warnings":behavior["warnings"].get(geo["image_id"],[])}
        return geo
    converter = SimpleNamespace(__file__=str(package/"source_converter_a2d2.py"),VERSION="audi-fixture-v1",repair_geometry_a2d2=convert)
    monkeypatch.setattr(repair,"PACKAGE",package)
    monkeypatch.setattr(repair,"_converter",lambda:converter)
    monkeypatch.setattr(repair,"load_boundary_maps",lambda ann:{key:np.zeros((874,1164),np.float32) for key in ("lane","my_car","movable","solid","dashed")})
    monkeypatch.setattr(repair.qc,"check",lambda *args,**kwargs:(.99,[]))
    monkeypatch.setattr(repair,"_alignment",lambda *args:{"source_coverage":.9,"observed_points_on_paint":.99})
    with store.registry() as reg:
        for image_id in ids:
            reg.update_image(image_id,status="curated")
            candidate = settings.path("lane_geometry","teachers","comma_segnet_fresh",image_id+".json")
            geo = json.loads(candidate.read_text())
            geo.update(teacher_version=COMPARISON_VERSION,source_guided=False)
            candidate.write_text(json.dumps(geo))
            reg.add_annotation(image_id,"teacher","comma_segnet_fresh",COMPARISON_VERSION,str(candidate),params={"candidate_only":True})
    from bcdata.source_validation import run
    run(settings,apply=True,source_name="a2d2",image_ids=ids)
    with store.registry() as reg:
        reg.conn.execute("UPDATE source_validation_groups SET state='blocked'")
        reg.conn.execute("UPDATE source_conversion_checks SET state='flagged'")
        reg.conn.execute("UPDATE source_conversion_checks SET state='awaiting_audit' WHERE image_id IN (?,?,?)",ids[3:])
        reg.conn.execute("UPDATE qc SET issues=? WHERE image_id LIKE 'audi-%'",(json.dumps([{"code":"low_mask_coverage"}]),))
        reg.conn.commit()
    report = {"converter_version":converter.VERSION,"converter_code_path":converter.__file__,
              "converter_code_sha256":file_sha256(converter.__file__),
              "dependency_sha256":{str((package/name).resolve()):file_sha256(package/name) for name in repair.DEPENDENCIES},
              "evaluator_code_path":str(evaluator),"evaluator_code_sha256":file_sha256(evaluator),
              "safety_checks":{"approved_sample_regressions":0,"original_annotations_unchanged":True,
                               "converter_and_dependencies_stable":True,"source_fingerprints_verified":True,
                               "known_failures_resolved_or_flagged":True},
              "known_failure_checks":[{"image_id":ids[0],"status":"passed"}],"unresolved_known_failures":[]}
    control_evidence(tmp_path/"synthetic-controls",report)
    report_path = tmp_path/"disposable-regression-evidence.json"
    report_path.write_text(json.dumps(report))
    return settings,store,ids,report_path,report,behavior


def test_audi_preparation_is_read_only_and_repair_requires_three_actual_samples(tmp_path,monkeypatch):
    settings,store,ids,evidence,_,_ = fixture(tmp_path,monkeypatch)
    before = snapshot(store)
    original_files = {row["path"]:Path(row["path"]).read_bytes() for row in before["annotations"] if Path(row["path"]).is_file()}
    prepared = repair.prepare(settings,evidence)
    assert prepared["drafts"] == 6 and prepared["errors"] == []
    assert snapshot(store) == before
    result = repair.apply_plan(settings,prepared["plan_path"])
    assert result["counts"] == {"sample":3,"awaiting_audit":3}
    assert len(result["sample_ids"]) == 3
    after = snapshot(store)
    assert after["images"] == before["images"]
    assert after["review_events"] == before["review_events"] == []
    assert all(Path(path).read_bytes() == content for path,content in original_files.items())
    old_groups = {row["group_key"]:row for row in before["source_validation_groups"]}
    assert all(row == next(g for g in after["source_validation_groups"] if g["group_key"] == key) for key,row in old_groups.items())
    assert not any(group["state"] == "blocked" for group in store.stats()["source_audit_groups"])
    with store.registry() as reg:
        archive = [dict(row) for row in reg.conn.execute("SELECT * FROM a2d2_conversion_repair_history")]
        assert len(archive) == 6
        assert all(json.loads(row["previous_group"])["state"] == "blocked" for row in archive)
        assert reg.count("SELECT count(*) FROM source_conversion_checks WHERE state='validated'") == 0
    for image_id in result["sample_ids"][:2]:
        store.decide(image_id,"approved","disposable-human",0,str(uuid.uuid4()))
    refresh_groups(settings)
    assert store.stats()["source_validated"] == 0
    store.decide(result["sample_ids"][2],"approved","disposable-human",0,str(uuid.uuid4()))
    refresh_groups(settings)
    assert store.stats()["source_validated"] == 3
    with store.registry() as reg:
        group = reg.conn.execute("SELECT evidence FROM source_validation_groups WHERE batch_id=?",(result["batch_id"],)).fetchone()
        assert json.loads(group[0])["policy"] == repair.POLICY


def test_audi_install_skips_new_human_review_and_live_phone_claim(tmp_path,monkeypatch):
    settings,store,ids,evidence,_,_ = fixture(tmp_path,monkeypatch)
    prepared = repair.prepare(settings,evidence)
    store.decide(ids[0],"approved","disposable-human",0,str(uuid.uuid4()))
    with store.registry() as reg:
        reg.conn.execute("INSERT INTO review_claims VALUES (?,?,?)",(ids[1],"phone-reviewer",time.time()+500))
        reg.conn.commit()
    before = snapshot(store)
    result = repair.apply_plan(settings,prepared["plan_path"])
    assert {item["image_id"]:item["reason"] for item in result["skipped"]} == {ids[0]:"human_reviewed",ids[1]:"active_reviewer"}
    after = snapshot(store)
    assert after["images"] == before["images"] and after["review_events"] == before["review_events"]
    assert after["review_claims"] == before["review_claims"]
    for image_id in ids[:2]:
        assert [c for c in after["source_conversion_checks"] if c["image_id"] == image_id] == [c for c in before["source_conversion_checks"] if c["image_id"] == image_id]


def test_audi_install_skips_source_byte_changes_and_preserves_passed_groups(tmp_path,monkeypatch):
    settings,store,ids,evidence,_,_ = fixture(tmp_path,monkeypatch)
    with store.registry() as reg:
        reg.conn.execute("INSERT INTO source_validation_groups VALUES (?,?,?,?,?,?,?)",
                         ("unrelated-passed-group","unrelated-passed-batch","unrelated",'[]',"passed",'{"preserve":"exact evidence"}',time.time()))
        reg.conn.commit()
        passed = dict(reg.conn.execute("SELECT * FROM source_validation_groups WHERE group_key='unrelated-passed-group'").fetchone())
    prepared = repair.prepare(settings,evidence)
    with store.registry() as reg:
        source = reg.annotation(ids[0],"source_label")
        Path(source["path"]).write_bytes(Path(source["path"]).read_bytes()+b"changed")
    result = repair.apply_plan(settings,prepared["plan_path"])
    assert {item["image_id"]:item["reason"] for item in result["skipped"]} == {ids[0]:"source_bytes_changed"}
    with store.registry() as reg:
        assert dict(reg.conn.execute("SELECT * FROM source_validation_groups WHERE group_key='unrelated-passed-group'").fetchone()) == passed


@pytest.mark.parametrize("change",["qc","draft","evidence","config"])
def test_audi_apply_rejects_changed_staged_bytes_code_or_configuration(tmp_path,monkeypatch,change):
    settings,store,ids,evidence,_,_ = fixture(tmp_path,monkeypatch)
    prepared = repair.prepare(settings,evidence)
    plan = json.loads(Path(prepared["plan_path"]).read_text())
    before = snapshot(store)
    if change == "config":
        settings.cfg["seed"] = 999
    elif change == "evidence":
        evidence.write_text(evidence.read_text()+" ")
    else:
        key = "qc_path" if change == "qc" else "geometry_path"
        path = Path(plan["repairs"][0][key])
        path.write_text(path.read_text()+" ")
    with pytest.raises(ValueError):
        repair.apply_plan(settings,prepared["plan_path"])
    assert snapshot(store) == before


def test_audi_known_unresolved_curve_keeps_actual_flag_and_missing_warning_rejects(tmp_path,monkeypatch):
    settings,store,ids,evidence,report,behavior = fixture(tmp_path,monkeypatch)
    case = {"image_id":ids[0],"status":"flagged","flagged":True,"warnings":["unresolved_missing_curve"]}
    report["known_failure_checks"] = [case]
    report["unresolved_known_failures"] = [case]
    evidence.write_text(json.dumps(report))
    failed = repair.prepare(settings,evidence)
    assert failed["errors"] and failed["errors"][0]["image_id"] == ids[0]
    with pytest.raises(ValueError,match="preparation errors"):
        repair.apply_plan(settings,failed["plan_path"])
    behavior["warnings"][ids[0]] = ["unresolved_missing_curve"]
    prepared = repair.prepare(settings,evidence)
    assert prepared["errors"] == [] and prepared["still_flagged"] == 1
    result = repair.apply_plan(settings,prepared["plan_path"])
    assert result["counts"]["flagged"] == 1
    assert ids[0] not in result["sample_ids"]


def test_audi_regression_report_rejects_approved_control_regressions(tmp_path,monkeypatch):
    settings,_,_,evidence,report,_ = fixture(tmp_path,monkeypatch)
    report["safety_checks"]["approved_sample_regressions"] = 1
    evidence.write_text(json.dumps(report))
    with pytest.raises(ValueError,match="approved-control regressions"):
        repair.prepare(settings,evidence)


def test_audi_prepare_skips_any_old_review_history_even_when_unlocked(tmp_path,monkeypatch):
    settings,store,ids,evidence,_,_ = fixture(tmp_path,monkeypatch)
    with store.registry() as reg:
        reg.conn.execute("INSERT INTO reviews (image_id,reviewer,decision,created_at) VALUES (?,?,?,?)",(ids[0],"old-human","difficult",time.time()))
        reg.conn.commit()
        reg.add_annotation(ids[1],"human","old-human","old-history",reg.annotation(ids[1],"geometry","geometry")["path"])
    prepared = repair.prepare(settings,evidence)
    assert {item["image_id"]:item["reason"] for item in prepared["skipped"]} == {ids[0]:"has_review_history",ids[1]:"has_human_annotation"}
    assert prepared["drafts"] == 4


def test_blocked_source_omissions_cannot_be_resampled_after_only_qc_or_metadata_changes(tmp_path,monkeypatch):
    settings,store,ids,evidence,_,_ = fixture(tmp_path,monkeypatch)
    converter = repair._converter()
    def same_boundaries(original,*args,**kwargs):
        geo = deepcopy(original)
        geo["source_converter"] = {"risk_family":"new_metadata","warnings":[]}
        for lane in geo["lanes"]:
            lane["flags"] = ["changed_diagnostic_only"]
        return geo
    converter.repair_geometry_a2d2 = same_boundaries
    prepared = repair.prepare(settings,evidence)
    # Flagged cases may have a justified QC correction; failed-audit held
    # cohorts require a substantive boundary repair before any new sampling.
    assert prepared["drafts"] == 3
    assert {item["image_id"]:item["reason"] for item in prepared["skipped"]} == {image_id:"blocked_boundary_geometry_unchanged" for image_id in ids[3:]}
    result = repair.apply_plan(settings,prepared["plan_path"])
    assert not set(ids[3:]) & set(result["sample_ids"])
    with store.registry() as reg:
        assert all(reg.conn.execute("SELECT state FROM source_conversion_checks WHERE image_id=?",(image_id,)).fetchone()[0] == "awaiting_audit" for image_id in ids[3:])


def test_audi_mid_install_database_failure_rolls_back_every_annotation_and_group(tmp_path,monkeypatch):
    settings,store,_,evidence,_,_ = fixture(tmp_path,monkeypatch)
    prepared = repair.prepare(settings,evidence)
    before = snapshot(store)
    connect = repair._connect
    class FailSecondAnnotation:
        def __init__(self,con):
            self.con,self.insertions = con,0
        def __getattr__(self,name):
            return getattr(self.con,name)
        def execute(self,query,*args):
            if query.startswith("INSERT INTO annotations"):
                self.insertions += 1
                if self.insertions == 2:
                    raise sqlite3.IntegrityError("simulated interrupted installation")
            return self.con.execute(query,*args)
    monkeypatch.setattr(repair,"_connect",lambda settings,write=False:FailSecondAnnotation(connect(settings,write)) if write else connect(settings,write))
    with pytest.raises(sqlite3.IntegrityError,match="simulated interrupted"):
        repair.apply_plan(settings,prepared["plan_path"])
    assert snapshot(store) == before
    with store.registry() as reg:
        assert not reg.conn.execute("SELECT 1 FROM sqlite_master WHERE name='a2d2_conversion_repair_history'").fetchone()


@pytest.mark.parametrize("metric",["source_coverage","observed_points_on_paint"])
def test_audi_repair_refuses_worse_independently_measured_source_alignment(tmp_path,monkeypatch,metric):
    settings,store,_,evidence,_,_ = fixture(tmp_path,monkeypatch)
    def alignment(geo,paint):
        result = {"source_coverage":.9,"observed_points_on_paint":.99}
        if geo["source_converter"]["risk_family"] == "native_paint_recovery":
            result[metric] -= .02
        return result
    monkeypatch.setattr(repair,"_alignment",alignment)
    before = snapshot(store)
    prepared = repair.prepare(settings,evidence)
    assert prepared["drafts"] == 0 and prepared["errors"] == []
    assert all(item["reason"] == "source_alignment_regressed" for item in prepared["skipped"])
    assert snapshot(store) == before


def test_audi_new_unsafe_topology_warning_cannot_create_a_new_audit(tmp_path,monkeypatch):
    settings,_,ids,evidence,_,behavior = fixture(tmp_path,monkeypatch)
    behavior["warnings"] = {image_id:["ambiguous_new_merge"] for image_id in ids}
    prepared = repair.prepare(settings,evidence)
    assert prepared["drafts"] == 0
    assert all(item["reason"] == "new_unsafe_topology" for item in prepared["skipped"])


def test_unchanged_audi_boundary_with_only_a_new_warning_is_not_a_repair(tmp_path,monkeypatch):
    settings,_,ids,evidence,_,_ = fixture(tmp_path,monkeypatch)
    def warning_only(original,*args,**kwargs):
        geo = deepcopy(original)
        geo["source_converter"] = {"risk_family":"metadata_only","warnings":["new_warning"]}
        return geo
    repair._converter().repair_geometry_a2d2 = warning_only
    prepared = repair.prepare(settings,evidence)
    assert prepared["drafts"] == 0
    skipped = {item["image_id"]:item["reason"] for item in prepared["skipped"]}
    assert all(skipped[image_id] == "no_substantive_geometry_or_qc_improvement" for image_id in ids[:3])


def test_actual_source_alignment_measurement_detects_a_boundary_moved_off_paint():
    paint = np.zeros((180,320),np.float32)
    paint[90:171,78:83] = 1
    lane = {"slot":2,"points":[[80,170,True],[80,90,True]],"y_top":90,"y_bottom":170}
    original = {"width":320,"height":180,"horizon_y":75,"valid_bottom":179,"lanes":[lane]}
    moved = deepcopy(original)
    for point in moved["lanes"][0]["points"]:
        point[0] += 80
    before,after = repair._alignment(original,paint),repair._alignment(moved,paint)
    assert before == {"source_coverage":1.,"observed_points_on_paint":1.}
    assert after == {"source_coverage":0.,"observed_points_on_paint":0.}
    assert repair._worse_alignment(before,after)


def retention_lane(slot,x,ys):
    return {"slot":slot,"points":[[x,y,True] for y in ys],"y_top":min(ys),"y_bottom":max(ys),
            "fit":{"basis":"interp","ys":list(ys),"xs":[x]*len(ys)}}


def test_one_boundary_gain_cannot_hide_another_physical_boundary_loss():
    paint = np.zeros((220,320),np.float32)
    paint[80:191,48:53] = 1
    paint[80:191,248:253] = 1
    original = {"width":320,"height":220,"horizon_y":10,"valid_bottom":219,
                "lanes":[retention_lane(2,50,range(150,191,5)),retention_lane(3,250,range(80,191,5))]}
    proposed = {**original,"lanes":[retention_lane(2,50,range(80,191,5)),retention_lane(3,250,range(120,191,5))]}
    old,new = repair._alignment(original,paint),repair._alignment(proposed,paint)
    assert new["source_coverage"]>old["source_coverage"]
    assert not repair._worse_alignment(old,new)
    retention = repair.boundary_retention(original,proposed,paint)
    assert retention[0]["retained_fraction"]==1
    assert retention[1]["retained_fraction"]<.95


def test_boundary_retention_uses_source_width_allows_slot_changes_and_never_extrapolates():
    paint = np.zeros((220,320),np.float32)
    paint[100:196,60:101] = 1
    original = {"lanes":[retention_lane(2,61,range(100,196,5))]}
    centred = {"lanes":[retention_lane(1,80,range(100,196,5))]}
    assert repair.boundary_retention(original,centred,paint)[0]["retained_fraction"]==1
    # The fit can evaluate y=100, but that lies outside the candidate's actual
    # supported span and therefore cannot count as retained evidence.
    centred["lanes"][0]["y_top"] = 105
    assert repair.boundary_retention(original,centred,paint)[0]["retained_fraction"]==.95
    centred["lanes"][0]["y_top"] = 110
    assert repair.boundary_retention(original,centred,paint)[0]["retained_fraction"]==.9


def test_development_only_zero_regressions_cannot_authorize_audi_installation():
    report = {"safety_checks":{"complete_required_evaluation":False,"approved_sample_regressions":0},
              "subsets_evaluated":["development"],"records":[],"selection_sha256":{},"input_fingerprints":{}}
    with pytest.raises(ValueError,match="complete approved-control"):
        repair._verify_controls(report)


def test_declared_control_counts_without_bound_manifests_are_rejected():
    report = {"safety_checks":{"complete_required_evaluation":True,"approved_samples_evaluated":103,"untouched_samples_evaluated":24},
              "subsets_evaluated":["development","untouched_evaluation","approved_regression","failed_audit_cases"],
              "records":[{"image_id":str(index),"subset":"approved_regression" if index<103 else "untouched_evaluation"} for index in range(127)],
              "approved_regressions":[],"selection_sha256":{},"input_fingerprints":{}}
    with pytest.raises(ValueError,match="bound manifest"):
        repair._verify_controls(report)


def test_explicit_staging_selection_cannot_touch_other_images(tmp_path,monkeypatch):
    settings,store,ids,evidence,_,_ = fixture(tmp_path,monkeypatch)
    before = snapshot(store)
    selected = ids[:2]
    prepared = repair.prepare(settings,evidence,image_ids=selected)
    plan = json.loads(Path(prepared["plan_path"]).read_text())
    assert plan["selected_image_ids"] == sorted(selected)
    assert {row["image_id"] for row in plan["repairs"]} == set(selected)
    assert snapshot(store) == before
    with pytest.raises(ValueError,match="distinct image IDs"):
        repair.prepare(settings,evidence,image_ids=selected+selected)


def test_v2_replacement_requires_refresh_before_an_old_phone_can_approve(tmp_path,monkeypatch):
    from bcdata.review_store import ReviewError
    settings,store,ids,evidence,report,_=fixture(tmp_path,monkeypatch)
    repair._converter().VERSION='a2d2-pairwise-source-v2'
    report['converter_version']='a2d2-pairwise-source-v2'
    evidence.write_text(json.dumps(report))
    prepared=repair.prepare(settings,evidence)
    result=repair.apply_plan(settings,prepared['plan_path'])
    image_id=result['sample_ids'][0]
    assert store.detail(image_id)['revision']==1
    with pytest.raises(ReviewError,match='changed'):
        store.decide(image_id,'approved','phone',0,str(uuid.uuid4()))
    with store.registry() as reg:
        assert reg.count('SELECT count(*) FROM review_events')==0
