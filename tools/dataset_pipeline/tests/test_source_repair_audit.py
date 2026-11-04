"""Repair generations use disposable fixtures and never invent human approval."""
import json
from pathlib import Path
import time
import uuid

import numpy as np
import pytest
from PIL import Image

from bcdata.review_export import export_reviewed
from bcdata.review_store import ReviewError, ReviewStore
from bcdata.source_repair_audit import _samples, requeue_repairs
from bcdata.source_validation import refresh_groups, run, validated_record
from bcdata.teachers.base import file_sha256
from tests.test_review import build_review_fixture


def blocked_fixture(tmp_path, rejection=False):
    settings = build_review_fixture(tmp_path)
    store = ReviewStore(settings)
    with store.registry() as reg:
        original = dict(reg.image("sample-0"))
        template = json.loads(settings.path("lane_geometry", "sample-0.json").read_text())
        for index in range(7):
            image_id = f"sample-{index}"
            if index >= 3:
                row = {key: value for key, value in original.items() if key not in ("created_at", "updated_at")}
                row.update(image_id=image_id, session_id=f"session-{index}", source_ref=f"fixture:{index}", sha256=str(index))
                reg.add_image(**row)
                geo = {**template, "image_id": image_id}
                path = settings.path("lane_geometry", image_id + ".json")
                path.write_text(json.dumps(geo))
                reg.add_annotation(image_id, "geometry", "geometry", "fixture-auto", str(path), license_ids=["comma10k"])
                reg.set_qc(image_id, .8, "medium", .2, [], False, "fixture-auto")
            reg.update_image(image_id, split="train", tags={"lighting": {"value": "day"}, "review_batch": {"value": "source-fixture"}})
            source = settings.root / (image_id + "-source.png")
            Image.fromarray(np.ones((874, 1164), np.uint8)).save(source)
            reg.add_annotation(image_id, "source_label", "comma10k", "source:fixture", str(source), params={"human": True})
            teacher = settings.path("lane_geometry", "teachers", "comma_segnet_fresh", image_id + ".json")
            teacher.parent.mkdir(parents=True, exist_ok=True)
            teacher.write_text(settings.path("lane_geometry", image_id + ".json").read_text())
    initial = run(settings, sample_size=2, apply=True, minimum_per_group=2)
    old_samples = initial["sample_ids"]
    action = "rejected" if rejection else "corrected"
    store.decide(old_samples[0], action, "Nick", 0, str(uuid.uuid4()), lanes=None if rejection else [],
                 notes="Parking garage; unsuitable image" if rejection else "Boundary correction")
    store.decide(old_samples[1], "approved", "Nick", 0, str(uuid.uuid4()))
    refresh_groups(settings)
    with store.registry() as reg:
        checks = [dict(row) for row in reg.conn.execute("SELECT * FROM source_conversion_checks WHERE state='awaiting_audit'")]
    repairs = []
    for check in checks:
        geo = json.loads(Path(check["geometry_path"]).read_text())
        geo["version"] = "fixture-repair-v1"
        path = settings.path("lane_geometry", "repairs", check["image_id"] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(geo))
        repairs.append({"image_id": check["image_id"], "previous_check": check, "geometry_path": str(path),
                        "geometry_version": geo["version"], "diagnostics": {"risk": "unchanged"},
                        "qc": {"score": .8, "tier": "source_label", "priority": .2, "issues": [], "qc_sample": False}})
    return settings, store, old_samples, repairs


def snapshot(store):
    with store.registry() as reg:
        return {
            "images": [dict(row) for row in reg.images()],
            "events": [dict(row) for row in reg.conn.execute("SELECT * FROM review_events ORDER BY event_id")],
            "groups": [dict(row) for row in reg.conn.execute("SELECT * FROM source_validation_groups ORDER BY group_key")],
            "checks": [dict(row) for row in reg.conn.execute("SELECT * FROM source_conversion_checks ORDER BY image_id")],
            "annotations": [dict(row) for row in reg.conn.execute("SELECT * FROM annotations ORDER BY id")],
        }


