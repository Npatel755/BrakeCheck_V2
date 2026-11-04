"""Disposable Audi source conversions retain human audit and label authority."""
from copy import deepcopy
import json
import time
import uuid

import cv2
import numpy as np
from PIL import Image
import pytest

from bcdata import lanes
from bcdata import source_validation
from bcdata.fusion import fuse_image, load_fused
from bcdata.geometry import GeoParams, extract_lanes
from bcdata.review_store import ReviewStore
from bcdata.source_boundary_maps import load_boundary_maps
from bcdata.source_validation import (A2D2_POLICY, a2d2_conversion_reasons,
                                      a2d2_strata, refresh_groups, run, validated_record)
from bcdata.teachers.source_labels import SourceLabelTeacher, load_classes
from bcdata.teachers.base import TeacherOutput, save_output
from bcdata.teachers.base import file_sha256
from tests.test_review import build_review_fixture


PALETTE = {"#ffc125": "Solid line", "#8000ff": "Dashed line",
           "#ff00ff": "RD normal street", "#c87dd2": "Painted driv. instr.",
           "#d23273": "Zebra crossing", "#ff0000": "Car 1",
           "#87ceff": "Ego car"}


def annotation(tmp_path, rgb):
    image = tmp_path / "source.png"
    Image.fromarray(rgb).save(image)
    palette = tmp_path / "class_list.json"
    palette.write_text(json.dumps(PALETTE))
    return {"path": str(image), "params": json.dumps({"format": "a2d2_rgb", "human": True,
                                                       "palette_path": str(palette)})}


def test_audi_shared_classes_and_boundary_maps_exclude_non_boundary_paint(tmp_path):
    rgb = np.asarray([[[255,193,37], [128,0,255], [255,0,255], [200,125,210],
                       [210,50,115], [255,0,0], [135,206,255], [254,193,37]]], np.uint8)
    ann = annotation(tmp_path, rgb)
    classes = load_classes(ann)
    assert classes.tolist() == [[2,2,1,0,0,4,5,0]]
    maps = load_boundary_maps(ann)
    for key, cls in (("lane",2), ("movable",4), ("my_car",5)):
        np.testing.assert_array_equal(maps[key], classes == cls)
    np.testing.assert_array_equal(maps["solid"], [[1,0,0,0,0,0,0,0]])
    np.testing.assert_array_equal(maps["dashed"], [[0,1,0,0,0,0,0,0]])
    np.testing.assert_array_equal(maps["lane"], maps["solid"] + maps["dashed"])


def test_audi_human_source_stays_fused_truth_when_model_disagrees(tmp_path):
    settings = build_review_fixture(tmp_path)
    store = ReviewStore(settings)
    rgb = np.asarray([[[255,193,37], [128,0,255], [255,0,255], [200,125,210]]], np.uint8)
    ann = annotation(tmp_path,rgb)
    with store.registry() as reg:
        reg.update_image("sample-0",source="a2d2",license_id="a2d2")
        reg.add_annotation("sample-0","source_label","a2d2","original-label",ann["path"],
                           license_ids=["a2d2"],params=json.loads(ann["params"]))
        row = reg.image("sample-0")
        teacher = SourceLabelTeacher(settings,reg)
        source = teacher.predict(np.zeros_like(rgb),row)
        np.testing.assert_array_equal(source.lane,[[1,1,0,0]])
        np.testing.assert_array_equal(source.road,[[1,1,1,0]])
        for name,output in (("source_labels",source),("fixture_model",TeacherOutput(1-source.lane,1-source.road))):
            paths = save_output(settings,name,"fixture-v1","sample-0",output)
            reg.add_annotation("sample-0","teacher",name,"fixture-v1",paths["_meta"],license_ids=["a2d2"])
        fuse_image(settings,reg,row,["fixture_model"])
        record,maps = load_fused(reg,"sample-0")
        assert record["mode"] == "source_label"
        np.testing.assert_array_equal(maps["lane"],source.lane)
        assert record["teacher_vs_source"]["fixture_model"]["lane_iou"] == 0
        assert "a2d2" in json.loads(reg.annotation("sample-0","fused","fusion")["license_ids"])


