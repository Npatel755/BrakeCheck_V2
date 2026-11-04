from copy import deepcopy
import json
import sqlite3

from fastapi import HTTPException
import pytest

from bcdata.college_group_clearance import FILENAME, SOURCE, prepare, valid_clearances, verify
from bcdata.simulation_review import SimulationCorrection
from tests.test_real_source_review import decision, fixture, raw_json, repin, sha


ORDINARY = "potential_transverse_marking"
OTHER_ORDINARY = "short_native_polyline"
COMPLETE = "native_coverage_incomplete"


def held_cohort(tmp_path, reasons=None):
    root, rows, store = fixture(tmp_path)
    for row in rows:
        row.update(split="hold", intended_split="train",
                   qc_reasons=list(reasons if reasons is not None else [ORDINARY]))
    repin(root, rows)
    return root, rows, store


def acknowledge(store, row, *, resolved=None, lanes=None, approve=True, complete=False):
    current = store.detail(row["image_id"])
    return store.correct(SimulationCorrection(
        image_id=current["image_id"], revision=current["revision"],
        fingerprint=current["fingerprint"],
        lanes=deepcopy(current["geometry"]["lanes"] if lanes is None else lanes),
        resolved_qc_reasons=list(row["qc_reasons"] if resolved is None else resolved),
        complete_annotation=complete, approve=approve), "Human")


def approved_cohort(tmp_path, reasons=None):
    root, rows, store = held_cohort(tmp_path, reasons)
    for row in rows:
        acknowledge(store, row)
    return root, rows, store


def add_targets(root, rows, count=2):
    """Keep the original three audit samples while adding unsampled members."""
    template = rows[-1]
    added = []
    for index in range(len(rows), len(rows) + count):
        row = deepcopy(template)
        image_id = f"college-{index:016x}"
        row["image_id"] = image_id
        for key in ("image", "geometry", "source_label"):
            row[key] = template[key].replace(template["image_id"], image_id)
            (root / row[key]).write_bytes((root / template[key]).read_bytes())
        geometry = json.loads((root / row["geometry"]).read_bytes())
        geometry["image_id"] = image_id
        (root / row["geometry"]).write_bytes(raw_json(geometry))
        row["geometry_sha256"] = sha((root / row["geometry"]).read_bytes())
        added.append(row)
    rows.extend(added)
    audits = json.loads((root / "audits.json").read_bytes())
    audits["groups"][0]["count"] = len(rows)
    (root / "audits.json").write_bytes(raw_json(audits))
    repin(root, rows)
    return added


def existing_clearances(store):
    _, records, groups, manifest_sha, audits_sha = store.snapshot(fresh=True)
    return valid_clearances(store, records, groups, manifest_sha, audits_sha,
                            store.decisions(), store.corrections())


def write_artifact(root, artifact):
    (root / FILENAME).write_bytes(raw_json(artifact))


