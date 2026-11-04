"""Audi import worker tests use disposable images and mocked downloads/models."""
from copy import deepcopy
import json
import random

import pytest

from bcdata import import_audi
from bcdata.paint_proposals import NAME, VERSION
from bcdata.review_store import ReviewStore
from bcdata.source_validation import refresh_groups
from tests.test_review import build_review_fixture


def fixture(tmp_path,monkeypatch,count=6):
    settings = build_review_fixture(tmp_path)
    for name in ("a2d2-LICENSE.txt","a2d2-NOTICE.txt"):
        settings.path("licenses",name).parent.mkdir(exist_ok=True)
        settings.path("licenses",name).write_text("Disposable fixture notice")
    items = []
    for index in range(count):
        item = {"img":f"camera_lidar_semantic/fixture/image-{index}.png", "name":f"image-{index}.png",
                "session":"a2d2:fixture-drive","split":"train","frame":index}
        for key in ("img","mask","metadata","palette","calibration"):
            item[key+"_meta"] = {"key":item["img"] if key == "img" else f"camera_lidar_semantic/fixture/{key}-{index}.png",
                                 "size":1,"etag":"0"*32}
        items.append(item)
    plan = {"schema":"a2d2_import_plan:v1","source":"a2d2","base_url":import_audi.a2d2.BASE_URL,
            "camera":"front_center","selected":count,"items":items,"items_sha256":import_audi.canonical_sha(items)}
    plan["plan_sha256"] = import_audi.canonical_sha(plan)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    store = ReviewStore(settings)
    hashes = [random.Random(8913+index).getrandbits(64) for index in range(count)]
    calls = {"downloads":[],"prepared":[],"loads":0,"unloads":0}
    def ingest(settings,reg,licenses,*,items,**kwargs):
        calls["downloads"].extend(item["img"] for item in items)
        for item in items:
            index = int(item["name"].removeprefix("image-").removesuffix(".png"))
            image_id = "audi-fixture-"+str(index)
            if reg.image(image_id):
                continue
            template = dict(reg.image("sample-0"))
            record = {key:value for key,value in template.items() if key not in ("created_at","updated_at")}
            record.update(image_id=image_id,source="a2d2",source_ref=import_audi._ref(item),
                          session_id=item["session"],frame_index=index,split=item["split"],status="ingested",
                          sha256="fake-audi-"+str(index),phash=f"{hashes[index]:016x}",license_id="a2d2")
            reg.add_image(**record)
            source = import_audi.a2d2.safe_path(settings.path("raw","a2d2"),item["mask_meta"]["key"])
            source.parent.mkdir(parents=True,exist_ok=True)
            source.write_bytes(b"disposable source bytes")
            reg.add_annotation(image_id,"source_label","a2d2","s3:"+item["mask_meta"]["etag"],str(source),
                               license_ids=["a2d2"],params={"format":"class_map","human":True})
        return {"errors":[]}
    def prepare(settings,reg,licenses,teacher,row):
        image_id = row["image_id"]
        assert reg.image(image_id)["status"] == "ingested"
        assert image_id not in {item["image_id"] for item in store.batch("fixture-staging-observer")}
        calls["prepared"].append(image_id)
        geo = json.loads(settings.path("lane_geometry","sample-0.json").read_text())
        geo.update(image_id=image_id,source_converter={"risk_family":"unchanged_clean","warnings":[]})
        for kind,producer in (("teacher","source_labels"),("fused","fusion"),("geometry","geometry"),("teacher",NAME)):
            out = settings.root / f"{image_id}-{kind}-{producer}.json"
            out.write_text(json.dumps(geo))
            reg.add_annotation(image_id,kind,producer,"fixture-auto",str(out),license_ids=["a2d2"])
        candidate = settings.path("lane_geometry","teachers",NAME,image_id+".json")
        candidate.parent.mkdir(parents=True,exist_ok=True)
        candidate.write_text(json.dumps({**geo,"teacher_version":VERSION}))
        reg.set_qc(image_id,.9,"source_label",.1,[],False,"fixture-auto")
    class Teacher:
        def unload(self):
            calls["unloads"] += 1
    def load(settings):
        assert settings.get("teachers.device") == "cpu" and settings.get("teachers.amp") is False
        calls["loads"] += 1
        return Teacher()
    monkeypatch.setattr(import_audi.a2d2,"ingest",ingest)
    monkeypatch.setattr(import_audi,"_prepare",prepare)
    monkeypatch.setattr(import_audi,"_load_cpu_teacher",load)
    return settings,store,path,plan,calls,hashes