def test_audi_transverse_stop_bar_stays_paint_reference_but_not_lane(tmp_path):
    rgb = np.zeros((180,320,3), np.uint8)
    rgb[110:116,10:310] = (255,193,37)
    # These separate source classes must never leak into lane proposals.
    rgb[120:170,40:50] = (200,125,210)
    rgb[125:130,60:250] = (210,50,115)
    paint = load_boundary_maps(annotation(tmp_path, rgb))["lane"]
    assert paint[112,150] == 1
    geometry = extract_lanes(paint, GeoParams())
    assert not [lane for lane in geometry["lanes"] if lane.get("slot")]
    assert geometry["tags"]["crosswalk_or_stop_line"]
    assert a2d2_conversion_reasons(geometry) == []
    assert "transverse_present" in a2d2_strata({"split":"train","tags":"{}"}, geometry)


def test_audi_near_horizontal_promoted_geometry_requires_individual_attention():
    unsafe = {"lanes":[{"slot":2,"points":[[20,110],[290,113]]}]}
    assert a2d2_conversion_reasons(unsafe) == ["transverse_source_promoted_to_boundary"]
    assert a2d2_conversion_reasons({"lanes":[{"slot":2,"points":[[20,150],[80,90]]}]}) == []


@pytest.mark.parametrize("source,mode,repaired", [("a2d2","source_label",True),
                                                   ("comma10k","source_label",True),
                                                   ("a2d2","teachers",False),
                                                   ("other","source_label",False)])
def test_repairs_route_only_trusted_source_geometry(tmp_path, monkeypatch, source, mode, repaired):
    settings = build_review_fixture(tmp_path)
    store = ReviewStore(settings)
    calls = []
    with store.registry() as reg:
        reg.update_image("sample-0", source=source)
        row = reg.image("sample-0")
        geometry = json.loads(settings.path("lane_geometry", "sample-0.json").read_text())
        reg.add_annotation("sample-0", "fused", "fusion", "source-test", "unused", license_ids=["a2d2"])
        monkeypatch.setattr(lanes, "load_fused", lambda *args: ({"version":"test", "mode":mode}, {"lane":np.ones((874,1164),np.float32)}))
        monkeypatch.setattr(lanes, "extract_lanes", lambda *args: deepcopy(geometry))
        monkeypatch.setattr(lanes, "risk_diagnostics", lambda geo: {"risky":False})
        def repair(geo, *args):
            calls.append("repair")
            return geo
        monkeypatch.setattr(lanes, "repair_geometry", repair)
        monkeypatch.setattr(lanes, "repair_geometry_v4", repair)
        reg.add_annotation("sample-0", "source_label", source, "fixture-source", "unused", params={"human":True})
        monkeypatch.setattr(lanes, "load_boundary_maps", lambda *args: {"lane":np.ones((874,1164),np.float32),
                                                                      "solid":np.ones((874,1164),np.float32),
                                                                      "dashed":np.zeros((874,1164),np.float32)})
        def audi(geo, paint, *args, **kwargs):
            assert kwargs["solid_mask"].shape == paint.shape
            assert not kwargs["dashed_mask"].any()
            calls.append("audi")
            return geo
        monkeypatch.setattr(lanes, "repair_geometry_a2d2", audi)
        lanes.run(settings, reg, [row], force=True, per_teacher=False)
        assert calls == (["repair","repair"] + (["audi"] if source == "a2d2" else []) if repaired else [])
        assert json.loads(reg.annotation("sample-0", "geometry", "geometry")["license_ids"]) == ["a2d2"]


def test_audi_converter_version_does_not_invalidate_legacy_comma_geometry(settings):
    import dataclasses
    import hashlib
    params = lanes.params_for(settings)
    original = "geometry:" + hashlib.sha1(json.dumps(["fixture-fused", dataclasses.asdict(params),
                                                      lanes.GEOMETRY_CODE_VERSION], sort_keys=True).encode()).hexdigest()[:12]
    assert lanes.geometry_version("fixture-fused", params) == original
    assert lanes.geometry_version("fixture-fused", params, source="comma10k") == original
    assert lanes.geometry_version("fixture-fused", params, source="a2d2") != original