def test_repair_dry_run_is_deterministic_and_read_only(tmp_path):
    settings, store, _, repairs = blocked_fixture(tmp_path)
    before = snapshot(store)
    first = requeue_repairs(settings, repairs, sample_target=2, minimum_per_group=2)
    second = requeue_repairs(settings, list(reversed(repairs)), sample_target=2, minimum_per_group=2)
    assert first["counts"] == {"sample": 2, "awaiting_audit": 3}
    assert first["sample_ids"] == second["sample_ids"]
    assert first["batch_id"] == second["batch_id"]
    assert snapshot(store) == before


def test_repair_requires_fresh_audit_preserves_history_and_undo_revokes(tmp_path):
    settings, store, old_samples, repairs = blocked_fixture(tmp_path)
    before = snapshot(store)
    original_bytes = {row["path"]: Path(row["path"]).read_bytes() for row in before["annotations"]}
    report = requeue_repairs(settings, repairs, sample_target=2, minimum_per_group=2, apply=True)
    after = snapshot(store)
    assert after["images"] == before["images"] and after["events"] == before["events"]
    assert {row["group_key"]: row for row in after["groups"] if row["group_key"] in {g["group_key"] for g in before["groups"]}} == {g["group_key"]: g for g in before["groups"]}
    assert all(Path(path).read_bytes() == data for path, data in original_bytes.items())
    assert not set(report["sample_ids"]) & set(old_samples)
    assert store.stats()["source_validated"] == 0
    assert export_reviewed(settings)["exported"] == 2
    with store.registry() as reg:
        archive = [dict(row) for row in reg.conn.execute("SELECT * FROM source_conversion_repair_history")]
        assert len(archive) == len(repairs) == 5
        assert {json.loads(row["previous_check"])["image_id"] for row in archive} == {repair["image_id"] for repair in repairs}
        assert all(reg.annotation(repair["image_id"], "geometry", "geometry")["model_version"] == "fixture-repair-v1" for repair in repairs)
    events = []
    for image_id in report["sample_ids"]:
        event = str(uuid.uuid4())
        store.decide(image_id, "approved", "Nick", store.detail(image_id)["revision"], event)
        events.append(event)
    refresh_groups(settings)
    assert store.stats()["source_validated"] == 3
    assert export_reviewed(settings)["source_validated"] == 3
    store.undo(events[0], "Nick")
    refresh_groups(settings)
    assert store.stats()["source_validated"] == 0
    assert export_reviewed(settings)["source_validated"] == 0


def test_repair_skips_active_reviewer_and_new_human_decision(tmp_path):
    settings, store, _, repairs = blocked_fixture(tmp_path)
    busy, reviewed = repairs[0]["image_id"], repairs[1]["image_id"]
    with store.registry() as reg:
        with reg.tx() as con:
            con.execute("INSERT INTO review_claims VALUES (?,?,?)", (busy, "phone-owner", time.time() + 600))
    store.decide(reviewed, "approved", "Nick", 0, str(uuid.uuid4()))
    before = snapshot(store)
    report = requeue_repairs(settings, repairs, sample_target=2, minimum_per_group=2, apply=True)
    assert {item["image_id"]: item["reason"] for item in report["skipped"]} == {busy: "active_reviewer", reviewed: "human_reviewed"}
    assert report["eligible"] == 3 and len(report["applied_ids"]) == 3
    after = snapshot(store)
    assert after["events"] == before["events"]
    for image_id in (busy, reviewed):
        assert next(row for row in after["checks"] if row["image_id"] == image_id) == next(row for row in before["checks"] if row["image_id"] == image_id)


def test_suitability_rejection_is_preserved_without_releasing_other_conversions(tmp_path):
    settings, store, old_samples, repairs = blocked_fixture(tmp_path, rejection=True)
    rejected_before = store.detail(old_samples[0])
    report = requeue_repairs(settings, repairs, sample_target=2, minimum_per_group=2, apply=True)
    assert store.detail(old_samples[0])["review_status"] == rejected_before["review_status"] == "rejected"
    assert report["counts"] == {"sample": 2, "awaiting_audit": 3}
    refresh_groups(settings)
    assert store.stats()["source_validated"] == 0
    assert all(group["state"] != "passed" for group in store.stats()["source_audit_groups"])


