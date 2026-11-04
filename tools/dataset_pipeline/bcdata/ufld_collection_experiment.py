"""Private, provisional UFLDv2 collection comparisons; never an approved export.

The complete cleared base is immutable. Model additions are exact copies of a
verified teacher experiment. Synthetic additions use pinned simulator ground
truth, with automatic structural QC but deliberately without human audit gates.
The original import, its audit decisions, and approved collections are untouched.
"""
from __future__ import annotations

from collections import Counter
import copy
import json
import os
from pathlib import Path
import shutil
import time
import uuid

from .bootstrap import atomic_json
from .export_ufld import cache_entry, write_sample
from .geometry import make_crop
from .import_tulane import PINNED, SOURCE, VERSION, native_geometry, source_records
from .quality import read_rgb
from .teachers.base import file_sha256
from .ufld_baseline import HEIGHT, WIDTH
from .ufld_experiment import (
    PSEUDO_DECISION, PSEUDO_ORIGIN, _base_state, _digest, _geometry, _identity,
    _inventory, _read, _relative, _sample_paths, _session,
    _source_hash, _teacher_provenance, _verify_sample, load_experiment_export,
)

FORMAT = "ufld_collection_experiment_v1"
SIM_ORIGIN = "simulator_projected_gt"
SIM_DECISION = "experimental_unaudited_simulator"
COLLECTIONS = {
    "converted_plus_proposals": (True, False),
    "converted_plus_simulated": (False, True),
    "converted_plus_proposals_plus_simulated": (True, True),
}
ALIASES = {2: "converted_plus_proposals", 3: "converted_plus_simulated",
           4: "converted_plus_proposals_plus_simulated"}
FINAL_IMPORT_STATES = {"awaiting_audit", "ready", "complete", "audit_ready", "awaiting_audits"}


def _asset(root, relative):
    path = (root / _relative(relative)).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Collection asset escapes its isolated snapshot")
    return path


