import json

from PIL import Image
import pytest

from bcdata.bootstrap import atomic_json
from bcdata.college_group_clearance import FILENAME, SOURCE, prepare
from bcdata.converted_merge import build_converted_merge, load_converted_merge, plan_converted_merge
from bcdata.teachers.base import file_sha256
from bcdata.ufld_baseline import NativeDataset, load_frozen_export
from tests.test_college_group_clearance import acknowledge, add_targets, held_cohort, write_artifact
from tests.test_real_source_review import repin
from tests.test_ufld_experiment import _rgb, experiment_fixture


def cleared_held_source(base):
    root, rows, store = held_cohort(base.temp)
    targets = add_targets(root, rows)
    for index, row in enumerate(rows):
        path = root / row["image"]
        Image.fromarray(_rgb(index + 20)[:72, :128]).save(path, quality=95)
        row["source_sha256"] = file_sha256(path)
    repin(root, rows)
    for row in rows[:3]:
        acknowledge(store, row)
    write_artifact(root, prepare(store))
    atomic_json(base.dataset / "split_inventory.json", base.inventory)
    return root, rows, targets, store


def test_converted_merge_pins_group_proof_and_reports_individual_and_cohort_additions_separately(experiment_fixture):
    f = experiment_fixture
    root, rows, targets, store = cleared_held_source(f)
    originals = {path: path.read_bytes() for folder in (root, f.dataset)
                 for path in folder.rglob("*") if path.is_file()}
    proof_bytes = (root / FILENAME).read_bytes()
    plan = plan_converted_merge(f.temp, f.base, sources=[SOURCE])
    assert plan["candidate_train_images"] == 5
    assert plan["source_snapshots"][SOURCE]["group_warning_clearance_sha256"] == file_sha256(root / FILENAME)

    report = build_converted_merge(f.temp, f.base, f.output, sources=[SOURCE])
    dataset, records, _ = load_converted_merge(f.output)
    assert report["extra_train_images"] == 5
    assert report["individually_cleared_held_images"] == 3
    assert report["group_cleared_held_images"] == 2
    assert report["train_images"] == 7 and report["val_images"] == 1
    assert report["test_images_used"] == report["source_val_images_used"] == 0
    assert records[:len(f.records)] == f.records
    assert load_frozen_export(f.output)[1] == records
    assert len(NativeDataset(dataset, "train")) == 7
    assert len(NativeDataset(dataset, "val")) == 1
    retained = f"source_imports/{SOURCE}/{FILENAME}"
    assert (dataset / retained).read_bytes() == proof_bytes
    hashes = json.loads((dataset / "hashes.json").read_bytes())
    assert hashes[retained] == report["source_snapshots"][SOURCE]["group_warning_clearance_sha256"]
    additions = {row["image_id"]: row for row in records[len(f.records):]}
    assert set(additions) == {row["image_id"] for row in rows}
    for target in targets:
        record = additions[target["image_id"]]
        assert record["original_split"] == "hold" and record["split"] == "train"
        proof = record["isolated_source_provenance"]["clearance"]
        assert proof["type"] == "sampled_unchanged_warning_cohort"
        assert proof["geometry_unchanged"] is True and proof["human_decisions_created"] == 0
        assert len(proof["sample_decisions"]) == 3
    assert store.status()["counts"]["group_cleared_held"] == 2
    assert all(path.read_bytes() == raw for path, raw in originals.items())


def test_changed_group_proof_during_merge_aborts_publication(experiment_fixture):
    f = experiment_fixture
    root, _, _, _ = cleared_held_source(f)
    changed = False

    def progress(value):
        nonlocal changed
        if value["phase"] == "verifying_source_clearance" and not changed:
            changed = True
            # Whitespace preserves the parsed proof, but changes its pinned
            # source bytes and must prevent publication of the snapshot.
            path = root / FILENAME
            path.write_bytes(path.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="changed while building"):
        build_converted_merge(f.temp, f.base, f.output, sources=[SOURCE], progress=progress)
    assert changed
    assert not f.output.exists()
    assert list(f.temp.glob("experiment.preparing-*/FAILED.json"))