def test_repair_flags_fresh_conversion_defects_and_is_idempotent(tmp_path):
    settings, store, _, repairs = blocked_fixture(tmp_path)
    repairs[0]["qc"]["issues"] = [{"code": "crossing", "message": "Fresh geometry crossing"}]
    report = requeue_repairs(settings, repairs, sample_target=2, minimum_per_group=2, apply=True)
    assert report["counts"] == {"flagged": 1, "sample": 2, "awaiting_audit": 2}
    assert repairs[0]["image_id"] not in report["sample_ids"]
    before = snapshot(store)
    repeated = requeue_repairs(settings, repairs, sample_target=2, minimum_per_group=2, apply=True)
    assert repeated["eligible"] == 0 and repeated["applied_ids"] == []
    assert {item["reason"] for item in repeated["skipped"]} == {"check_changed"}
    assert snapshot(store) == before


def test_repair_source_fingerprint_drift_is_held_and_not_overwritten(tmp_path):
    settings, store, _, repairs = blocked_fixture(tmp_path)
    source = Path(repairs[0]["previous_check"]["source_path"])
    source.write_bytes(b"source changed")
    report = requeue_repairs(settings, repairs, sample_target=2, minimum_per_group=2, apply=True)
    assert report["skipped"] == [{"image_id": repairs[0]["image_id"], "reason": "source_bytes_changed"}]
    with store.registry() as reg:
        assert validated_record(reg, repairs[0]["image_id"], verify_files=True) is None
        assert dict(reg.conn.execute("SELECT * FROM source_conversion_checks WHERE image_id=?", (repairs[0]["image_id"],)).fetchone()) == repairs[0]["previous_check"]


def test_repair_risk_groups_do_not_reuse_broad_original_strata(tmp_path):
    settings, store, _, repairs = blocked_fixture(tmp_path)
    repairs[0]["diagnostics"]["risk"] = "lane_added"
    report = requeue_repairs(settings, repairs, sample_target=2, minimum_per_group=2, apply=True)
    assert report["groups"] == 2
    assert len(report["sample_ids"]) == 3  # Minimum two per cohort, one for singleton.
    with store.registry() as reg:
        groups = [dict(row) for row in reg.conn.execute("SELECT * FROM source_validation_groups WHERE batch_id=?", (report["batch_id"],))]
    assert {json.loads(group["evidence"])["risk"] for group in groups} == {"unchanged", "lane_added"}
    assert all(json.loads(group["evidence"])["requires_fresh_human_audit"] for group in groups)


def test_stats_hide_superseded_blocked_groups_but_keep_active_held_warnings(tmp_path):
    settings, store, _, repairs = blocked_fixture(tmp_path)
    assert [group["state"] for group in store.stats()["source_audit_groups"]] == ["blocked"]
    first = requeue_repairs(settings, repairs[:2], sample_target=2, minimum_per_group=2, apply=True)
    groups = store.stats()["source_audit_groups"]
    assert {group["state"] for group in groups} == {"blocked", "awaiting_audit"}
    assert sum(group["unreviewed"] for group in groups) == 5
    requeue_repairs(settings, repairs[2:], sample_target=2, minimum_per_group=2, apply=True)
    groups = store.stats()["source_audit_groups"]
    assert len(groups) == 2 and all(group["state"] == "awaiting_audit" for group in groups)
    assert all(group["batch_id"].startswith("comma-source-repair-audit") for group in groups)
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM source_validation_groups WHERE state='blocked'") == 1
    detail = store.detail(first["sample_ids"][0])
    assert detail["source_conversion"]["repair_policy"] == "comma-source-repair-audit-v1"
    assert detail["source_conversion"]["requires_fresh_human_audit"] is True


def test_stats_do_not_present_fully_reviewed_group_as_active_warning(tmp_path):
    settings, store, _, repairs = blocked_fixture(tmp_path)
    requeue_repairs(settings, repairs, sample_target=2, minimum_per_group=2, apply=True)
    for repair in repairs:
        image_id = repair["image_id"]
        store.decide(image_id, "approved", "Nick", store.detail(image_id)["revision"], str(uuid.uuid4()))
    assert store.stats()["source_audit_groups"] == []
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM source_validation_groups") == 2


