"""Converted merging preserves cleared truth and never reads held-out photos for fitting."""
import copy
import json

import numpy as np
from PIL import Image
import pytest

from bcdata.bootstrap import atomic_json
from bcdata.converted_merge import _render, build_converted_merge, load_converted_merge, plan_converted_merge
from bcdata.simulation_review import SimulationCorrection
from bcdata.teachers.base import file_sha256
from bcdata.ufld_baseline import NativeDataset, load_frozen_export
from tests.test_real_source_review import decision, fixture as real_fixture, repin
from tests.test_ufld_experiment import _rgb, experiment_fixture


def source_fixture(base, *, held=False, intended="train", empty=False, complete=False):
    root, rows, store = real_fixture(base.temp)
    reason = "native_coverage_incomplete" if complete else "potential_transverse_marking"
    for index, row in enumerate(rows):
        path = root / row["image"]
        Image.fromarray(_rgb(index + 20)[:72, :128]).save(path, quality=95)
        row["source_sha256"] = file_sha256(path)
        if held:
            row.update(split="hold", intended_split=intended, qc_reasons=[reason])
    repin(root, rows)
    if held:
        item = store.detail(rows[0]["image_id"])
        store.correct(SimulationCorrection(image_id=item["image_id"], revision=0,
            fingerprint=item["fingerprint"], lanes=[] if empty else item["geometry"]["lanes"],
            resolved_qc_reasons=[reason], approve=True, complete_annotation=complete,
            no_visible_lane_paint=bool(complete and empty)), "Human")
    else:
        for index, row in enumerate(rows):
            store.decide(decision(store.detail(row["image_id"]), "approve", index), "Human")
    atomic_json(base.dataset / "split_inventory.json", base.inventory)
    return root, rows, store


def build(base, **kwargs):
    return build_converted_merge(base.temp, base.base, base.output,
                                 sources=["college_station_lane_marking"], **kwargs)


def test_cleared_real_merge_preserves_base_and_pins_source_evidence(experiment_fixture):
    f = experiment_fixture
    root, rows, _ = source_fixture(f)
    originals = {path: path.read_bytes() for folder in (root, f.dataset)
                 for path in folder.rglob("*") if path.is_file()}
    phases = []
    report = build(f, progress=lambda value: phases.append(value["phase"]))
    dataset, records, marker = load_converted_merge(f.output)
    assert report["extra_train_images"] == 3 and report["train_images"] == 5 and report["val_images"] == 1
    assert report["test_images_used"] == report["source_val_images_used"] == 0
    assert records[:len(f.records)] == f.records
    assert marker["collection_id"] == "converted_only" and marker["experimental"] is False
    assert load_frozen_export(f.output)[1] == records
    assert len(NativeDataset(dataset, "train")) == 5 and len(NativeDataset(dataset, "val")) == 1
    assert json.loads((dataset / "reserved_test_ids.json").read_text()) == ["reserved-test"]
    for record in records[len(f.records):]:
        assert record["decision"] == record["label_origin"] == "source_validated"
        assert record["isolated_source_provenance"]["clearance"]["sample_decisions"]
        assert (dataset / record["native_evidence"]["image"]).read_bytes() == (root / next(
            row["image"] for row in rows if row["image_id"] == record["image_id"])).read_bytes()
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert "rendering_cleared_real_sources" in phases and phases[-1] == "complete"


@pytest.mark.parametrize("points", [
    [[20, 5], [18, 10]],  # Visible paint entirely above the output row anchors.
    [[20, 40], [18, 41]],  # Exactly one output row, insufficient for a lane.
])
def test_cleared_photo_without_two_output_rows_is_skipped_without_erasing_approval(experiment_fixture, points):
    f = experiment_fixture
    root, rows, store = source_fixture(f)
    skipped = rows[0]
    geometry_path = root / skipped["geometry"]
    geometry = json.loads(geometry_path.read_bytes())
    geometry["lanes"] = [{"slot": 2, "points": points}]
    atomic_json(geometry_path, geometry)
    skipped["geometry_sha256"] = file_sha256(geometry_path)
    repin(root, rows)
    for number, row in enumerate(rows):
        store.decide(decision(store.detail(row["image_id"]), "approve", 3100 + number), "Human")
    votes = copy.deepcopy(store.decisions())
    before = {path: path.read_bytes() for folder in (root, f.dataset)
              for path in folder.rglob("*") if path.is_file()}
    plan = plan_converted_merge(f.temp, f.base, sources=["college_station_lane_marking"])
    assert plan["candidate_train_images"] == 2
    expected_rejection = {"image_id": skipped["image_id"], "source": "college_station_lane_marking",
                          "reason": "no_trainable_output_rows"}
    assert expected_rejection in plan["rejected"]
    report = build(f)
    _, records, _ = load_converted_merge(f.output)
    assert report["extra_train_images"] == 2
    assert expected_rejection in report["rejected"]
    assert records[:len(f.records)] == f.records
    assert {row["image_id"] for row in records[len(f.records):]} == {row["image_id"] for row in rows[1:]}
    assert store.decisions() == votes
    assert store.decisions()[skipped["image_id"]]["action"] == "approve"
    assert all(path.read_bytes() == raw for path, raw in before.items())