def reseal(artifact):
    content = {key: value for key, value in artifact.items() if key != "content_sha256"}
    artifact["content_sha256"] = sha(json.dumps(
        content, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode("utf-8"))


def assert_rejected(store, artifact):
    with pytest.raises((ValueError, HTTPException)):
        verify(store, artifact)
    assert existing_clearances(store) == {}


def test_existing_unchanged_sample_approvals_make_a_sealed_ordinary_warning_proof(tmp_path):
    root, rows, store = approved_cohort(tmp_path, [ORDINARY, OTHER_ORDINARY])
    originals = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    decisions_before = store.decisions()
    artifact = prepare(store)

    assert artifact["source"] == SOURCE
    assert artifact["schema"] == "college_group_warning_clearance:v1"
    assert artifact["group_count"] == 1 and artifact["sample_count"] == 3
    assert artifact["human_decisions_created"] == 0
    assert artifact["full_photo_annotations_propagated"] is False
    assert artifact["auto_promote"] is False
    proof = artifact["groups"][rows[0]["group_key"]]
    assert proof["sample_ids"] == [row["image_id"] for row in rows]
    assert proof["resolved_qc_reasons"] == sorted([ORDINARY, OTHER_ORDINARY])
    assert proof["geometry_unchanged"] is True
    assert proof["scope"] == "same_import_same_group_ordinary_warning_codes_only"
    assert proof["sample_decisions"] == [decisions_before[row["image_id"]] for row in rows]
    assert all(sample["geometry_unchanged"] for sample in proof["samples"].values())
    assert verify(store, artifact) == artifact["groups"]
    assert existing_clearances(store) == {}
    assert store.decisions() == decisions_before
    assert all(path.read_bytes() == raw for path, raw in originals.items())

    write_artifact(root, artifact)
    assert existing_clearances(store) == artifact["groups"]
    assert store.decisions() == decisions_before
    assert all(path.read_bytes() == raw for path, raw in originals.items())


def test_only_the_intersection_of_actual_sample_confirmations_can_propagate(tmp_path):
    root, rows, store = held_cohort(tmp_path)
    rows[0]["qc_reasons"].append(OTHER_ORDINARY)
    rows[1]["qc_reasons"].append("ego_slot_center_ambiguous")
    repin(root, rows)
    for row in rows:
        acknowledge(store, row)
    proof = prepare(store)["groups"][rows[0]["group_key"]]
    assert proof["resolved_qc_reasons"] == [ORDINARY]
    assert proof["samples"][rows[0]["image_id"]]["resolved_qc_reasons"] == sorted(
        [ORDINARY, OTHER_ORDINARY])


def test_disjoint_confirmed_warnings_do_not_create_a_cohort_clearance(tmp_path):
    root, rows, store = held_cohort(tmp_path)
    rows[0]["qc_reasons"] = [OTHER_ORDINARY]
    repin(root, rows)
    for row in rows:
        acknowledge(store, row)
    artifact = prepare(store)
    assert artifact["groups"] == {} and artifact["sample_count"] == 0
    assert rows[0]["group_key"] in artifact["excluded_groups"]


@pytest.mark.parametrize("missing", ["approval", "extra_warning_confirmation"])
def test_every_original_sample_requires_a_current_approval_and_no_unconfirmed_warning(tmp_path, missing):
    root, rows, store = held_cohort(tmp_path)
    if missing == "extra_warning_confirmation":
        rows[-1]["qc_reasons"].append(OTHER_ORDINARY)
        repin(root, rows)
    for row in rows[:-1]:
        acknowledge(store, row)
    final = acknowledge(store, rows[-1], resolved=[ORDINARY], approve=missing != "approval")
    assert final["decision"] is None
    assert bool(final["qc_reasons"]) == (missing == "extra_warning_confirmation")
    artifact = prepare(store)
    assert artifact["groups"] == {} and artifact["group_count"] == 0


@pytest.mark.parametrize("change", ["point", "segments"])
def test_approved_samples_with_changed_physical_geometry_cannot_clear_other_photos(tmp_path, change):
    root, rows, store = held_cohort(tmp_path)
    if change == "segments":
        path = root / rows[0]["geometry"]
        geometry = json.loads(path.read_bytes())
        geometry["lanes"] = [{
            "slot": 2, "points": [[20, 20, True], [18, 30, True], [12, 60, True], [10, 70, True]],
            "segments": [[[20, 20, True], [18, 30, True]], [[12, 60, True], [10, 70, True]]],
            "physical": True}]
        path.write_bytes(raw_json(geometry))
        rows[0]["geometry_sha256"] = sha(path.read_bytes())
        repin(root, rows)
        changed = [{"slot": 2, "points": [[20, 20], [18, 30], [12, 60], [10, 70]],
                    "segments": [[[20, 20], [18, 30], [12, 60], [10, 70]]]}]
    else:
        changed = [{"slot": 2, "points": [[21, 20], [10, 70]]}]
    edited = acknowledge(store, rows[0], lanes=changed)
    assert edited["decision"]["action"] == "approve" and not edited["qc_reasons"]
    for row in rows[1:]:
        acknowledge(store, row)
    assert prepare(store)["groups"] == {}


@pytest.mark.parametrize("reasons", [[COMPLETE], [ORDINARY, COMPLETE]])
def test_whole_photo_annotation_claims_never_propagate_even_with_unchanged_points(tmp_path, reasons):
    _, rows, store = held_cohort(tmp_path, reasons)
    for row in rows:
        item = acknowledge(store, row, complete=True)
        assert item["decision"]["action"] == "approve"
        assert item["geometry"]["annotation_scope"] == "single_photo_manual_replacement"
        assert item["source_geometry"]["lanes"] == item["geometry"]["lanes"]
    assert prepare(store)["groups"] == {}


@pytest.mark.parametrize("action", ["skip", "exclude"])
def test_a_later_sample_decision_invalidates_the_existing_seal(tmp_path, action):
    root, rows, store = approved_cohort(tmp_path)
    artifact = prepare(store)
    write_artifact(root, artifact)
    current = store.detail(rows[0]["image_id"])
    store.decide(decision(current, action, 100), "Human")
    assert_rejected(store, artifact)
    assert prepare(store)["groups"] == {}


def test_a_later_unapproved_correction_makes_the_original_approval_stale(tmp_path):
    root, rows, store = approved_cohort(tmp_path)
    artifact = prepare(store)
    write_artifact(root, artifact)
    edited = acknowledge(store, rows[0], approve=False,
                         lanes=[{"slot": 2, "points": [[22, 20], [10, 70]]}])
    assert edited["decision"] is None and edited["previous_decision_stale"]
    assert_rejected(store, artifact)
    assert prepare(store)["groups"] == {}


@pytest.mark.parametrize("event_change", ["delete", "reviewer", "previous_revision"])
def test_a_matching_immutable_decision_event_is_required(tmp_path, event_change):
    root, rows, store = approved_cohort(tmp_path)
    artifact = prepare(store)
    write_artifact(root, artifact)
    event_id = store.decisions()[rows[0]["image_id"]]["event_id"]
    with sqlite3.connect(root / "review.sqlite") as con:
        if event_change == "delete":
            con.execute("DELETE FROM decision_events WHERE event_id=?", (event_id,))
        elif event_change == "reviewer":
            con.execute("UPDATE decision_events SET reviewer='Other reviewer' WHERE event_id=?", (event_id,))
        else:
            con.execute("UPDATE decision_events SET previous_revision=99 WHERE event_id=?", (event_id,))
    assert_rejected(store, artifact)
    assert prepare(store)["groups"] == {}


def test_non_college_source_rejects_clearance_even_with_existing_sample_approvals(tmp_path):
    root, rows, store = fixture(tmp_path, "pandaset_plus")
    for index, row in enumerate(rows):
        store.decide(decision(store.detail(row["image_id"]), "approve", index), "Human")
    with pytest.raises(ValueError, match="restricted to College Station"):
        prepare(store)
    write_artifact(root, {"schema": "college_group_warning_clearance:v1", "source": SOURCE})
    with pytest.raises(ValueError):
        verify(store, json.loads((root / FILENAME).read_bytes()))
    assert existing_clearances(store) == {}


@pytest.mark.parametrize("resealed", [False, True])
@pytest.mark.parametrize("tamper", ["warning", "decision", "code", "count"])
def test_tampered_proof_is_rejected_even_if_its_plain_content_hash_is_recomputed(tmp_path, tamper, resealed):
    root, rows, store = approved_cohort(tmp_path)
    artifact = prepare(store)
    proof = artifact["groups"][rows[0]["group_key"]]
    if tamper == "warning":
        proof["resolved_qc_reasons"].append(OTHER_ORDINARY)
    elif tamper == "decision":
        proof["samples"][rows[0]["image_id"]]["decision"]["reviewer"] = "Invented reviewer"
    elif tamper == "code":
        artifact["code_sha256"]["college_group_clearance.py"] = "0" * 64
    else:
        artifact["sample_count"] += 1
    if resealed:
        reseal(artifact)
    write_artifact(root, artifact)
    assert_rejected(store, artifact)


@pytest.mark.parametrize("file_key", ["image", "geometry", "source_label", "correction"])
def test_changed_sample_source_or_saved_correction_bytes_fail_closed(tmp_path, file_key):
    root, rows, store = approved_cohort(tmp_path)
    artifact = prepare(store)
    write_artifact(root, artifact)
    relative = (store.corrections()[rows[0]["image_id"]]["geometry_path"]
                if file_key == "correction" else rows[0][file_key])
    path = root / relative
    path.write_bytes(path.read_bytes() + b"\n")
    assert_rejected(store, artifact)
    assert prepare(store)["groups"] == {}


def test_a_re_pinned_source_snapshot_cannot_reuse_previous_sample_confirmations(tmp_path):
    root, rows, store = approved_cohort(tmp_path)
    artifact = prepare(store)
    write_artifact(root, artifact)
    rows[0]["session_id"] = "different-source-provenance"
    repin(root, rows)
    assert_rejected(store, artifact)
    assert prepare(store)["groups"] == {}


@pytest.mark.parametrize("raw", [b"not json", b"null", b"[]", b"{}"])
def test_malformed_optional_clearance_artifact_has_no_effect(tmp_path, raw):
    root, _, store = approved_cohort(tmp_path)
    (root / FILENAME).write_bytes(raw)
    assert existing_clearances(store) == {}


def test_clearance_releases_only_unsampled_members_and_does_not_double_count_sample_approvals(tmp_path, monkeypatch):
    root, rows, store = held_cohort(tmp_path)
    targets = add_targets(root, rows)
    for row in rows[:3]:
        acknowledge(store, row)
    before = store.status()["counts"]
    assert before["individually_cleared_held"] == 3
    assert before["group_cleared_held"] == 0 and before["held_uncleared"] == 2
    artifact = prepare(store)
    original_files = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    write_artifact(root, artifact)
    decisions_before = store.decisions()
    eligible = store.eligible_records()
    assert eligible["train_images"] == 5
    exported = {row["image_id"]: row for row in eligible["records"]}
    for sample in rows[:3]:
        assert exported[sample["image_id"]]["clearance"]["type"] == "individual_human_approval"
    for target in targets:
        item = exported[target["image_id"]]
        assert item["split"] == "train" and item["original_split"] == "hold"
        assert item["geometry"] == target["geometry"]
        assert item["qc_reasons"] == [] and item["source_qc_reasons"] == [ORDINARY]
        assert item["resolved_qc_reasons"] == [ORDINARY]
        assert item["clearance"]["type"] == "sampled_unchanged_warning_cohort"
        assert item["clearance"]["geometry_unchanged"] is True
        assert item["clearance"]["human_decisions_created"] == 0
        assert item["clearance"]["sample_decisions"] == artifact["groups"][target["group_key"]]["sample_decisions"]
        assert len(item["clearance"]["proof_sha256"]) == 64
    assert store.decisions() == decisions_before
    assert all(path.read_bytes() == raw for path, raw in original_files.items())

    def no_full_export():
        pytest.fail("Status must not run the full export API")

    monkeypatch.setattr(store, "eligible_records", no_full_export)
    counts = store.status()["counts"]
    assert counts["eligible_train_candidates"] == 5
    assert counts["individually_cleared_held"] == 3 and counts["group_cleared_held"] == 2
    assert counts["eligible_group_train_candidates"] == counts["held_uncleared"] == 0
    assert counts["approved"] == 3


@pytest.mark.parametrize("target_issue", [
    "extra_ordinary_warning", "full_photo_warning", "unknown_warning",
    "intended_none", "intended_val", "intended_test", "different_group",
    "empty_geometry", "nonphysical_geometry", "integer_physical", "road_edge", "curb", "pavement_edge",
])
def test_group_clearance_cannot_release_a_target_outside_its_exact_warning_and_paint_scope(tmp_path, target_issue):
    root, rows, store = held_cohort(tmp_path)
    target = add_targets(root, rows, count=1)[0]
    if target_issue == "extra_ordinary_warning":
        target["qc_reasons"].append(OTHER_ORDINARY)
    elif target_issue == "full_photo_warning":
        target["qc_reasons"].append(COMPLETE)
    elif target_issue == "unknown_warning":
        target["qc_reasons"].append("unknown_warning")
    elif target_issue.startswith("intended_"):
        intended = target_issue.removeprefix("intended_")
        target["intended_split"] = None if intended == "none" else intended
    elif target_issue == "different_group":
        target["group_key"] = "hold/another-cohort"
        audits = json.loads((root / "audits.json").read_bytes())
        audits["groups"][0]["count"] -= 1
        audits["groups"].append({"group_key": target["group_key"], "split": "hold", "count": 1,
                                 "sample_ids": [target["image_id"]]})
        (root / "audits.json").write_bytes(raw_json(audits))
    else:
        path = root / target["geometry"]
        geometry = json.loads(path.read_bytes())
        if target_issue == "empty_geometry":
            geometry["lanes"] = []
        elif target_issue == "nonphysical_geometry":
            geometry["lanes"][0]["physical"] = False
        elif target_issue == "integer_physical":
            geometry["lanes"][0]["physical"] = 1
        else:
            geometry["lanes"][0]["kind"] = target_issue
        path.write_bytes(raw_json(geometry))
        target["geometry_sha256"] = sha(path.read_bytes())
    repin(root, rows)
    for row in rows[:3]:
        acknowledge(store, row)
    artifact = prepare(store)
    assert rows[0]["group_key"] in artifact["groups"]
    write_artifact(root, artifact)
    counts = store.status()["counts"]
    assert counts["individually_cleared_held"] == 3
    assert counts["group_cleared_held"] == 0 and counts["held_uncleared"] == 1
    exported = store.eligible_records()
    assert exported["train_images"] == 3
    assert target["image_id"] not in {row["image_id"] for row in exported["records"]}


@pytest.mark.parametrize("history", ["decision", "stale_correction"])
def test_target_personal_review_history_takes_precedence_over_group_clearance(tmp_path, history):
    root, rows, store = held_cohort(tmp_path)
    target = add_targets(root, rows, count=1)[0]
    for row in rows[:3]:
        acknowledge(store, row)
    write_artifact(root, prepare(store))
    with sqlite3.connect(root / "review.sqlite") as con:
        if history == "decision":
            # Unsampled photos have no review endpoint. Seed a prior record to
            # prove that cohort clearance never overrides personal history.
            con.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?)",
                        (target["image_id"], "skip", "Human", "prior-target-event",
                         "0" * 64, 1, 1.0))
        else:
            correction = store.corrections()[rows[0]["image_id"]]
            con.execute("INSERT INTO corrections VALUES(?,?,?,?,?,?)",
                        (target["image_id"], "0" * 64, correction["geometry_path"],
                         correction["geometry_sha256"], "Human", 1.0))
    assert existing_clearances(store)
    assert store.status()["counts"]["group_cleared_held"] == 0
    assert store.eligible_records()["train_images"] == 3