def test_flagged_cases_do_not_keep_a_superseded_group_hold_warning_alive(tmp_path):
    settings, store, _, repairs = blocked_fixture(tmp_path)
    with store.registry() as reg:
        reg.conn.execute("UPDATE source_conversion_checks SET state='flagged' WHERE image_id=?",(repairs[0]['image_id'],))
        reg.conn.commit()
    requeue_repairs(settings, repairs[1:], sample_target=2, minimum_per_group=2, apply=True)
    groups = store.stats()['source_audit_groups']
    assert groups and all(group['state'] != 'blocked' for group in groups)
    assert store.detail(repairs[0]['image_id'])['source_conversion']['state'] == 'flagged'
    assert store.next_image('owner', status='flagged')['image_id'] == repairs[0]['image_id']


def test_future_repair_group_needs_all_three_sample_approvals(tmp_path):
    settings, store, _, repairs = blocked_fixture(tmp_path)
    report = requeue_repairs(settings, repairs, sample_target=2, apply=True)
    assert len(report['sample_ids']) == 3
    for image_id in report['sample_ids'][:2]:
        store.decide(image_id,'approved','Nick',store.detail(image_id)['revision'],str(uuid.uuid4()))
    refresh_groups(settings)
    assert store.stats()['source_validated'] == 0
    image_id = report['sample_ids'][2]
    store.decide(image_id,'approved','Nick',store.detail(image_id)['revision'],str(uuid.uuid4()))
    refresh_groups(settings)
    assert store.stats()['source_validated'] == 2


def test_source_fragment_candidate_requires_explicit_adoption_and_preserves_source(tmp_path):
    settings, store, _, repairs = blocked_fixture(tmp_path)
    image_id = repairs[0]["image_id"]
    original = store.detail(image_id)
    source_path = Path(repairs[0]["previous_check"]["source_path"])
    source_bytes = source_path.read_bytes()
    candidate = {**original["geometry"], "teacher": "source_refined", "teacher_version": "source_refined:fixture-v2", "source_guided": True,
                 "model_meta": {"source_sha256": "a" * 64, "geometry_sha256": "b" * 64, "source_geometry_version": "fixture-auto"}}
    path = settings.path("lane_geometry", "teachers", "source_refined", image_id + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidate))
    detail = store.detail(image_id)
    option = next(option for option in detail["teacher_options"] if option["name"] == "source_refined")
    assert option["label"] == "Source paint · recovered fragments"
    assert option["capabilities"] == {"painted_lanes": True, "road_edges": False}
    assert detail["origin"] == original["origin"] == "source_derived_proposal"
    with store.registry() as reg:
        assert reg.annotation(image_id, "human") is None
    with pytest.raises(ReviewError, match="changed"):
        store.decide(image_id, "corrected", "Nick", 0, str(uuid.uuid4()), lanes=candidate["lanes"],
                     candidate="source_refined", candidate_version="outdated")
    store.decide(image_id, "corrected", "Nick", 0, str(uuid.uuid4()), lanes=candidate["lanes"],
                 candidate="source_refined", candidate_version=candidate["teacher_version"])
    saved = store.detail(image_id)
    assert saved["origin"] == "human" and saved["review_status"] == "corrected"
    assert saved["geometry"]["candidate_provenance"]["source_sha256"] == "a" * 64
    assert saved["geometry"]["candidate_provenance"]["geometry_sha256"] == "b" * 64
    assert saved["geometry"].get("model_licenses", []) == []
    assert source_path.read_bytes() == source_bytes
    with store.registry() as reg:
        human = reg.annotation(image_id, "human")
        assert json.loads(human["license_ids"]) == ["comma10k"]


def test_sealed_plan_rejects_changed_draft_bytes_before_any_mutation(tmp_path):
    settings, store, _, repairs = blocked_fixture(tmp_path)
    for repair in repairs:
        repair["geometry_sha256"] = file_sha256(repair["geometry_path"])
    before = snapshot(store)
    draft = Path(repairs[0]["geometry_path"])
    geo = json.loads(draft.read_text())
    geo["lanes"][0]["points"][0][0] += 1
    draft.write_text(json.dumps(geo))
    with pytest.raises(ValueError, match="Repair draft fingerprint changed"):
        requeue_repairs(settings, repairs, sample_target=2, minimum_per_group=2, apply=True)
    assert snapshot(store) == before
    # The new fingerprint must be deliberately sealed by plan preparation.
    repairs[0]["geometry_sha256"] = file_sha256(draft)
    report = requeue_repairs(settings, repairs, sample_target=2, minimum_per_group=2, apply=True)
    assert len(report["applied_ids"]) == 5