def test_hidden_staging_publishes_only_after_three_sample_gate_exists(tmp_path,monkeypatch):
    settings,store,path,plan,calls,_ = fixture(tmp_path,monkeypatch)
    result = import_audi.run(settings,path,16,chunk_size=2)
    assert result["state"] == "ready" and result["published"] == 6
    assert result["audit_counts"] == {"awaiting_audit":3,"sample":3}
    assert calls["loads"] == calls["unloads"] == 1
    assert len(calls["prepared"]) == 6
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM review_events") == 0
        assert reg.count("SELECT count(*) FROM images WHERE source='a2d2' AND locked=1") == 0
        group = reg.conn.execute("SELECT * FROM source_validation_groups").fetchone()
        assert group["state"] == "awaiting_audit"
        assert len(json.loads(group["sample_ids"])) == 3
    refresh_groups(settings)
    assert store.stats()["source_validated"] == 0
    # A repeated completed request cannot repeat downloads or annotations.
    before = deepcopy(calls)
    assert import_audi.run(settings,path,16)["run_id"] == result["run_id"]
    assert calls == before


def test_interrupted_publication_resumes_existing_audit_without_rewriting_labels(tmp_path,monkeypatch):
    settings,store,path,plan,calls,_ = fixture(tmp_path,monkeypatch)
    publish = import_audi.publish
    monkeypatch.setattr(import_audi,"publish",lambda *args: (_ for _ in ()).throw(RuntimeError("simulated interruption")))
    with pytest.raises(RuntimeError,match="simulated interruption"):
        import_audi.run(settings,path,16,chunk_size=2)
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM images WHERE source='a2d2' AND status='curated'") == 0
        annotations = [tuple(row) for row in reg.conn.execute("SELECT * FROM annotations WHERE image_id LIKE 'audi-%'")]
        groups = [tuple(row) for row in reg.conn.execute("SELECT * FROM source_validation_groups")]
    monkeypatch.setattr(import_audi,"publish",publish)
    result = import_audi.run(settings,path,16,chunk_size=2)
    assert result["published"] == 6 and calls["loads"] == 1
    with store.registry() as reg:
        assert annotations == [tuple(row) for row in reg.conn.execute("SELECT * FROM annotations WHERE image_id LIKE 'audi-%'")]
        assert groups == [tuple(row) for row in reg.conn.execute("SELECT * FROM source_validation_groups")]


def test_existing_human_image_is_skipped_and_all_saved_bytes_remain(tmp_path,monkeypatch):
    settings,store,path,plan,calls,_ = fixture(tmp_path,monkeypatch)
    with store.registry() as reg:
        # Simulate an older imported Audi image that already has a human label.
        old = dict(reg.image("sample-0"))
        old = {key:value for key,value in old.items() if key not in ("created_at","updated_at")}
        old.update(image_id="audi-fixture-0",source="a2d2",source_ref=import_audi._ref(plan["items"][0]),
                   sha256="old-audi-human",review_status="approved",locked=1)
        reg.add_image(**old)
        human = settings.root / "old-human.json"
        human.write_bytes(b"original human label")
        reg.add_annotation("audi-fixture-0","human","fixture-human","human-original",str(human))
        before = tuple(reg.image("audi-fixture-0"))
        annotations = [tuple(row) for row in reg.annotations("audi-fixture-0")]
    result = import_audi.run(settings,path,16,chunk_size=2)
    assert result["published"] == 5
    assert plan["items"][0]["img"] not in calls["downloads"]
    with store.registry() as reg:
        assert tuple(reg.image("audi-fixture-0")) == before
        assert [tuple(row) for row in reg.annotations("audi-fixture-0")] == annotations
    assert human.read_bytes() == b"original human label"


def test_audi_dedup_runs_before_model_and_keeps_original_files(tmp_path,monkeypatch):
    settings,store,path,plan,calls,hashes = fixture(tmp_path,monkeypatch)
    hashes[1] = hashes[0] ^ 3  # pHash distance two, despite different content IDs.
    result = import_audi.run(settings,path,16,chunk_size=2)
    assert result["excluded"] == 1 and result["published"] == 5
    assert "audi-fixture-1" not in calls["prepared"]
    with store.registry() as reg:
        row = reg.image("audi-fixture-1")
        assert row["status"] == "dropped"
        assert row["reject_reason"] == "near_duplicate_phash:audi-fixture-0"
        assert not reg.conn.execute("SELECT 1 FROM source_conversion_checks WHERE image_id=?",(row["image_id"],)).fetchone()
        assert reg.annotation(row["image_id"],"source_label") is not None


def test_pilot_retains_full_plan_identity_and_reports_selected_subset(tmp_path,monkeypatch):
    settings,_,path,plan,_,_ = fixture(tmp_path,monkeypatch)
    result = import_audi.run(settings,path,16,limit=3)
    assert result["plan_sha256"] == plan["plan_sha256"]
    assert result["items_sha256"] == plan["items_sha256"]
    assert result["selection_sha256"] == import_audi.canonical_sha(plan["items"][:3])
    assert result["pilot"] is True and result["total"] == 3 and result["full_plan_total"] == 6