def audi_fixture(tmp_path, count=6):
    settings = build_review_fixture(tmp_path)
    store = ReviewStore(settings)
    ids = ["audi-" + str(index) for index in range(count)]
    with store.registry() as reg:
        template = dict(reg.image("sample-0"))
        geo = json.loads(settings.path("lane_geometry", "sample-0.json").read_text())
        for index, image_id in enumerate(ids):
            record = {key: value for key, value in template.items() if key not in ("created_at","updated_at")}
            record.update(image_id=image_id,source="a2d2",sha256="audi-fixture-"+str(index),
                          source_ref="fixture:"+str(index), session_id="audi-fixture-drive",status="ingested",
                          split="train", tags={"lighting":{"value":"day"},"review_batch":{"value":"audi-fixture"}})
            reg.add_image(**record)
            geometry = {**deepcopy(geo), "image_id":image_id,
                        "source_converter":{"risk_family":"unchanged_clean","warnings":[]}}
            path = settings.path("lane_geometry", image_id + ".json")
            path.write_text(json.dumps(geometry))
            reg.add_annotation(image_id,"geometry","geometry","fixture-auto",str(path),license_ids=["a2d2"])
            mask = settings.root / (image_id + "-mask.png")
            Image.fromarray(np.zeros((12,12),np.uint8)).save(mask)
            reg.add_annotation(image_id,"source_label","a2d2","source-fixture",str(mask),
                               license_ids=["a2d2"], params={"format":"class_map","human":True})
            reg.set_qc(image_id,.8,"medium",.2,[],False,"fixture-auto")
            teacher = settings.path("lane_geometry","teachers","comma_segnet_fresh",image_id+".json")
            teacher.parent.mkdir(parents=True,exist_ok=True)
            teacher.write_text(json.dumps(geometry))
    return settings, store, ids


def approve(store, image_id):
    store.decide(image_id,"approved","fixture-human",store.detail(image_id)["revision"],str(uuid.uuid4()))


def test_audi_staging_hidden_and_held_until_three_real_human_samples_pass(tmp_path):
    settings, store, ids = audi_fixture(tmp_path)
    report = run(settings,apply=True,source_name="a2d2",image_ids=ids,include_staged=True)
    samples = report["sample_ids"]
    assert report["policy"] == A2D2_POLICY
    assert report["counts"] == {"awaiting_audit":3,"sample":3}
    assert len(samples) == 3
    assert not set(ids) & {row["image_id"] for row in store.batch("fixture")}
    with store.registry() as reg:
        assert not reg.conn.execute("SELECT 1 FROM source_conversion_checks WHERE image_id LIKE 'sample-%'").fetchone()
        for image_id in ids:
            assert reg.image(image_id)["status"] == "ingested"
            reg.update_image(image_id,status="curated")
    for image_id in samples[:2]:
        approve(store,image_id)
    refresh_groups(settings)
    with store.registry() as reg:
        assert not reg.conn.execute("SELECT 1 FROM source_conversion_checks WHERE state='validated'").fetchone()
    approve(store,samples[2]); refresh_groups(settings)
    with store.registry() as reg:
        group = reg.conn.execute("SELECT * FROM source_validation_groups").fetchone()
        assert group["state"] == "passed"
        assert json.loads(group["evidence"])["policy"] == A2D2_POLICY
        held = set(ids) - set(samples)
        assert all(validated_record(reg,image_id,verify_files=True) is not None for image_id in held)
        assert all(reg.annotation(image_id,"human") is None and not reg.image(image_id)["locked"] for image_id in held)
        assert reg.count("SELECT count(*) FROM review_events") == 3


def test_audi_checks_exclude_live_claim_and_any_human_annotation(tmp_path):
    settings, store, ids = audi_fixture(tmp_path)
    with store.registry() as reg:
        reg.conn.execute("INSERT INTO review_claims VALUES (?,?,?)",(ids[0],"active-human",time.time()+300))
        reg.conn.commit()
        reg.add_annotation(ids[1],"human","fixture-human","old-human","unused")
    report = run(settings,apply=True,source_name="a2d2",image_ids=ids,include_staged=True)
    assert sum(report["counts"].values()) == 4
    with store.registry() as reg:
        assert not reg.conn.execute("SELECT 1 FROM source_conversion_checks WHERE image_id IN (?,?)",ids[:2]).fetchone()
        assert reg.conn.execute("SELECT 1 FROM review_claims WHERE image_id=?",(ids[0],)).fetchone()