def test_repair_causes_receive_distinct_fresh_audits(tmp_path):
    settings, store, previous_samples, repairs = blocked_fixture(tmp_path)
    families = ["duplicate_double_merge", "fragmented_ego", "simple_supported", "complex_branching", "simple_supported"]
    for repair, family in zip(repairs, families):
        repair["diagnostics"]["risk_family"] = family
    report = requeue_repairs(settings, repairs, sample_target=1, apply=True)
    # A small total target cannot collapse independent causes into one audit or
    # reuse the approvals that preceded this repair generation.
    assert report["groups"] == 4 and len(report["sample_ids"]) == 5
    assert set(previous_samples).isdisjoint(report["sample_ids"])
    assert store.stats()["source_validated"] == 0
    with store.registry() as reg:
        groups = list(reg.conn.execute("SELECT * FROM source_validation_groups WHERE batch_id=?", (report["batch_id"],)))
    assert {json.loads(group["evidence"])["risk_family"] for group in groups} == set(families)
    assert all(group["state"] == "awaiting_audit" for group in groups)
    assert all(len(json.loads(group["sample_ids"])) in (1, 2) for group in groups)


def test_same_repair_cause_is_separated_by_lighting_and_lane_count(tmp_path):
    settings, store, _, repairs = blocked_fixture(tmp_path)
    for repair in repairs:
        repair["diagnostics"]["risk_family"] = "simple_supported"
    with store.registry() as reg:
        row = reg.image(repairs[0]["image_id"])
        tags = json.loads(row["tags"])
        tags["lighting"] = {"value": "night"}
        reg.update_image(row["image_id"], tags=tags)
    report = requeue_repairs(settings, repairs, sample_target=1, apply=True)
    assert report["groups"] == 2 and len(report["sample_ids"]) == 4
    with store.registry() as reg:
        strata = [row[0] for row in reg.conn.execute("SELECT stratum FROM source_validation_groups WHERE batch_id=?", (report["batch_id"],))]
    assert any(":night:lanes-" in value for value in strata)
    assert any(":day:lanes-" in value for value in strata)


@pytest.mark.parametrize("family", ["", "unsafe:group", "A", 7, "a" * 65])
def test_invalid_repair_cause_fails_before_any_installation(tmp_path, family):
    settings, store, _, repairs = blocked_fixture(tmp_path)
    repairs[0]["diagnostics"]["risk_family"] = family
    before = snapshot(store)
    with pytest.raises(ValueError, match="risk family"):
        requeue_repairs(settings, repairs, sample_target=1, apply=True)
    assert snapshot(store) == before