def test_missing_cached_model_stops_hidden_with_no_fake_audits(tmp_path,monkeypatch):
    settings,store,path,_,calls,_ = fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(import_audi,"_load_cpu_teacher",lambda *args: (_ for _ in ()).throw(RuntimeError("missing pinned model cache")))
    with pytest.raises(RuntimeError,match="missing pinned"):
        import_audi.run(settings,path,16)
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM images WHERE source='a2d2' AND status='curated'") == 0
        assert reg.count("SELECT count(*) FROM source_conversion_checks") == 0
    assert calls["prepared"] == []
    assert json.loads(settings.path("reports","a2d2_import.json").read_text())["state"] == "error"


def test_low_working_space_stops_before_model_or_publication_and_can_resume(tmp_path,monkeypatch):
    settings,store,path,_,calls,_ = fixture(tmp_path,monkeypatch)
    usage = import_audi.shutil.disk_usage(settings.root)
    monkeypatch.setattr(import_audi.shutil,"disk_usage",lambda _:usage._replace(free=4_900_000_000))
    # The raw transfer already passed its separate budget check. This guard
    # protects output generation when another job consumes working space.
    monkeypatch.setattr(import_audi,"check_download_budget",lambda *args:None)
    with pytest.raises(OSError,match="working space"):
        import_audi.run(settings,path,16)
    assert calls["loads"] == 0 and calls["prepared"] == []
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM images WHERE source='a2d2' AND status='curated'") == 0
        assert reg.count("SELECT count(*) FROM source_conversion_checks") == 0
    monkeypatch.setattr(import_audi.shutil,"disk_usage",lambda _:usage._replace(free=25_000_000_000))
    assert import_audi.run(settings,path,16)["state"] == "ready"


def test_pinned_plan_changes_are_rejected_before_any_download(tmp_path,monkeypatch):
    settings,_,path,plan,calls,_ = fixture(tmp_path,monkeypatch)
    plan["items"][0]["split"] = "test"
    path.write_text(json.dumps(plan))
    with pytest.raises(ValueError,match="item hash"):
        import_audi.run(settings,path,16)
    assert calls["downloads"] == []


def test_partial_ready_retries_hidden_failure_preserving_already_published_images(tmp_path,monkeypatch):
    settings,store,path,_,calls,_ = fixture(tmp_path,monkeypatch)
    prepare = import_audi._prepare
    def fail_one(settings,reg,licenses,teacher,row):
        if row["image_id"] == "audi-fixture-5":
            raise RuntimeError("temporary prediction failure")
        return prepare(settings,reg,licenses,teacher,row)
    monkeypatch.setattr(import_audi,"_prepare",fail_one)
    result = import_audi.run(settings,path,16)
    assert result["state"] == "partial_ready" and result["published"] == 5 and result["failed"] == 1
    with store.registry() as reg:
        assert reg.image("audi-fixture-5")["status"] == "ingested"
        existing = [tuple(row) for row in reg.conn.execute("SELECT * FROM annotations WHERE image_id='audi-fixture-0'")]
    monkeypatch.setattr(import_audi,"_prepare",prepare)
    result = import_audi.run(settings,path,16)
    assert result["state"] == "ready" and result["published"] == 6 and result["failed"] == 0
    assert result["newly_published"] == 1
    with store.registry() as reg:
        assert existing == [tuple(row) for row in reg.conn.execute("SELECT * FROM annotations WHERE image_id='audi-fixture-0'")]
        assert reg.count("SELECT count(*) FROM review_events") == 0


@pytest.mark.parametrize("changed_module", ["qc", "source_converter_a2d2"])
def test_changed_processing_fingerprint_cannot_publish_a_previously_audited_hidden_image(tmp_path,monkeypatch,changed_module):
    settings,store,path,_,_,_ = fixture(tmp_path,monkeypatch)
    publish = import_audi.publish
    monkeypatch.setattr(import_audi,"publish",lambda *args: (_ for _ in ()).throw(RuntimeError("interrupted")))
    with pytest.raises(RuntimeError,match="interrupted"):
        import_audi.run(settings,path,16)
    monkeypatch.setattr(import_audi,"publish",publish)
    sha = import_audi.file_sha256
    changed_path = getattr(import_audi,changed_module).__file__
    monkeypatch.setattr(import_audi,"file_sha256",lambda path: "changed-processing-version" if str(path) == str(changed_path) else sha(path))
    result = import_audi.run(settings,path,16)
    assert result["state"] == "partial_ready" and result["published"] == 0
    assert result["failed"] == 6
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM images WHERE source='a2d2' AND status='curated'") == 0