def test_audi_staging_never_clears_an_existing_comma_review_claim(tmp_path):
    settings,store,ids = audi_fixture(tmp_path)
    with store.registry() as reg:
        geometry = reg.annotation("sample-0","geometry","geometry")
        reg.add_annotation("sample-0","source_label","comma10k","fixture-source",geometry["path"],params={"human":True})
        source = reg.annotation("sample-0","source_label")
        reg.conn.execute("INSERT INTO source_conversion_checks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("sample-0","old-comma-batch","old-comma-group","awaiting_audit",
                          geometry["id"],geometry["model_version"],geometry["path"],file_sha256(geometry["path"]),
                          source["id"],source["path"],file_sha256(source["path"]),"{}",time.time()))
        reg.conn.execute("INSERT INTO review_claims VALUES (?,?,?)",("sample-0","existing-comma-human",time.time()+300))
        reg.conn.commit()
    run(settings,apply=True,source_name="a2d2",image_ids=ids,include_staged=True)
    with store.registry() as reg:
        assert reg.conn.execute("SELECT owner FROM review_claims WHERE image_id='sample-0'").fetchone()[0] == "existing-comma-human"


def test_audi_groups_keep_lane_four_separate_and_refuse_unsupported_sources(tmp_path):
    row = {"split":"train","tags":json.dumps({"lighting":{"value":"day"}})}
    three = {"lanes":[{"slot":slot} for slot in (1,2,3)],"source_converter":{"risk_family":"boundary_join"}}
    four = {**three,"lanes":three["lanes"]+[{"slot":4}]}
    assert a2d2_strata(row,three) != a2d2_strata(row,four)
    assert a2d2_strata(row,four).endswith(":lanes-4")
    settings, _, ids = audi_fixture(tmp_path)
    with pytest.raises(ValueError,match="supported human"):
        run(settings,source_name="a_model")
    with pytest.raises(ValueError,match="explicit Audi"):
        run(settings,source_name="a2d2",include_staged=True)
    with pytest.raises(ValueError,match="three samples"):
        run(settings,source_name="a2d2",image_ids=ids,minimum_per_group=2)


def test_audi_changed_source_bytes_block_release_after_sample_approvals(tmp_path):
    settings, store, ids = audi_fixture(tmp_path)
    report = run(settings,apply=True,source_name="a2d2",image_ids=ids,include_staged=True)
    with store.registry() as reg:
        for image_id in ids:
            reg.update_image(image_id,status="curated")
        held = next(image_id for image_id in ids if image_id not in report["sample_ids"])
        source = reg.annotation(held,"source_label")
        with open(source["path"],"ab") as handle:
            handle.write(b"changed")
    for image_id in report["sample_ids"]:
        approve(store,image_id)
    refresh_groups(settings)
    with store.registry() as reg:
        assert reg.conn.execute("SELECT state FROM source_validation_groups").fetchone()[0] == "blocked"
        assert validated_record(reg,held,verify_files=True) is None


def test_audi_apply_rolls_back_if_geometry_changes_between_inspection_and_transaction(tmp_path,monkeypatch):
    settings,store,ids = audi_fixture(tmp_path)
    select = source_validation.select_samples
    def changed(*args,**kwargs):
        selected = select(*args,**kwargs)
        with store.registry() as reg:
            geometry = reg.annotation(ids[0],"geometry","geometry")
            with open(geometry["path"],"a") as handle:
                handle.write(" ")
        return selected
    monkeypatch.setattr(source_validation,"select_samples",changed)
    with pytest.raises(RuntimeError,match="geometry changed"):
        run(settings,apply=True,source_name="a2d2",image_ids=ids,include_staged=True)
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM source_conversion_checks") == 0
        assert reg.count("SELECT count(*) FROM source_validation_groups") == 0