def developer_evidence(settings):
    import bcdata.source_repair_audit as audit_module
    path = settings.path("reports", "converter-development-regression.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"development_only": True, "requires_fresh_human_audit": True}))
    return {"converter_code_sha256": file_sha256(Path(audit_module.__file__).with_name("source_converter.py")),
            "regression_report_path": str(path), "regression_report_sha256": file_sha256(path)}


def test_development_evidence_is_sealed_and_preserved_without_granting_approval(tmp_path):
    settings, store, _, repairs = blocked_fixture(tmp_path)
    evidence = developer_evidence(settings)
    for repair in repairs:
        repair["diagnostics"].update(risk_family="simple_supported", converter_evidence=evidence)
    before = snapshot(store)
    report = requeue_repairs(settings, repairs, sample_target=1, apply=True)
    after = snapshot(store)
    assert after["images"] == before["images"] and after["events"] == before["events"]
    assert len(report["sample_ids"]) == 3 and store.stats()["source_validated"] == 0
    with store.registry() as reg:
        group = reg.conn.execute("SELECT evidence FROM source_validation_groups WHERE batch_id=?", (report["batch_id"],)).fetchone()
        archive = list(reg.conn.execute("SELECT replacement FROM source_conversion_repair_history WHERE batch_id=?", (report["batch_id"],)))
    assert json.loads(group[0])["converter_evidence"] == [evidence]
    assert all(json.loads(row[0])["diagnostics"]["converter_evidence"] == evidence for row in archive)
    assert export_reviewed(settings)["source_validated"] == 0


@pytest.mark.parametrize("drift", ["code", "report", "missing_fields"])
def test_changed_or_incomplete_development_evidence_aborts_without_mutation(tmp_path, drift):
    settings, store, _, repairs = blocked_fixture(tmp_path)
    evidence = developer_evidence(settings)
    if drift == "code":
        evidence["converter_code_sha256"] = "f" * 64
    elif drift == "report":
        Path(evidence["regression_report_path"]).write_text("changed")
    else:
        del evidence["regression_report_sha256"]
    repairs[0]["diagnostics"]["converter_evidence"] = evidence
    before = snapshot(store)
    with pytest.raises(ValueError, match="[Cc]onverter|converter evidence"):
        requeue_repairs(settings, repairs, sample_target=1, apply=True)
    assert snapshot(store) == before


def test_explicit_converter_module_path_is_bound_in_evidence(tmp_path):
    import bcdata.source_repair_audit as audit_module
    settings, store, _, repairs = blocked_fixture(tmp_path)
    evidence = developer_evidence(settings)
    evidence["converter_code_path"] = str(Path(audit_module.__file__).with_name("source_converter.py").resolve())
    for repair in repairs:
        repair["diagnostics"]["converter_evidence"] = evidence
    report = requeue_repairs(settings, repairs, sample_target=1, apply=True)
    with store.registry() as reg:
        group = reg.conn.execute("SELECT evidence FROM source_validation_groups WHERE batch_id=?", (report["batch_id"],)).fetchone()
    assert json.loads(group[0])["converter_evidence"] == [evidence]
    assert store.stats()["source_validated"] == 0


@pytest.mark.parametrize("location", ["outside", "different_module"])
def test_converter_evidence_rejects_code_outside_allowed_converter_modules(tmp_path, location):
    import bcdata.source_repair_audit as audit_module
    settings, store, _, repairs = blocked_fixture(tmp_path)
    evidence = developer_evidence(settings)
    if location == "outside":
        code = settings.root / "source_converter_v3.py"
        code.write_text("# Outside the implementation package")
    else:
        code = Path(audit_module.__file__)
    evidence.update(converter_code_path=str(code.resolve()), converter_code_sha256=file_sha256(code))
    repairs[0]["diagnostics"]["converter_evidence"] = evidence
    before = snapshot(store)
    with pytest.raises(ValueError, match="bcdata/source_converter"):
        requeue_repairs(settings, repairs, sample_target=1, apply=True)
    assert snapshot(store) == before


def test_large_repair_cohorts_require_only_three_fresh_samples_each():
    groups = {f"cause-{index}": [{"image_id": f"image-{index}-{n}", "original_stratum": f"stratum-{n % 5}"}
                                 for n in range(100)] for index in range(3)}
    selected = _samples(groups, target=64)
    assert {key: len(ids) for key, ids in selected.items()} == {key: 3 for key in groups}
    assert sum(map(len, selected.values())) == 9
    assert len(_samples({"large": groups["cause-0"]}, target=64)["large"]) == 3
    assert len(_samples({"pair": groups["cause-0"][:2]}, target=64)["pair"]) == 2
    assert len(_samples({"large": groups["cause-0"]}, target=64, minimum_per_group=2, maximum_per_group=2)["large"]) == 2


def test_repair_maximum_below_group_minimum_fails_before_any_mutation(tmp_path):
    settings, store, _, repairs = blocked_fixture(tmp_path)
    before = snapshot(store)
    with pytest.raises(ValueError, match="Maximum samples"):
        requeue_repairs(settings, repairs, apply=True, maximum_per_group=2)
    assert snapshot(store) == before


def test_default_repair_target_of_sixty_four_still_creates_only_three_samples(tmp_path):
    settings, store, _, repairs = blocked_fixture(tmp_path)
    report = requeue_repairs(settings, repairs, apply=True)
    assert report["minimum_per_group"] == report["maximum_per_group"] == 3
    assert report["counts"] == {"sample": 3, "awaiting_audit": 2}
    assert len(report["sample_ids"]) == 3
