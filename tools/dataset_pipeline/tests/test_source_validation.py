import csv
import json
from pathlib import Path
import uuid

import numpy as np
import pytest
from PIL import Image

from bcdata.review_export import export_reviewed
from bcdata.review_store import ReviewStore
from bcdata.source_validation import classify, refresh_groups, run, select_samples, strata, validated_record
from tests.test_review import build_review_fixture


def setup_source(tmp_path):
    settings = build_review_fixture(tmp_path)
    store = ReviewStore(settings)
    with store.registry() as reg:
        for index in range(3):
            image_id = f"sample-{index}"
            reg.update_image(image_id, split="train", tags={"lighting": {"value": "day"}, "review_batch": {"value": "source-fixture"}})
            mask = settings.root / (image_id + "-source.png")
            Image.fromarray(np.ones((874, 1164), np.uint8)).save(mask)
            reg.add_annotation(image_id, "source_label", "comma10k", "source:fixture", str(mask), params={"human": True, "format": "class_map"})
            path = settings.path("lane_geometry", "teachers", "comma_segnet_fresh", image_id + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(settings.path("lane_geometry", image_id + ".json").read_text())
    report = run(settings, sample_size=2, apply=True, minimum_per_group=2)
    with store.registry() as reg:
        held = reg.conn.execute("SELECT image_id FROM source_conversion_checks WHERE state='awaiting_audit'").fetchone()[0]
    return settings, store, report["sample_ids"], held


def test_future_groups_default_to_three_even_when_total_target_is_smaller():
    groups = {'large':['a','b','c','d'], 'pair':['e','f'], 'singleton':['g']}
    for strata in (None, {image_id:image_id for ids in groups.values() for image_id in ids}):
        selected = select_samples(groups, target=1, sample_strata=strata)
        assert {key:len(ids) for key,ids in selected.items()} == {'large':3,'pair':2,'singleton':1}


def test_total_sample_target_never_expands_a_group_beyond_three():
    groups = {f"group-{index}": [f"image-{index}-{n}" for n in range(100)] for index in range(3)}
    for strata in (None, {image_id: f"stratum-{n % 5}" for ids in groups.values() for n, image_id in enumerate(ids)}):
        samples = select_samples(groups, target=64, sample_strata=strata)
        assert {key: len(ids) for key, ids in samples.items()} == {key: 3 for key in groups}
        assert sum(map(len, samples.values())) == 9
        assert len(select_samples({"only": groups["group-0"]}, target=64, sample_strata=strata)["only"]) == 3
        assert select_samples({"pair": ["one", "two"]}, target=64)["pair"] in (["one", "two"], ["two", "one"])


def test_explicit_sample_limits_allow_two_but_reject_maximum_below_minimum():
    assert len(select_samples({"one": list("abcde")}, minimum_per_group=2, maximum_per_group=2)["one"]) == 2
    with pytest.raises(ValueError, match="Maximum samples"):
        select_samples({"one": list("abcde")}, minimum_per_group=3, maximum_per_group=2)


def approve_samples(store, samples):
    for image_id in samples:
        store.decide(image_id, "approved", "Nick", store.detail(image_id)["revision"], str(uuid.uuid4()))


def test_sample_audit_gates_source_export_without_fake_human_approval(tmp_path):
    settings, store, samples, held = setup_source(tmp_path)
    assert store.stats()["pending"] == 2 and store.stats()["awaiting_source_audit"] == 1
    assert {store.next_image("owner", status="audit_sample")["image_id"]} <= set(samples)
    assert held not in {row["image_id"] for row in store.batch("owner")}
    assert export_reviewed(settings)["exported"] == 0
    approve_samples(store, samples)
    refresh_groups(settings)
    stats = store.stats()
    assert stats["pending"] == 0 and stats["source_validated"] == 1
    assert stats["accepted"] == stats["reviewed"] == 2
    with store.registry() as reg:
        assert reg.image(held)["locked"] == 0
        assert reg.annotation(held, "human") is None
        assert reg.count("SELECT count(*) FROM review_events") == 2
    exported = export_reviewed(settings)
    assert exported["exported"] == 3 and exported["source_validated"] == 1
    manifest = list(csv.DictReader(settings.path("exports", "ufld_v2", "BrakeCheck_Reviewed", "manifest.csv").open()))
    record = next(row for row in manifest if row["image_id"] == held)
    assert record["label_origin"] == record["decision"] == "source_validated"
    assert json.loads(record["source_validation"])["evidence"]["sample_count"] == 2
    with store.registry() as reg:
        event = reg.conn.execute("SELECT event_id FROM review_events WHERE image_id=?", (samples[0],)).fetchone()[0]
    store.undo(event, "Nick")
    refresh_groups(settings)
    assert store.stats()["source_validated"] == 0 and store.stats()["awaiting_source_audit"] == 1
    assert export_reviewed(settings)["exported"] == 1


def test_corrected_audit_blocks_group_without_dumping_all_images_into_manual_queue(tmp_path):
    settings, store, samples, held = setup_source(tmp_path)
    store.decide(samples[0], "corrected", "Nick", 0, str(uuid.uuid4()), lanes=[])
    refresh_groups(settings)
    assert store.stats()["source_audit_groups"][0]["state"] == "blocked"
    assert store.stats()["source_validated"] == 0
    assert held not in {row["image_id"] for row in store.batch("owner")}


def test_changed_geometry_invalidates_source_eligibility_and_returns_to_manual_queue(tmp_path):
    settings, store, samples, held = setup_source(tmp_path)
    approve_samples(store, samples); refresh_groups(settings)
    with store.registry() as reg:
        ann = reg.annotation(held, "geometry", "geometry")
        new_path = settings.root / "changed.json"
        new_path.write_text(Path(ann["path"]).read_text())
        reg.add_annotation(held, "geometry", "geometry", ann["model_version"], str(new_path))
        assert validated_record(reg, held, verify_files=True) is None
    assert store.stats()["pending"] == 1
    assert store.stats()["source_validated"] == 0
    assert export_reviewed(settings)["exported"] == 2


def test_valid_border_exit_is_not_a_manual_task_but_missing_lines_are():
    geo = {"width": 100, "height": 100, "lanes": [{"slot": 2, "points": [[-2, 80], [20, 60], [30, 40]], "x_ref": 20}]}
    assert classify(geo, [], geo) == []
    assert "model_source_count_disagreement" in classify({**geo, "lanes": []}, ["no_lanes"], geo)
    assert "low_mask_coverage" in classify(geo, ["low_mask_coverage"], geo)


def test_new_converter_groups_by_conversion_risk_and_samples_across_lighting_lane_counts():
    row = {"split": "train", "tags": json.dumps({"lighting": {"value": "day"}})}
    geo = {"lanes": [{"slot": 2}], "source_converter": {"joined_chains": 1, "recovered_short_chains": 0}}
    assert strata(row, geo) == "train:risk-extent_changed:source-positive"
    assert strata(row, {**geo, "source_converter": {"recovered_short_chains": 1}}) == "train:risk-lane_added:source-positive"
    assert strata(row, {**geo, "source_converter": {"joined_chains": 1, "recovered_short_chains": 1}}) == "train:risk-multiple:source-positive"
    assert strata(row, {**geo, "lanes": [], "source_converter": {"version": "new"}}) == "train:risk-unchanged:source-zero"
    legacy = {"lanes": [{"slot": 2}]}
    assert strata(row, legacy) == "train:day:lanes-1"
    ids = [f"day-{index}" for index in range(20)] + ["night-0", "night-1", "day-two-lanes"]
    buckets = {image_id: "day:lanes-1" if image_id.startswith("day-") else "night:lanes-1" for image_id in ids}
    buckets["day-two-lanes"] = "day:lanes-2"
    selected = select_samples({"risk-cohort": ids}, target=3, sample_strata=buckets)["risk-cohort"]
    assert {buckets[image_id] for image_id in selected} == {"day:lanes-1", "night:lanes-1", "day:lanes-2"}


def test_source_converter_uncertainty_stays_flagged_even_when_qc_has_no_issue():
    geo = {"width": 100, "height": 100, "lanes": [{"slot": 2, "points": [[10, 80], [20, 50]], "x_ref": 10}],
           "source_converter": {"warnings": ["inward_or_duplicate_fragment"]}}
    assert classify(geo, [], geo) == ["repair_inward_or_duplicate_fragment"]
    assert classify({**geo, "source_converter": {"warnings": []}}, [], geo) == []