def test_target_geometry_tampering_is_not_counted_or_exported_as_cleared(tmp_path):
    root, rows, store = held_cohort(tmp_path)
    target = add_targets(root, rows, count=1)[0]
    for row in rows[:3]:
        acknowledge(store, row)
    write_artifact(root, prepare(store))
    assert store.status()["counts"]["group_cleared_held"] == 1
    path = root / target["geometry"]
    path.write_bytes(path.read_bytes() + b"\n")
    assert existing_clearances(store)
    assert store.status()["counts"]["group_cleared_held"] == 0
    assert store.eligible_records()["train_images"] == 3


@pytest.mark.parametrize("lane", [
    {"slot": 2, "points": [[20, 10], [10, 20]]},
    {"slot": 2, "points": [[20, 31], [10, 32]]},
    {"slot": 2, "points": [[20, 20], [15, 45, False], [10, 70]]},
    {"slot": 2, "points": [[20, 20], [10, 70]], "segments": [[[20, 20]], [[10, 70]]]},
    {"slot": 2, "points": [[20, 20], [15, 60], [10, 40]]},
    {"slot": 2, "points": [[20, 20], [15, 40], [13, 30], [10, 70]],
     "segments": [[[20, 20], [15, 40]], [[13, 30], [10, 70]]]},
], ids=["above-output-rows", "only-one-output-row", "hidden-point-break", "disconnected-singletons",
        "folded-section", "overlapping-sections"])
