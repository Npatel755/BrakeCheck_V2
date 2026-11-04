import hashlib
import io
import json

from fastapi import HTTPException
from PIL import Image
import pytest

from bcdata.real_source_review import RealSourceReview
from bcdata.simulation_review import SimulationCorrection, SimulationDecision


def raw_json(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def fixture(tmp_path, source="college_station_lane_marking", warning=False):
    root = tmp_path / "experiments" / source
    root.mkdir(parents=True)
    rows = []
    for index in range(3):
        image_id = ("college-" if source.startswith("college") else "panda-") + f"{index:016x}"
        image = Image.new("RGB", (128, 72), (50 + index, 60, 70))
        stream = io.BytesIO()
        image.save(stream, "JPEG")
        photo = stream.getvalue()
        geometry = raw_json({"image_id": image_id, "width": 128, "height": 72,
                             "lanes": [{"slot": 2, "points": [[20, 20], [10, 70]]}]})
        native = b"20 20 10 70\n"
        paths = {"image": f"images/{image_id}.jpg", "geometry": f"geometry/{image_id}.json",
                 "source_label": f"native/{image_id}.lines.txt"}
        for path, raw in zip(paths.values(), (photo, geometry, native)):
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        rows.append({"image_id": image_id, "source": source, "synthetic": False,
                     "pool": "commercial", "split": "train", "session_id": "drive/1",
                     "group_key": "train/drive-1/two-lanes", "qc_reasons": ["bad_point"] if warning and index == 0 else [],
                     "source_sha256": sha(photo), "geometry_sha256": sha(geometry),
                     "source_label_sha256": sha(native), **paths})
    manifest = b"".join(raw_json(row) for row in rows)
    audits = raw_json({"schema": "real_lane_audits:v1", "manifest_sha256": sha(manifest),
                       "groups": [{"group_key": rows[0]["group_key"], "split": "train", "count": 3,
                                   "sample_ids": [row["image_id"] for row in rows]}]})
    (root / "manifest.jsonl").write_bytes(manifest)
    (root / "audits.json").write_bytes(audits)
    (root / "import.json").write_bytes(raw_json({"schema": "real_lane_import:v1", "source": source,
        "license": "CC0-1.0" if source.startswith("college") else "CC-BY-4.0", "state": "awaiting_audit",
        "manifest_sha256": sha(manifest), "audits_sha256": sha(audits), "auto_promote": False}))
    return root, rows, RealSourceReview(tmp_path, source)


def decision(detail, action, event):
    return SimulationDecision(image_id=detail["image_id"], action=action,
                              event_id=f"event{event:011d}", revision=detail["revision"],
                              fingerprint=detail["fingerprint"])


def repin(root, rows):
    manifest = b"".join(raw_json(row) for row in rows)
    (root / "manifest.jsonl").write_bytes(manifest)
    audits = json.loads((root / "audits.json").read_bytes())
    audits["manifest_sha256"] = sha(manifest)
    for group in audits["groups"]:
        group["split"] = next(row["split"] for row in rows if row["group_key"] == group["group_key"])
    audits_raw = raw_json(audits)
    (root / "audits.json").write_bytes(audits_raw)
    metadata = json.loads((root / "import.json").read_bytes())
    metadata.update(manifest_sha256=sha(manifest), audits_sha256=sha(audits_raw))
    (root / "import.json").write_bytes(raw_json(metadata))


def test_sample_approval_releases_only_its_isolated_train_group(tmp_path):
    root, rows, store = fixture(tmp_path)
    assert store.status()["counts"]["eligible_train_candidates"] == 0
    for index, row in enumerate(rows):
        detail = store.detail(row["image_id"])
        assert detail["geometry"]["width"] == 128
        store.decide(decision(detail, "approve", index), "Nick")
    result = store.status()
    assert result["counts"]["eligible_train_candidates"] == 3
    assert result["counts"]["eligible_group_train_candidates"] == 3
    assert result["counts"]["individually_cleared_held"] == result["counts"]["held_uncleared"] == 0
    assert result["groups"][0]["state"] == "eligible"
    assert store.eligible_records()["train_images"] == 3
    assert not (tmp_path / "registry.sqlite").exists()
    assert (root / "review.sqlite").exists()


def test_changed_native_label_or_manifest_blocks_previous_approval(tmp_path):
    root, rows, store = fixture(tmp_path, "pandaset_plus")
    detail = store.detail(rows[0]["image_id"])
    store.decide(decision(detail, "approve", 1), "Nick")
    (root / rows[0]["source_label"]).write_bytes(b"changed\n")
    with pytest.raises(HTTPException) as error:
        store.detail(rows[0]["image_id"])
    assert error.value.status_code == 409
    (root / rows[0]["source_label"]).write_bytes(b"20 20 10 70\n")
    (root / "manifest.jsonl").write_bytes((root / "manifest.jsonl").read_bytes() + b"\n")
    with pytest.raises(HTTPException) as error:
        store.snapshot(fresh=True)
    assert error.value.status_code == 409


def test_pandaset_sample_approval_does_not_assert_visible_paint_for_every_frame(tmp_path):
    _, rows, store = fixture(tmp_path, "pandaset_plus")
    for index, row in enumerate(rows):
        detail = store.detail(row["image_id"])
        store.decide(decision(detail, "approve", index), "Nick")
    result = store.status()
    assert result["groups"][0]["state"] == "eligible"
    assert result["counts"]["eligible_train_candidates"] == 0
    assert store.eligible_records()["train_images"] == 0


def test_audit_queue_keeps_group_samples_together_with_train_first(tmp_path):
    root, rows, _ = fixture(tmp_path)
    rows[0]["split"] = "hold"
    rows[0]["group_key"] = "hold/scene-b"
    for row in rows[1:]:
        row["group_key"] = "train/scene-a"
    manifest = b"".join(raw_json(row) for row in rows)
    audits = raw_json({"schema": "real_lane_audits:v1", "manifest_sha256": sha(manifest),
                       "groups": [{"group_key": "hold/scene-b", "split": "hold", "count": 1,
                                   "sample_ids": [rows[0]["image_id"]]},
                                  {"group_key": "train/scene-a", "split": "train", "count": 2,
                                   "sample_ids": [row["image_id"] for row in rows[1:]]}]})
    (root / "manifest.jsonl").write_bytes(manifest)
    (root / "audits.json").write_bytes(audits)
    (root / "import.json").write_bytes(raw_json({"schema": "real_lane_import:v1",
        "source": "college_station_lane_marking", "license": "CC0-1.0", "state": "awaiting_audit",
        "manifest_sha256": sha(manifest), "audits_sha256": sha(audits), "auto_promote": False}))
    store = RealSourceReview(tmp_path, "college_station_lane_marking")
    first = store.queue(limit=2)
    assert [item["image_id"] for item in first["items"]] == [row["image_id"] for row in rows[1:]]
    after = store.queue(after=first["next_after"])
    assert after["items"][0]["image_id"] == rows[0]["image_id"]


def test_warned_sample_cannot_approve_and_skip_holds_group(tmp_path):
    _, rows, store = fixture(tmp_path, warning=True)
    detail = store.detail(rows[0]["image_id"])
    with pytest.raises(HTTPException) as error:
        store.decide(decision(detail, "approve", 1), "Nick")
    assert error.value.status_code == 422
    store.decide(decision(detail, "skip", 2), "Nick")
    assert store.status()["groups"][0]["state"] == "awaiting_audit"
    assert store.queue(status="pending")["items"][0]["image_id"] == rows[1]["image_id"]


def test_segmented_correction_preserves_untouched_metadata_and_originals(tmp_path):
    root, rows, store = fixture(tmp_path)
    row = rows[0]
    path = root / row["geometry"]
    source = json.loads(path.read_bytes())
    source["lanes"] = [
        {"slot": 2, "points": [[20, 20, True], [18, 30, True], [12, 60, True], [10, 70, True]],
         "segments": [{"points": [[20, 20, True], [18, 30, True]], "source": "native-section"},
                      [[12, 60, True], [10, 70, True]]], "physical": True},
        {"slot": 3, "points": [[100, 20, True], [110, 70, True]],
         "fit": {"basis": "source-fit"}, "physical": True}]
    path.write_bytes(raw_json(source))
    row["geometry_sha256"] = sha(path.read_bytes())
    manifest = b"".join(raw_json(item) for item in rows)
    (root / "manifest.jsonl").write_bytes(manifest)
    audits = json.loads((root / "audits.json").read_bytes())
    audits["manifest_sha256"] = sha(manifest)
    audits_raw = raw_json(audits)
    (root / "audits.json").write_bytes(audits_raw)
    metadata = json.loads((root / "import.json").read_bytes())
    metadata.update(manifest_sha256=sha(manifest), audits_sha256=sha(audits_raw))
    (root / "import.json").write_bytes(raw_json(metadata))
    originals = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    item = store.detail(row["image_id"])
    lanes = [{"slot": 2, "points": [[20, 20], [18, 30], [12, 60], [10, 70]],
              "segments": [[[20, 20], [18, 30]], [[12, 60], [10, 70]]]},
             {"slot": 3, "points": [[100, 20], [110, 70]]}]
    no_op = store.correct(SimulationCorrection(image_id=row["image_id"], revision=0,
                           fingerprint=item["fingerprint"], lanes=lanes), "Nick")
    assert no_op["geometry"] == source
    assert no_op["fingerprint"] == item["fingerprint"]
    assert not (root / "review.sqlite").exists()
    for index, sample in enumerate(rows):
        store.decide(decision(store.detail(sample["image_id"]), "approve", index), "Nick")
    approved = store.detail(row["image_id"])
    stale_decision = decision(approved, "approve", 10)
    lanes[0]["points"][0] = [21, 20]
    lanes[0]["segments"][0][0] = [21, 20]
    corrected = store.correct(SimulationCorrection(image_id=row["image_id"], revision=approved["revision"],
                              fingerprint=approved["fingerprint"], lanes=lanes), "Nick")
    assert corrected["geometry"]["lanes"][1] == source["lanes"][1]
    assert corrected["geometry"]["lanes"][0]["segments"] == lanes[0]["segments"]
    assert corrected["source_geometry"] == source
    assert corrected["decision"] is None and corrected["previous_decision_stale"]
    assert store.eligible_records()["train_images"] == 0
    with pytest.raises(HTTPException) as error:
        store.decide(stale_decision, "Nick")
    assert error.value.status_code == 409
    reopened = RealSourceReview(tmp_path, "college_station_lane_marking")
    assert reopened.detail(row["image_id"])["geometry"] == corrected["geometry"]
    reopened.decide(decision(reopened.detail(row["image_id"]), "approve", 11), "Nick")
    exported = {item["image_id"]: item for item in reopened.eligible_records()["records"]}[row["image_id"]]
    assert exported["geometry_origin"] == "human_audit_correction"
    assert json.loads((root / exported["geometry"]).read_bytes())["lanes"][0]["segments"] == lanes[0]["segments"]
    assert exported["original_geometry"] == row["geometry"]
    assert all(p.read_bytes() == raw for p, raw in originals.items())


def test_explicit_warning_resolution_is_sample_only_bound_and_keeps_hold_split(tmp_path):
    root, rows, store = fixture(tmp_path)
    for row in rows:
        row.update(split="hold", intended_split="train", qc_reasons=["potential_transverse_marking"])
    repin(root, rows)
    originals = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    item = store.detail(rows[0]["image_id"])
    assert item["repairable_qc_reasons"] == ["potential_transverse_marking"]
    corrected = store.correct(SimulationCorrection(image_id=item["image_id"], revision=0,
        fingerprint=item["fingerprint"], lanes=item["geometry"]["lanes"],
        resolved_qc_reasons=["potential_transverse_marking"], approve=True), "Human")
    assert corrected["fingerprint"] != item["fingerprint"]
    assert corrected["qc_reasons"] == []
    assert corrected["source_qc_reasons"] == corrected["resolved_qc_reasons"] == ["potential_transverse_marking"]
    assert corrected["split"] == "hold" and corrected["intended_split"] == "train"
    assert corrected["decision"]["action"] == "approve"
    status = store.status()
    assert status["groups"][0]["qc_flagged_images"] == 2
    assert status["groups"][0]["eligible_train_images"] == 0
    assert status["counts"]["approved"] == 1
    assert status["counts"]["eligible_train_candidates"] == 1
    assert status["counts"]["eligible_group_train_candidates"] == 0
    assert status["counts"]["individually_cleared_held"] == 1
    assert status["counts"]["held_uncleared"] == 2
    eligible = store.eligible_records()
    assert eligible["train_images"] == 1
    assert eligible["records"][0]["original_split"] == "hold"
    assert eligible["records"][0]["clearance"]["type"] == "individual_human_approval"
    assert store.detail(rows[1]["image_id"])["qc_reasons"] == ["potential_transverse_marking"]
    reopened = RealSourceReview(tmp_path, "college_station_lane_marking")
    assert reopened.detail(item["image_id"])["resolved_qc_reasons"] == ["potential_transverse_marking"]
    assert all(p.read_bytes() == raw for p, raw in originals.items())
    edited = reopened.correct(SimulationCorrection(image_id=item["image_id"], revision=corrected["revision"],
        fingerprint=corrected["fingerprint"], lanes=[{"slot": 2, "points": [[22, 20], [10, 70]]}],
        approve=True), "Human")
    assert edited["resolved_qc_reasons"] == ["potential_transverse_marking"]
    assert edited["decision"]["action"] == "approve"
    cleared = reopened.correct(SimulationCorrection(image_id=item["image_id"], revision=edited["revision"],
        fingerprint=edited["fingerprint"], lanes=edited["geometry"]["lanes"], resolved_qc_reasons=[]), "Human")
    assert cleared["resolved_qc_reasons"] == [] and cleared["qc_reasons"] == ["potential_transverse_marking"]
    assert cleared["previous_decision_stale"] and cleared["decision"] is None
    rows[0]["session_id"] = "changed-source-provenance"
    repin(root, rows)
    stale = reopened.detail(item["image_id"])
    assert stale["qc_reasons"] == ["potential_transverse_marking"]
    assert stale["resolved_qc_reasons"] == [] and stale["decision"] is None
    assert stale["previous_decision_stale"]


def test_remaining_structural_warning_saves_overlay_without_autoapproval(tmp_path):
    root, rows, store = fixture(tmp_path)
    rows[0]["qc_reasons"] = ["potential_transverse_marking", "native_coverage_incomplete"]
    repin(root, rows)
    item = store.detail(rows[0]["image_id"])
    corrected = store.correct(SimulationCorrection(image_id=item["image_id"], revision=0,
        fingerprint=item["fingerprint"], lanes=[{"slot": 2, "points": [[21, 20], [10, 70]]}],
        resolved_qc_reasons=["potential_transverse_marking"], approve=True), "Human")
    assert corrected["correction"] and corrected["decision"] is None
    assert corrected["qc_reasons"] == ["native_coverage_incomplete"]
    with pytest.raises(HTTPException) as error:
        store.decide(decision(corrected, "approve", 99), "Human")
    assert error.value.status_code == 422


@pytest.mark.parametrize("reason", ["native_coverage_incomplete", "invalid_native_number", "invalid_native_polyline",
    "degenerate_native_polyline", "unknown_warning"])
def test_missing_annotation_or_unknown_warning_cannot_be_dismissed_by_ordinary_checkbox(tmp_path, reason):
    root, rows, store = fixture(tmp_path)
    rows[0]["qc_reasons"] = [reason]
    repin(root, rows)
    item = store.detail(rows[0]["image_id"])
    with pytest.raises(HTTPException) as error:
        store.correct(SimulationCorrection(image_id=item["image_id"], revision=0,
            fingerprint=item["fingerprint"], lanes=item["geometry"]["lanes"],
            resolved_qc_reasons=[reason], approve=True), "Human")
    assert error.value.status_code == 422
    assert not (root / "review.sqlite").exists()


@pytest.mark.parametrize("reason", ["native_polyline_not_single_valued", "native_points_outside_image"])
def test_omitted_native_line_warning_can_be_explicitly_resolved_on_valid_effective_geometry(tmp_path, reason):
    root, rows, store = fixture(tmp_path)
    for row in rows:
        row.update(split="hold", intended_split="train", qc_reasons=[reason])
    repin(root, rows)
    originals = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    item = store.detail(rows[0]["image_id"])
    assert item["repairable_qc_reasons"] == [reason]
    # An omitted problematic native line leaves valid effective geometry. Saving
    # alone does not resolve the source warning or approve the sample.
    saved = store.correct(SimulationCorrection(image_id=item["image_id"], revision=0,
        fingerprint=item["fingerprint"], lanes=item["geometry"]["lanes"], approve=True), "Human")
    assert saved["qc_reasons"] == [reason] and saved["decision"] is None
    corrected = store.correct(SimulationCorrection(image_id=item["image_id"], revision=saved["revision"],
        fingerprint=saved["fingerprint"], lanes=saved["geometry"]["lanes"],
        resolved_qc_reasons=[reason], approve=True), "Human")
    assert corrected["qc_reasons"] == [] and corrected["resolved_qc_reasons"] == [reason]
    assert corrected["source_qc_reasons"] == [reason] and corrected["decision"]["action"] == "approve"
    assert corrected["source_geometry"] == item["source_geometry"]
    assert corrected["split"] == "hold" and corrected["intended_split"] == "train"
    assert store.status()["groups"][0]["qc_flagged_images"] == 2
    assert store.eligible_records()["train_images"] == 1
    assert all(p.read_bytes() == raw for p, raw in originals.items())
    reopened = RealSourceReview(tmp_path, "college_station_lane_marking")
    assert reopened.detail(item["image_id"])["resolved_qc_reasons"] == [reason]


@pytest.mark.parametrize("points", [
    [[20, 20], [30, 20]],
    [[20, 20], [30, 60], [10, 40]],
    [[20, 20], [30, 60], [10, 40], [5, 70]],
])
def test_single_valued_resolution_rejects_duplicate_rows_and_folded_boundaries(tmp_path, points):
    root, rows, store = fixture(tmp_path)
    rows[0]["qc_reasons"] = ["native_polyline_not_single_valued"]
    repin(root, rows)
    item = store.detail(rows[0]["image_id"])
    with pytest.raises(HTTPException) as error:
        store.correct(SimulationCorrection(image_id=item["image_id"], revision=0,
            fingerprint=item["fingerprint"], lanes=[{"slot": 2, "points": points}],
            resolved_qc_reasons=["native_polyline_not_single_valued"], approve=True), "Human")
    assert error.value.status_code == 422
    assert "one point per row" in error.value.detail
    assert not (root / "review.sqlite").exists()


@pytest.mark.parametrize("points", [[[20, 20], [10, 70]], [[10, 70], [20, 20]]])
def test_single_valued_resolution_accepts_either_strict_row_direction(tmp_path, points):
    root, rows, store = fixture(tmp_path)
    rows[0]["qc_reasons"] = ["native_polyline_not_single_valued"]
    repin(root, rows)
    item = store.detail(rows[0]["image_id"])
    corrected = store.correct(SimulationCorrection(image_id=item["image_id"], revision=0,
        fingerprint=item["fingerprint"], lanes=[{"slot": 2, "points": points}],
        resolved_qc_reasons=["native_polyline_not_single_valued"], approve=True), "Human")
    assert corrected["qc_reasons"] == [] and corrected["decision"]["action"] == "approve"


@pytest.mark.parametrize("lane", [
    {"slot": 2, "points": [[20, 20], [128, 70]]},
    {"slot": 2, "points": [[20, 20], [10, -1]]},
    {"slot": 2, "points": [[20, 20], [float("nan"), 70]]},
    {"slot": 5, "points": [[20, 20], [10, 70]]},
])
def test_outside_image_resolution_requires_valid_finite_in_frame_geometry(tmp_path, lane):
    root, rows, store = fixture(tmp_path)
    rows[0]["qc_reasons"] = ["native_points_outside_image"]
    repin(root, rows)
    item = store.detail(rows[0]["image_id"])
    with pytest.raises(HTTPException) as error:
        store.correct(SimulationCorrection(image_id=item["image_id"], revision=0,
            fingerprint=item["fingerprint"], lanes=[lane],
            resolved_qc_reasons=["native_points_outside_image"], approve=True), "Human")
    assert error.value.status_code == 422
    assert not (root / "review.sqlite").exists()


@pytest.mark.parametrize("reason", ["native_coverage_incomplete", "invalid_native_number",
    "invalid_native_polyline", "degenerate_native_polyline"])
def test_complete_manual_annotation_repairs_only_one_photo_and_preserves_source_hold(tmp_path, reason):
    root, rows, store = fixture(tmp_path)
    for row in rows:
        row.update(split="hold", intended_split="train", qc_reasons=[reason])
    repin(root, rows)
    originals = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    item = store.detail(rows[0]["image_id"])
    assert item["complete_annotation_qc_reasons"] == [reason]
    assert reason not in item["repairable_qc_reasons"]
    corrected = store.correct(SimulationCorrection(image_id=item["image_id"], revision=0,
        fingerprint=item["fingerprint"], lanes=[{"slot": 2, "points": [[22, 20], [10, 70]]}],
        resolved_qc_reasons=[reason], complete_annotation=True, approve=True), "Human")
    assert corrected["qc_reasons"] == [] and corrected["resolved_qc_reasons"] == [reason]
    assert corrected["decision"]["action"] == "approve"
    assert corrected["human_complete_annotation"] is True and corrected["no_visible_lane_paint"] is False
    assert corrected["geometry"]["annotation_scope"] == "single_photo_manual_replacement"
    assert corrected["source_qc_reasons"] == [reason] and corrected["split"] == "hold"
    assert corrected["intended_split"] == "train" and corrected["source_geometry"] == item["source_geometry"]
    assert store.status()["groups"][0]["qc_flagged_images"] == 2
    assert store.status()["groups"][0]["approved"] == 1
    assert store.status()["counts"]["eligible_train_candidates"] == 1
    assert store.status()["counts"]["individually_cleared_held"] == 1
    assert store.status()["counts"]["held_uncleared"] == 2
    assert store.eligible_records()["train_images"] == 1
    assert store.detail(rows[1]["image_id"])["qc_reasons"] == [reason]
    assert all(p.read_bytes() == raw for p, raw in originals.items())
    reopened = RealSourceReview(tmp_path, "college_station_lane_marking")
    assert reopened.detail(item["image_id"])["human_complete_annotation"] is True


def test_complete_annotation_confirmation_is_bound_to_exact_saved_geometry(tmp_path):
    root, rows, store = fixture(tmp_path)
    rows[0]["qc_reasons"] = ["native_coverage_incomplete"]
    repin(root, rows)
    item = store.detail(rows[0]["image_id"])
    corrected = store.correct(SimulationCorrection(image_id=item["image_id"], revision=0,
        fingerprint=item["fingerprint"], lanes=item["geometry"]["lanes"],
        resolved_qc_reasons=["native_coverage_incomplete"], complete_annotation=True, approve=True), "Human")
    unchanged = store.correct(SimulationCorrection(image_id=item["image_id"], revision=corrected["revision"],
        fingerprint=corrected["fingerprint"], lanes=corrected["geometry"]["lanes"]), "Human")
    assert unchanged["fingerprint"] == corrected["fingerprint"] and unchanged["human_complete_annotation"]
    with pytest.raises(HTTPException) as error:
        store.correct(SimulationCorrection(image_id=item["image_id"], revision=corrected["revision"],
            fingerprint=corrected["fingerprint"], lanes=[{"slot": 2, "points": [[25, 20], [10, 70]]}],
            approve=True), "Human")
    assert error.value.status_code == 422
    assert store.detail(item["image_id"])["fingerprint"] == corrected["fingerprint"]
    draft = store.correct(SimulationCorrection(image_id=item["image_id"], revision=corrected["revision"],
        fingerprint=corrected["fingerprint"], lanes=[{"slot": 2, "points": [[25, 20], [10, 70]]}],
        resolved_qc_reasons=[], complete_annotation=False, no_visible_lane_paint=False, approve=True), "Human")
    assert draft["human_complete_annotation"] is False and draft["qc_reasons"] == ["native_coverage_incomplete"]
    assert draft["decision"] is None and draft["previous_decision_stale"]
    assert "annotation_scope" not in draft["geometry"]


def test_empty_complete_annotation_requires_separate_no_visible_paint_confirmation(tmp_path):
    root, rows, store = fixture(tmp_path)
    rows[0]["qc_reasons"] = ["native_coverage_incomplete"]
    repin(root, rows)
    item = store.detail(rows[0]["image_id"])
    payload = {"image_id": item["image_id"], "revision": 0, "fingerprint": item["fingerprint"],
               "lanes": [], "resolved_qc_reasons": ["native_coverage_incomplete"],
               "complete_annotation": True, "approve": True}
    with pytest.raises(HTTPException) as error:
        store.correct(SimulationCorrection(**payload), "Human")
    assert error.value.status_code == 422
    assert not (root / "review.sqlite").exists()
    corrected = store.correct(SimulationCorrection(**payload, no_visible_lane_paint=True), "Human")
    assert corrected["no_visible_lane_paint"] is True and corrected["human_complete_annotation"] is True
    assert corrected["qc_reasons"] == [] and corrected["decision"]["action"] == "approve"


@pytest.mark.parametrize("points", [[[20, 20], [30, 20]], [[20, 20], [30, 60], [10, 40]]])
def test_complete_manual_annotation_requires_monotone_boundaries(tmp_path, points):
    root, rows, store = fixture(tmp_path)
    rows[0]["qc_reasons"] = ["native_coverage_incomplete"]
    repin(root, rows)
    item = store.detail(rows[0]["image_id"])
    with pytest.raises(HTTPException) as error:
        store.correct(SimulationCorrection(image_id=item["image_id"], revision=0,
            fingerprint=item["fingerprint"], lanes=[{"slot": 2, "points": points}],
            resolved_qc_reasons=["native_coverage_incomplete"], complete_annotation=True, approve=True), "Human")
    assert error.value.status_code == 422
    assert not (root / "review.sqlite").exists()


def test_held_counts_require_a_final_matching_approval_and_do_not_call_full_export(tmp_path, monkeypatch):
    root, rows, store = fixture(tmp_path)
    for row in rows:
        row.update(split="hold", intended_split="train", qc_reasons=["potential_transverse_marking"])
    repin(root, rows)
    item = store.detail(rows[0]["image_id"])
    corrected = store.correct(SimulationCorrection(image_id=item["image_id"], revision=0,
        fingerprint=item["fingerprint"], lanes=item["geometry"]["lanes"],
        resolved_qc_reasons=["potential_transverse_marking"]), "Human")
    def no_full_export():
        pytest.fail("Status must not verify every eligible photo through the export API")
    monkeypatch.setattr(store, "eligible_records", no_full_export)
    assert store.status()["counts"]["eligible_train_candidates"] == 0
    assert store.status()["counts"]["held_uncleared"] == 3
    store.decide(decision(corrected, "approve", 900), "Human")
    assert store.status()["counts"]["eligible_train_candidates"] == 1
    assert store.status()["counts"]["individually_cleared_held"] == 1
    skipped = store.detail(item["image_id"])
    store.decide(decision(skipped, "skip", 901), "Human")
    assert store.status()["counts"]["eligible_train_candidates"] == 0
    assert store.status()["counts"]["held_uncleared"] == 3


@pytest.mark.parametrize("intended_split", [None, "val", "test"])
def test_held_counts_never_promote_a_non_train_intended_photo(tmp_path, intended_split):
    root, rows, store = fixture(tmp_path)
    for row in rows:
        row.update(split="hold", intended_split=intended_split, qc_reasons=["potential_transverse_marking"])
    repin(root, rows)
    item = store.detail(rows[0]["image_id"])
    store.correct(SimulationCorrection(image_id=item["image_id"], revision=0,
        fingerprint=item["fingerprint"], lanes=item["geometry"]["lanes"],
        resolved_qc_reasons=["potential_transverse_marking"], approve=True), "Human")
    counts = store.status()["counts"]
    assert counts["eligible_train_candidates"] == counts["individually_cleared_held"] == 0
    assert counts["held_uncleared"] == 3
    assert store.eligible_records()["train_images"] == 0


def test_changed_held_correction_bytes_are_not_counted_as_cleared(tmp_path):
    root, rows, store = fixture(tmp_path)
    for row in rows:
        row.update(split="hold", intended_split="train", qc_reasons=["potential_transverse_marking"])
    repin(root, rows)
    item = store.detail(rows[0]["image_id"])
    store.correct(SimulationCorrection(image_id=item["image_id"], revision=0,
        fingerprint=item["fingerprint"], lanes=item["geometry"]["lanes"],
        resolved_qc_reasons=["potential_transverse_marking"], approve=True), "Human")
    correction = store.corrections()[item["image_id"]]
    (root / correction["geometry_path"]).write_bytes(b"changed saved correction")
    counts = store.status()["counts"]
    assert counts["eligible_train_candidates"] == counts["individually_cleared_held"] == 0
    assert counts["held_uncleared"] == 3
    assert store.eligible_records()["train_images"] == 0


def test_held_pandaset_count_still_requires_per_image_paint_semantics(tmp_path):
    root, rows, store = fixture(tmp_path, "pandaset_plus")
    for row in rows:
        row.update(split="hold", intended_split="train")
    repin(root, rows)
    item = store.detail(rows[0]["image_id"])
    store.correct(SimulationCorrection(image_id=item["image_id"], revision=0,
        fingerprint=item["fingerprint"], lanes=[{"slot": 2, "points": [[22, 20], [10, 70]]}],
        approve=True), "Human")
    counts = store.status()["counts"]
    assert counts["eligible_train_candidates"] == counts["individually_cleared_held"] == 0
    assert counts["held_uncleared"] == 3
    assert store.eligible_records()["train_images"] == 0