@pytest.mark.parametrize("empty,complete", [(False, False), (True, False), (True, True), (False, True)])
def test_individually_approved_hold_addition_never_releases_held_peers(experiment_fixture, empty, complete):
    f = experiment_fixture
    root, rows, store = source_fixture(f, held=True, empty=empty, complete=complete)
    report = build(f)
    dataset, records, _ = load_converted_merge(f.output)
    assert report["extra_train_images"] == report["individually_cleared_held_images"] == 1
    record = records[-1]
    assert record["image_id"] == rows[0]["image_id"] and record["decision"] == "corrected"
    assert record["original_split"] == "hold" and record["split"] == "train"
    assert store.detail(record["image_id"])["split"] == "hold"
    assert store.status()["groups"][0]["qc_flagged_images"] == 2
    if empty:
        cache = json.loads((dataset / "culane_anno_cache.json").read_text())[record["image"]]
        assert all(point[0] < 0 for lane in cache for point in lane)
        assert record["lanes"] == 0
        assert record["isolated_source_provenance"]["empty_label_verification"] == "human_approved_empty_correction"


@pytest.mark.parametrize("intended", ["val", "test"])
def test_individual_hold_approval_never_imports_intended_evaluation_data(experiment_fixture, intended):
    f = experiment_fixture
    _, _, store = source_fixture(f, held=True, intended=intended, complete=True)
    assert store.eligible_records()["train_images"] == 0
    plan = plan_converted_merge(f.temp, f.base, sources=["college_station_lane_marking"])
    assert plan["candidate_train_images"] == 0
    assert not f.output.exists()


def test_cross_split_source_session_conflict_blocks_before_output_write(experiment_fixture):
    f = experiment_fixture
    root, rows, _ = source_fixture(f)
    rows[0]["split"] = "test"
    rows[0]["group_key"] = "test/other-group"
    audits = json.loads((root / "audits.json").read_text())
    audits["groups"] = [
        {"group_key": rows[0]["group_key"], "split": "test", "count": 1, "sample_ids": [rows[0]["image_id"]]},
        {"group_key": rows[1]["group_key"], "split": "train", "count": 2,
         "sample_ids": [row["image_id"] for row in rows[1:]]}]
    atomic_json(root / "audits.json", audits)
    repin(root, rows)
    with pytest.raises(ValueError, match="Recording session crosses"):
        build(f)
    assert not f.output.exists() and not list(f.temp.glob("experiment.preparing-*"))


def test_observed_section_gaps_and_visibility_survive_anchor_sampling(tmp_path):
    rgb = np.zeros((72, 128, 3), np.uint8)
    lane = {"slot": 2, "points": [[20, 70, True], [25, 60, True], [30, 50, False],
                                 [35, 40, True], [40, 30, True]],
            "fit": {"basis": "interp", "ys": [0, 71], "xs": [120, 120]}}
    entry, count, _ = _render(rgb, {"lanes": [lane]}, tmp_path, "images/visible.jpg")
    assert count == 1
    curve = {point[1]: point[0] for point in entry[1]}
    assert 0 < curve[560] < 500  # corrected points, never the stale x=120 fit.
    assert curve[400] < -10000
    mask = np.asarray(Image.open(tmp_path / "laneseg_label_w16/images/visible.png"))
    assert not mask[400].any()
    explicit = copy.deepcopy(lane)
    explicit["points"] = [point[:2] for point in lane["points"] if point[2]]
    explicit["segments"] = [explicit["points"][:2], explicit["points"][2:]]
    segmented, _, _ = _render(rgb, {"lanes": [explicit]}, tmp_path, "images/sections.jpg")
    assert segmented == entry


def test_lost_source_approval_during_build_aborts_publication(experiment_fixture):
    f = experiment_fixture
    _, rows, store = source_fixture(f, held=True)
    changed = False
    def progress(value):
        nonlocal changed
        if value["phase"] == "verifying_source_clearance" and not changed:
            changed = True
            item = store.detail(rows[0]["image_id"])
            store.decide(decision(item, "skip", 999), "Human")
    with pytest.raises(ValueError, match="decisions changed"):
        build(f, progress=progress)
    assert not f.output.exists()
    assert list(f.temp.glob("experiment.preparing-*/FAILED.json"))