def test_group_clearance_keeps_targets_without_a_trainable_observed_span_held(tmp_path, lane):
    root, rows, store = held_cohort(tmp_path)
    target = add_targets(root, rows, count=1)[0]
    path = root / target["geometry"]
    geometry = json.loads(path.read_bytes())
    geometry["lanes"] = [lane]
    path.write_bytes(raw_json(geometry))
    target["geometry_sha256"] = sha(path.read_bytes())
    repin(root, rows)
    for row in rows[:3]:
        acknowledge(store, row)
    write_artifact(root, prepare(store))

    assert existing_clearances(store)
    counts = store.status()["counts"]
    assert counts["individually_cleared_held"] == 3 and counts["group_cleared_held"] == 0
    assert counts["held_uncleared"] == 1
    eligible = store.eligible_records()
    assert eligible["train_images"] == 3
    assert target["image_id"] not in {row["image_id"] for row in eligible["records"]}


@pytest.mark.parametrize("changed_metadata", ["visibility", "integer_visibility", "physical", "integer_physical", "kind"])
def test_physical_metadata_changes_cannot_be_mistaken_for_unchanged_sample_geometry(tmp_path, changed_metadata):
    root, rows, store = held_cohort(tmp_path)
    path = root / rows[0]["geometry"]
    geometry = json.loads(path.read_bytes())
    geometry["lanes"][0].update(points=[[20, 20, True], [10, 70, True]],
                                 physical=True, kind="paint")
    path.write_bytes(raw_json(geometry))
    rows[0]["geometry_sha256"] = sha(path.read_bytes())
    repin(root, rows)
    for row in rows:
        acknowledge(store, row, lanes=[{"slot": 2, "points": [[20, 20], [10, 70]]}])
    assert prepare(store)["groups"]

    # Construct a self-consistent current saved correction and approval, with
    # identical XY coordinates but changed physical semantics. This isolates
    # semantic equality from the independent checks for stale file hashes.
    correction = store.corrections()[rows[0]["image_id"]]
    path = root / correction["geometry_path"]
    changed = json.loads(path.read_bytes())
    lane = changed["lanes"][0]
    if changed_metadata in {"visibility", "integer_visibility"}:
        lane["points"][0][2] = False if changed_metadata == "visibility" else 1
    elif changed_metadata in {"physical", "integer_physical"}:
        lane["physical"] = False if changed_metadata == "physical" else 1
    else:
        lane["kind"] = "road_edge"
    raw = raw_json(changed)
    path.write_bytes(raw)
    digest = sha(raw)
    fingerprint = sha((correction["source_fingerprint"] + digest).encode())
    event_id = store.decisions()[rows[0]["image_id"]]["event_id"]
    with sqlite3.connect(root / "review.sqlite") as con:
        con.execute("UPDATE corrections SET geometry_sha256=? WHERE image_id=?", (digest, rows[0]["image_id"]))
        con.execute("UPDATE decisions SET fingerprint=? WHERE image_id=?", (fingerprint, rows[0]["image_id"]))
        con.execute("UPDATE decision_events SET fingerprint=? WHERE event_id=?", (fingerprint, event_id))
    assert store.detail(rows[0]["image_id"])["decision"]["action"] == "approve"
    assert prepare(store)["groups"] == {}