def _copy(source, destination, *, immutable=False):
    """Only frozen prepared sample files may share immutable storage."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if immutable:
        try:
            os.link(source, destination)
            return
        except OSError:
            pass
    shutil.copy2(source, destination)


def _source_simulation(root):
    """Read the pinned completed import, without opening its review database."""
    root = Path(root).resolve()
    metadata = _read(root / "import.json")
    if (metadata.get("schema") != "tulane_simulation_import:v1"
            or metadata.get("source") != SOURCE or metadata.get("domain") != "synthetic"
            or metadata.get("license") != "Apache-2.0" or metadata.get("dataset_version") != VERSION
            or metadata.get("state") not in FINAL_IMPORT_STATES or metadata.get("errors")):
        raise ValueError("A complete commercially licensed synthetic import is required")
    plan = _read(root / "plan.json")
    sources = {split: {"filename": row[0], "count": row[1], "sha256": row[2]}
               for split, row in PINNED.items()}
    if (plan.get("schema") != "tulane_simulation_plan:v1" or plan.get("source_annotations") != sources
            or plan.get("pilot") is not False or plan.get("dataset_version") != VERSION):
        raise ValueError("Simulation must use the complete pinned native source annotations")
    native = {row["image_id"]: row for row in source_records(root / "upstream", expected=PINNED)}
    manifest = root / "manifest.jsonl"
    if metadata.get("manifest_sha256") != file_sha256(manifest):
        raise ValueError("Completed simulation manifest fingerprint changed")
    records, seen = [], set()
    for line in manifest.read_bytes().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        identifier = _identity(record)
        original = native.get(identifier)
        if identifier in seen or original is None:
            raise ValueError("Simulation manifest contains duplicate or unpinned images")
        seen.add(identifier)
        if (record.get("source") != SOURCE or record.get("synthetic") is not True
                or record.get("pool") != "commercial" or record.get("dataset_version") != VERSION
                or record.get("label_origin") != SIM_ORIGIN
                or any(record.get(key) != original.get(key) for key in
                       ("raw_file", "split", "session_id", "group_key", "qc_reasons"))):
            raise ValueError("Simulation metadata differs from pinned native annotations")
        if record.get("image") != "images/" + original["raw_file"]:
            raise ValueError("Simulation image is outside the synthetic source scope")
        for key in ("image", "geometry", "source_label"):
            _relative(record[key])
        _source_hash(record)
        records.append(record)
    if set(native) != seen or metadata.get("converted") != len(records) or plan.get("total_expected") != len(records):
        raise ValueError("Simulation import is incomplete")
    provenance = _read(root / "provenance/provenance.json")
    if (provenance.get("license") != "Apache-2.0" or provenance.get("domain") != "synthetic"
            or provenance.get("source") != SOURCE or provenance.get("dataset_version") != VERSION
            or provenance.get("download_scope") != ["train/source", "val/source"]):
        raise ValueError("Synthetic dataset license provenance is incomplete")
    for key, name in (("license_sha256", "LICENSE-Apache-2.0.txt"),
                      ("publisher_page_sha256", "publisher-page.html")):
        if provenance.get(key) != file_sha256(root / "provenance" / name):
            raise ValueError("Synthetic license evidence changed")
    return metadata, records, native


def _verify_native(root, record, original):
    assets = {key: _asset(root, record[key]) for key in ("image", "geometry", "source_label")}
    for key, hash_key in (("image", "source_sha256"), ("geometry", "geometry_sha256"),
                          ("source_label", "source_label_sha256")):
        if file_sha256(assets[key]) != record[hash_key]:
            raise ValueError("Native simulation bytes changed: " + key)
    if assets["source_label"].read_bytes() != original["native_line"]:
        raise ValueError("Simulation source label differs from pinned upstream bytes")
    geometry = _read(assets["geometry"])
    if geometry != original["geometry"]:
        raise ValueError("Simulation geometry differs from native projection")
    rgb = read_rgb(assets["image"])
    if rgb is None or rgb.shape != (720, 1280, 3):
        raise ValueError("Native simulation image must be 1280x720")
    return assets, rgb, _geometry(geometry, 1280, 720)


def _write_lists(target, records, cache):
    lists = target / "list"
    lists.mkdir(exist_ok=True)
    for split in ("train", "val"):
        lines = []
        for record in records:
            if record["split"] == split:
                image, _, segmentation = _sample_paths(record)
                flags = [int(any(point[0] >= 0 for point in curve)) for curve in cache[record["image"]]]
                lines.append(f"/{image.as_posix()} /{segmentation.as_posix()} " + " ".join(map(str, flags)))
        (lists / (split + "_gt.txt")).write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    for name in ("test", "test_gt"):
        (lists / (name + ".txt")).write_text("", encoding="utf-8")


def build_collection_experiment(base_run, output_run, *, split_inventory, collection_id,
                                model_run=None, sim_root=None):
    """Freeze a new private run. All simulator TRAIN rows passing QC are used.

    No audit query is performed. This exception only applies to these explicitly
    provisional experiments; it cannot change normal collection eligibility.
    """
    collection_id = ALIASES.get(collection_id, collection_id)
    if collection_id not in COLLECTIONS:
        raise ValueError("Unknown provisional collection")
    use_model, use_sim = COLLECTIONS[collection_id]
    if use_model != (model_run is not None) or use_sim != (sim_root is not None):
        raise ValueError("Collection inputs disagree with the requested composition")
    base_run, output_run = Path(base_run).resolve(), Path(output_run).resolve()
    if output_run.exists() or output_run == base_run or output_run.is_relative_to(base_run):
        raise ValueError("Collection experiment output must be a new isolated directory")
    base, base_records, ready, inventory, protected, reserved = _base_state(base_run, split_inventory)
    base_hashes, base_cache = _read(base / "hashes.json"), _read(base / "culane_anno_cache.json")
    sample_root, records, cache = base, copy.deepcopy(base_records), copy.deepcopy(base_cache)
    model_evidence = None
    if use_model:
        sample_root, records, _ = load_experiment_export(model_run)
        records = copy.deepcopy(records)
        model_report = _read(sample_root / "experiment.json")
        if (records[:len(base_records)] != base_records
                or model_report.get("base_manifest_sha256") != base_hashes["manifest.json"]):
            raise ValueError("Model proposal run does not preserve this exact cleared base")
        model_base_hashes = _read(sample_root / "base_hashes.json")
        if model_base_hashes != base_hashes:
            raise ValueError("Model proposal run has a different cleared byte snapshot")
        cache = _read(sample_root / "culane_anno_cache.json")
        for record in base_records:
            if cache.get(record["image"]) != base_cache.get(record["image"]):
                raise ValueError("Model proposal run changed cleared targets")
        model_evidence = {"run": str(Path(model_run).resolve()),
                          "manifest_sha256": file_sha256(sample_root / "manifest.json"),
                          "extra_train_images": len(records) - len(base_records)}
    sim_metadata = sim_records = native = None
    if use_sim:
        sim_root = Path(sim_root).resolve()
        sim_metadata, sim_records, native = _source_simulation(sim_root)
    stage = output_run.with_name(output_run.name + ".preparing-" + uuid.uuid4().hex)
    target = stage / "dataset"
    target.mkdir(parents=True)
    try:
        source_hashes = _read(sample_root / "hashes.json")
        for record in records:
            for path in _sample_paths(record):
                relative = path.as_posix()
                _copy(_asset(sample_root, relative), target / path, immutable=True)
                if file_sha256(target / path) != source_hashes.get(relative):
                    raise ValueError("Frozen source sample changed during isolated copy")
        for name, source in (("base_manifest.json", base / "manifest.json"),
                             ("base_hashes.json", base / "hashes.json"),
                             ("base_anno_cache.json", base / "culane_anno_cache.json"),
                             ("base_reserved_test_ids.json", base / "reserved_test_ids.json"),
                             ("base_export_marker.json", base / "export_marker.json")):
            _copy(source, target / name)
        for name in ("NOTICE.txt", "licenses"):
            source = base / name
            if source.is_dir():
                shutil.copytree(source, target / name)
            elif source.is_file():
                _copy(source, target / name)
        atomic_json(target / "split_inventory.json", list(inventory.values()))
        if use_model:
            for name in ("manifest.json", "culane_anno_cache.json", "experiment.json",
                         "teacher_provenance.json", "split_inventory.json", "hashes.json"):
                _copy(sample_root / name, target / "model_evidence" / name)
        if use_sim:
            for relative in ("import.json", "plan.json", "manifest.jsonl", "upstream/source_train.json",
                             "upstream/source_val.json", "provenance/provenance.json",
                             "provenance/LICENSE-Apache-2.0.txt", "provenance/publisher-page.html",
                             "provenance/NOTICE.txt"):
                _copy(sim_root / relative, target / "simulation_import" / relative)
        held_ids = {row["image_id"] for row in protected} | set(reserved)
        held_sessions = {_session(row) for row in protected}
        held_hashes = {row.get("source_sha256") or row.get("sha256") for row in protected} - {None, ""}
        held_hashes.update(_source_hash(row) for row in sim_records or [] if row["split"] == "val")
        seen_ids, seen_hashes = {r["image_id"] for r in records}, {_source_hash(r) for r in records}
        rejected, additions = [], []
        for row in sim_records or []:
            if row["split"] != "train":
                rejected.append({"image_id": row["image_id"], "reason": "official_simulation_validation_is_held_out"})
                continue
            if row["qc_reasons"]:
                rejected.append({"image_id": row["image_id"], "reason": "native_structural_qc", "qc_reasons": row["qc_reasons"]})
                continue
            identifier = row["image_id"]
            if identifier in seen_ids or identifier in held_ids or _session(row) in held_sessions:
                raise ValueError("Simulation identity/session overlaps cleared or held-out data")
            if _source_hash(row) in seen_hashes or _source_hash(row) in held_hashes:
                rejected.append({"image_id": identifier, "reason": "duplicate_or_held_out_source_fingerprint"})
                continue
            assets, rgb, geometry = _verify_native(sim_root, row, native[identifier])
            image = f"images/train/simulation/{identifier}.jpg"
            segmentation = "laneseg_label_w16/" + image.replace(".jpg", ".png")
            exists, lane_rows, _ = write_sample(rgb, geometry, make_crop({}, 1280, 720, WIDTH, HEIGHT),
                                                16, 95, target / image, target / segmentation)
            if not any(exists):
                rejected.append({"image_id": identifier, "reason": "native_no_trainable_sampled_geometry"})
                continue
            (target / image).with_suffix(".lines.txt").write_text("".join(
                " ".join(f"{x:.3f} {y}" for x, y in points) + " \n" for points in lane_rows if points), encoding="utf-8")
            evidence = {}
            for key, extension in (("image", ".jpg"), ("geometry", ".geometry.json"), ("source_label", ".native.json")):
                relative = "simulation_evidence/" + identifier + extension
                _copy(assets[key], target / relative)
                evidence[key] = relative
            record = {**copy.deepcopy(row), "image": image, "license": "Apache-2.0",
                      "geometry": evidence["geometry"], "source_label": evidence["source_label"],
                      "label_origin": SIM_ORIGIN, "decision": SIM_DECISION, "experimental": True,
                      "provisional": True, "unaudited": True, "audit_status": "not_consulted",
                      "training_weight": 1.0, "lanes": sum(exists), "native_evidence": evidence,
                      "export_geometry_sha256": _digest(geometry), "original_metadata": copy.deepcopy(row)}
            entry = cache_entry(lane_rows)
            _verify_sample(target, record, entry)
            records.append(record); cache[image] = entry; additions.append(record)
            seen_ids.add(identifier); seen_hashes.add(_source_hash(row))
        if use_sim and not additions:
            raise ValueError("No structurally valid native simulation TRAIN additions")
        if len(records) == len(base_records):
            raise ValueError("Private collection must include experimental additions")
        atomic_json(target / "manifest.json", records)
        atomic_json(target / "culane_anno_cache.json", cache)
        atomic_json(target / "reserved_test_ids.json", reserved)
        atomic_json(target / "rejected_extras.json", rejected)
        _copy(Path(__file__), target / "collection_builder.py")
        origins = dict(Counter(r["label_origin"] for r in records[len(base_records):]))
        report = {"format": FORMAT, "collection_id": collection_id, "experimental": True,
                  "provisional": True, "unaudited": True, "auto_promote": False,
                  "audit_policy": "not_consulted_private_provisional_only", "created_at": time.time(),
                  "base_run": str(base_run), "output_run": str(output_run),
                  "base_manifest_sha256": base_hashes["manifest.json"],
                  "base_train_images": sum(r["split"] == "train" for r in base_records),
                  "base_val_images": sum(r["split"] == "val" for r in base_records),
                  "extra_train_images": len(records) - len(base_records), "extra_origin_counts": origins,
                  "model_train_images": origins.get(PSEUDO_ORIGIN, 0),
                  "simulated_train_images": origins.get(SIM_ORIGIN, 0),
                  "train_images": sum(r["split"] == "train" for r in records),
                  "val_images": sum(r["split"] == "val" for r in records), "test_images_used": 0,
                  "model_evidence": model_evidence, "simulated_manifest_sha256": (
                      sim_metadata["manifest_sha256"] if use_sim else None),
                  "training_weight_policy": "equal_weight_all_structurally_valid_official_sim_train",
                  "simulator_policy": "native_projected_geometry_not_verified_visible_paint",
                  "crop_policy": "full_native_sensor_frame_no_extrapolation",
                  "collection_builder_sha256": file_sha256(target / "collection_builder.py"),
                  "rejection_counts": dict(Counter(r["reason"] for r in rejected))}
        atomic_json(target / "experiment.json", report)
        atomic_json(target / "export_marker.json", {"revision": ready["revision"], "errors": [],
            "validation": {"ok": True}, "format": FORMAT, "experimental": True,
            "provisional": True, "unaudited": True, "auto_promote": False,
            "base_manifest_sha256": base_hashes["manifest.json"],
            "manifest_sha256": file_sha256(target / "manifest.json")})
        _write_lists(target, records, cache)
        atomic_json(target / "hashes.json", {p.relative_to(target).as_posix(): file_sha256(p)
            for p in target.rglob("*") if p.is_file() and p.name != "hashes.json"})
        # Nested model hashes.json is evidence too, not an unhashed root envelope.
        if use_model:
            hashes = _read(target / "hashes.json")
            hashes["model_evidence/hashes.json"] = file_sha256(target / "model_evidence/hashes.json")
            atomic_json(target / "hashes.json", hashes)
        load_collection_export(stage)
        stage.rename(output_run)
        return report
    except Exception as error:
        atomic_json(stage / "FAILED.json", {"error": str(error), "originals_unchanged": True})
        raise


def load_collection_export(output_run):
    """Verify private provenance. Only --experimental-data dispatches here."""
    snapshot = Path(output_run).resolve() / "dataset"
    hashes = _read(snapshot / "hashes.json")
    required = {"manifest.json", "culane_anno_cache.json", "experiment.json", "base_manifest.json",
                "base_hashes.json", "base_anno_cache.json", "base_reserved_test_ids.json",
                "base_export_marker.json", "reserved_test_ids.json", "split_inventory.json",
                "export_marker.json", "collection_builder.py"}
    if not isinstance(hashes, dict) or not required <= set(hashes):
        raise ValueError("Collection snapshot is missing immutable provenance fingerprints")
    for relative, digest in hashes.items():
        if file_sha256(_asset(snapshot, relative)) != digest:
            raise ValueError("Frozen collection file changed: " + relative)
    report, marker = _read(snapshot / "experiment.json"), _read(snapshot / "export_marker.json")
    collection_id = report.get("collection_id")
    if collection_id not in COLLECTIONS:
        raise ValueError("Unknown provisional collection identity")
    use_model, use_sim = COLLECTIONS[collection_id]
    if (report.get("format") != FORMAT or marker.get("format") != FORMAT
            or report.get("provisional") is not True or marker.get("provisional") is not True
            or report.get("experimental") is not True or marker.get("experimental") is not True
            or report.get("auto_promote") is not False or marker.get("auto_promote") is not False
            or report.get("unaudited") is not True or marker.get("unaudited") is not True
            or report.get("audit_policy") != "not_consulted_private_provisional_only"
            or not marker.get("validation", {}).get("ok") or marker.get("errors")):
        raise ValueError("Collection must remain explicitly provisional and isolated")
    if report.get("collection_builder_sha256") != hashes["collection_builder.py"]:
        raise ValueError("Collection builder fingerprint changed")
    records, base = _read(snapshot / "manifest.json"), _read(snapshot / "base_manifest.json")
    cache, base_cache = _read(snapshot / "culane_anno_cache.json"), _read(snapshot / "base_anno_cache.json")
    base_hashes = _read(snapshot / "base_hashes.json")
    if (file_sha256(snapshot / "base_manifest.json") != base_hashes["manifest.json"]
            or file_sha256(snapshot / "base_anno_cache.json") != base_hashes["culane_anno_cache.json"]
            or report.get("base_manifest_sha256") != base_hashes["manifest.json"]
            or marker.get("base_manifest_sha256") != base_hashes["manifest.json"]
            or records[:len(base)] != base or len(records) <= len(base)):
        raise ValueError("Collection omitted or changed the frozen cleared base")
    if any(r.get("decision") not in {"approved", "corrected", "source_validated"}
           or r.get("pool") != "commercial" or r.get("split") not in {"train", "val"} for r in base):
        raise ValueError("Collection base contains unapproved truth")
    for record in base:
        if cache.get(record["image"]) != base_cache.get(record["image"]):
            raise ValueError("Collection changed cleared base lane targets")
        for path in _sample_paths(record):
            if hashes.get(path.as_posix()) != base_hashes.get(path.as_posix()):
                raise ValueError("Collection changed cleared base image/annotation bytes")
    reserved = _read(snapshot / "reserved_test_ids.json")
    if reserved != _read(snapshot / "base_reserved_test_ids.json"):
        raise ValueError("Collection changed frozen reserved test IDs")
    inventory = _inventory(_read(snapshot / "split_inventory.json"))
    for row in base:
        registered = inventory.get(row["image_id"])
        if registered is None or registered["split"] != row["split"] or _session(registered) != _session(row):
            raise ValueError("Cleared base disagrees with frozen split inventory")
    if any(identifier not in inventory or inventory[identifier]["split"] != "test" for identifier in reserved):
        raise ValueError("Collection reserved test provenance is incomplete")
    protected = [r for r in inventory.values() if r["split"] in {"val", "test"}] + [r for r in base if r["split"] == "val"]
    held_ids = {r["image_id"] for r in protected} | set(reserved)
    held_sessions = {_session(r) for r in protected}
    held_hashes = {r.get("source_sha256") or r.get("sha256") for r in protected} - {None, ""}
    model_records, teacher_hash = [], None
    if use_model:
        model_files = {"model_evidence/" + name for name in (
            "manifest.json", "culane_anno_cache.json", "experiment.json", "teacher_provenance.json",
            "split_inventory.json", "hashes.json")}
        if not model_files <= set(hashes):
            raise ValueError("Model addition evidence is missing")
        model_manifest = _read(snapshot / "model_evidence/manifest.json")
        model_hashes = _read(snapshot / "model_evidence/hashes.json")
        model_cache = _read(snapshot / "model_evidence/culane_anno_cache.json")
        model_report = _read(snapshot / "model_evidence/experiment.json")
        evidence = report.get("model_evidence") or {}
        if (model_report.get("format") != "ufld_teacher_experiment_v1"
                or model_report.get("experimental") is not True or model_report.get("auto_promote") is not False
                or model_manifest[:len(base)] != base
                or evidence.get("manifest_sha256") != hashes["model_evidence/manifest.json"]
                or model_report.get("base_manifest_sha256") != base_hashes["manifest.json"]):
            raise ValueError("Model addition snapshot changed its cleared base")
        teacher_hash = _digest(_teacher_provenance(_read(snapshot / "model_evidence/teacher_provenance.json"), protected))
        if teacher_hash != model_report.get("teacher_provenance_sha256"):
            raise ValueError("Model training ancestry evidence changed")
        model_records = model_manifest[len(base):]
        if records[len(base):len(base) + len(model_records)] != model_records:
            raise ValueError("Collection model additions are not exact collection-2 copies")
        if len(model_records) != evidence.get("extra_train_images"):
            raise ValueError("Model addition count changed")
        if len(model_records) > int(sum(r["split"] == "train" for r in base) * model_report["max_extra_ratio"]):
            raise ValueError("Model additions exceed their frozen teacher experiment ratio")
        for record in model_records:
            if cache.get(record["image"]) != model_cache.get(record["image"]):
                raise ValueError("Collection model lane targets changed")
            for path in _sample_paths(record):
                if hashes.get(path.as_posix()) != model_hashes.get(path.as_posix()):
                    raise ValueError("Collection model image/annotation bytes changed")
    native = sim_records = None
    if use_sim:
        simulation_files = {"simulation_import/" + name for name in (
            "import.json", "plan.json", "manifest.jsonl", "upstream/source_train.json",
            "upstream/source_val.json", "provenance/provenance.json",
            "provenance/LICENSE-Apache-2.0.txt", "provenance/publisher-page.html", "provenance/NOTICE.txt")}
        if not simulation_files <= set(hashes):
            raise ValueError("Frozen simulation import provenance is incomplete")
        sim_meta, sim_records, native = _source_simulation(snapshot / "simulation_import")
        if sim_meta["manifest_sha256"] != report.get("simulated_manifest_sha256"):
            raise ValueError("Frozen simulation import identity changed")
        sim_records = {r["image_id"]: r for r in sim_records}
        held_hashes.update(_source_hash(row) for row in sim_records.values() if row["split"] == "val")
    names, ids, source_hashes = set(), set(), set()
    for index, record in enumerate(records):
        identifier = _identity(record)
        if identifier in ids or record["image"] in names or identifier in reserved:
            raise ValueError("Duplicate or reserved-test collection record")
        ids.add(identifier); names.add(record["image"])
        for path in _sample_paths(record):
            if path.as_posix() not in hashes:
                raise ValueError("Collection sample lacks immutable fingerprints")
        if index >= len(base):
            if (record["split"] != "train" or identifier in held_ids or _session(record) in held_sessions
                    or _source_hash(record) in held_hashes or _source_hash(record) in source_hashes):
                raise ValueError("Experimental addition overlaps existing or held-out data")
            if record.get("pool") != "commercial" or record.get("experimental") is not True or record.get("training_weight") != 1.0:
                raise ValueError("Experimental collection provenance changed")
            if index < len(base) + len(model_records):
                registered = inventory.get(identifier)
                if (record.get("label_origin") != PSEUDO_ORIGIN or record.get("decision") != PSEUDO_DECISION
                        or record.get("teacher_provenance_sha256") != teacher_hash
                        or registered is None or registered.get("split") != "train"
                        or _session(record) != _session(registered) or _source_hash(record) != _source_hash(registered)):
                    raise ValueError("Model proposal truth provenance is invalid")
                original = record.get("original_metadata") or {}
                if registered.get("pool") != "commercial" or any(
                        r.get("review_status") in {"approved", "corrected", "rejected"}
                        or r.get("decision") in {"approved", "corrected", "rejected", "source_validated"}
                        or r.get("locked") in (True, 1, "1") for r in (original, registered)):
                    raise ValueError("Model proposal replaced protected human truth")
                copy_provenance = record.get("copy_provenance")
                if record.get("input_image_sha256") != _source_hash(record):
                    if (not isinstance(copy_provenance, dict)
                            or copy_provenance.get("transform") != "full_resolution_jpeg_q97"
                            or copy_provenance.get("original_sha256") != _source_hash(record)
                            or copy_provenance.get("copy_sha256") != record.get("input_image_sha256")
                            or any(copy_provenance.get(key) != registered.get(key) for key in ("width", "height"))):
                        raise ValueError("Model proposal derived-image provenance is incomplete")
                if int(record.get("lanes", 0)) < 1:
                    raise ValueError("Model absence is not a verified negative")
            else:
                original = sim_records.get(identifier) if use_sim else None
                if (not original or original["split"] != "train" or original["qc_reasons"]
                        or record.get("original_metadata") != original
                        or record.get("label_origin") != SIM_ORIGIN or record.get("decision") != SIM_DECISION
                        or record.get("provisional") is not True or record.get("unaudited") is not True
                        or record.get("audit_status") != "not_consulted" or record.get("license") != "Apache-2.0"):
                    raise ValueError("Simulation additions must remain unaudited native TRAIN geometry")
                evidence = record.get("native_evidence") or {}
                if any(evidence.get(key) not in hashes for key in ("image", "geometry", "source_label")):
                    raise ValueError("Native simulation byte evidence is incomplete")
                if any(record.get(key) != evidence.get(key) for key in ("geometry", "source_label")):
                    raise ValueError("Simulation label references do not match frozen native evidence")
                native_record = {**original, **{key: evidence[key] for key in evidence}}
                _, _, geometry = _verify_native(snapshot, native_record, native[identifier])
                if record.get("export_geometry_sha256") != _digest(geometry):
                    raise ValueError("Simulation export geometry fingerprint changed")
                expected_rows = [[] for _ in range(4)]
                from .geometry import sample_rows
                from .export_ufld import ROWS_LINES
                for lane in geometry["lanes"]:
                    points = sample_rows(lane, make_crop({}, 1280, 720, WIDTH, HEIGHT), ROWS_LINES)
                    if len(points) >= 2:
                        expected_rows[lane["slot"] - 1] = points
                if cache.get(record["image"]) != cache_entry(expected_rows):
                    raise ValueError("Simulation targets changed or extrapolate native geometry")
        source_hashes.add(record.get("source_sha256"))
        _verify_sample(snapshot, record, cache.get(record["image"], []))
    if set(cache) != names or marker.get("manifest_sha256") != hashes["manifest.json"]:
        raise ValueError("Collection manifest/cache identity mismatch")
    origins = dict(Counter(r["label_origin"] for r in records[len(base):]))
    expected = {"base_train_images": sum(r["split"] == "train" for r in base),
                "base_val_images": sum(r["split"] == "val" for r in base),
                "extra_train_images": len(records) - len(base), "extra_origin_counts": origins,
                "model_train_images": origins.get(PSEUDO_ORIGIN, 0),
                "simulated_train_images": origins.get(SIM_ORIGIN, 0),
                "train_images": sum(r["split"] == "train" for r in records),
                "val_images": sum(r["split"] == "val" for r in records), "test_images_used": 0}
    if any(report.get(key) != value for key, value in expected.items()):
        raise ValueError("Collection split/provenance counts disagree with immutable records")
    if (use_model and not model_records) or (use_sim and not origins.get(SIM_ORIGIN)):
        raise ValueError("Collection is missing a requested experimental source")
    return snapshot, records, marker
