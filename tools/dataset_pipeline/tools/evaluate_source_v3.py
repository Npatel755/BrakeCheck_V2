"""Read-only development comparison of V3 against actual source audit reviews.

Only report artifacts are written. The database is opened in SQLite read-only
mode, and neither reviews nor source/geometry annotation files are modified.
These samples informed converter development; results are not held-out accuracy.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

import numpy as np

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_ROOT))

from bcdata.teachers.source_labels import load_classes
from bcdata.source_boundary_maps import load_boundary_maps
from tools.evaluate_source_repair import score


METRIC = "Same-slot 20px raster IoU >= 0.5 against immutable actual human reviews"
LIMITATION = "Converter development check on previously inspected audit samples; not held-out accuracy or an official UFLDv2 benchmark"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def points(lane):
    return np.asarray([p[:2] for p in lane.get("points", []) if len(p) < 3 or p[2]], dtype=float).reshape(-1, 2)


def slotted(geometry):
    return {lane["slot"]: lane for lane in geometry.get("lanes", []) if lane.get("slot") in (1, 2, 3, 4)}


def support(geometry, paint):
    """Observed centers supported by paint within four horizontal native pixels."""
    h, w = paint.shape
    per_slot = {}
    for slot, lane in slotted(geometry).items():
        observed = points(lane)
        hits = 0
        for x, y in observed:
            if not np.isfinite((x, y)).all():
                continue
            xi, yi = round(float(x)), round(float(y))
            if 0 <= yi < h and 0 <= xi < w:
                hits += bool(paint[yi, max(0, xi - 4):min(w, xi + 5)].max() >= .5)
        per_slot[str(slot)] = {"supported": int(hits), "observed": len(observed),
                               "fraction": hits / max(1, len(observed))}
    hits = sum(v["supported"] for v in per_slot.values())
    total = sum(v["observed"] for v in per_slot.values())
    return {"supported": hits, "observed": total,
            "fraction": hits / total if total else None, "by_slot": per_slot}


def observed_extents(before, after):
    """Track retained y span for each previously slotted observed boundary."""
    result = {}
    replacements = slotted(after)
    for slot, lane in slotted(before).items():
        a = points(lane)
        b = points(replacements[slot]) if slot in replacements else np.empty((0, 2))
        if not len(a):
            continue
        old = [float(a[:, 1].min()), float(a[:, 1].max())]
        new = [float(b[:, 1].min()), float(b[:, 1].max())] if len(b) else None
        overlap = max(0., min(old[1], new[1]) - max(old[0], new[0])) if new else 0.
        result[str(slot)] = {"old_y": old, "new_y": new,
                            "retained_span_fraction": overlap / max(1., old[1] - old[0]),
                            "preserved_with_2px_tolerance": bool(new and new[0] <= old[0] + 2 and new[1] >= old[1] - 2)}
    return result


def totals(records):
    output = {"images": len(records), "decisions": dict(Counter(r["decision"] for r in records))}
    for version in ("old", "v3"):
        tp, fp, fn = (sum(r[version][key] for r in records) for key in ("tp", "fp", "fn"))
        support_rows = [r["source_paint_support"][version] for r in records]
        observed = sum(r["observed"] for r in support_rows)
        support_means = [r["fraction"] for r in support_rows if r["fraction"] is not None]
        output[version] = {
            "tp": tp, "fp": fp, "fn": fn, "f1": 2 * tp / max(1, 2 * tp + fp + fn),
            "precision": tp / max(1, tp + fp), "recall": tp / max(1, tp + fn),
            "count_matches": sum(r[version]["count_match"] for r in records),
            "source_paint_support_mean_per_image": float(np.mean(support_means)) if support_means else None,
            "source_paint_support_all_observed_points": sum(r["supported"] for r in support_rows) / observed if observed else None,
        }
    extents = [e for r in records for e in r["observed_extents"].values()]
    output["preserved_observed_extents"] = {
        "previous_boundaries": len(extents),
        "preserved_with_2px_tolerance": sum(e["preserved_with_2px_tolerance"] for e in extents),
        "mean_retained_span_fraction": float(np.mean([e["retained_span_fraction"] for e in extents])) if extents else None,
    }
    return output


def evaluate(data_root: Path, output: Path):
    # Delayed import lets the V3 author explicitly mark the implementation ready.
    from bcdata.source_converter_v3 import VERSION, repair_geometry_v3, risk_diagnostics

    code_path = PIPELINE_ROOT / "bcdata" / "source_converter_v3.py"
    code_sha = sha256(code_path)
    dependencies = {str(p): sha256(p) for p in (PIPELINE_ROOT / "bcdata" / "geometry.py",
                                               PIPELINE_ROOT / "bcdata" / "source_boundary_maps.py")}
    evaluator_sha = sha256(__file__)
    database = data_root / "metadata.sqlite"
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    records, skipped, seen, checked_formats = [], [], set(), set()
    output.mkdir(parents=True, exist_ok=True)
    geometry_output = output / "geometry"
    geometry_output.mkdir(exist_ok=True)
    try:
        groups = connection.execute("SELECT * FROM source_validation_groups ORDER BY group_key").fetchall()
        for group in groups:
            phase = "repair" if group["batch_id"].startswith("comma-source-repair-audit") else "original"
            for image_id in json.loads(group["sample_ids"]):
                if image_id in seen:
                    raise ValueError("Audit sample appears in multiple groups: " + image_id)
                seen.add(image_id)
                row = connection.execute("SELECT * FROM images WHERE image_id=?", (image_id,)).fetchone()
                if row is None:
                    raise ValueError("Missing audit sample image: " + image_id)
                if row["review_status"] not in ("approved", "corrected") or not row["locked"]:
                    skipped.append({"image_id": image_id, "decision": row["review_status"], "reason": "not_accepted_human_review"})
                    continue
                check = connection.execute("SELECT * FROM source_conversion_checks WHERE image_id=?", (image_id,)).fetchone()
                if check is None or check["group_key"] != group["group_key"]:
                    raise ValueError("Sample immutable conversion check no longer matches group: " + image_id)
                human_annotation = connection.execute("SELECT * FROM annotations WHERE image_id=? AND kind='human' ORDER BY created_at DESC,id DESC LIMIT 1", (image_id,)).fetchone()
                source_annotation = connection.execute("SELECT * FROM annotations WHERE id=? AND kind='source_label'", (check["source_id"],)).fetchone()
                if human_annotation is None or source_annotation is None:
                    raise ValueError("Missing bound source or human annotation: " + image_id)
                source_path, geometry_path, human_path = (Path(source_annotation["path"]), Path(check["geometry_path"]), Path(human_annotation["path"]))
                source_sha, geometry_sha, human_sha = (sha256(p) for p in (source_path, geometry_path, human_path))
                if source_sha != check["source_sha256"] or geometry_sha != check["geometry_sha256"]:
                    raise ValueError("Immutable source/check fingerprint changed: " + image_id)
                original = json.loads(geometry_path.read_text(encoding="utf-8"))
                human = json.loads(human_path.read_text(encoding="utf-8"))
                masks = load_boundary_maps(source_annotation)
                paint = masks["lane"]
                if paint.shape != (original["height"], original["width"]):
                    raise ValueError("Source class map must retain native shape: " + image_id)
                if (human["height"], human["width"]) != paint.shape:
                    raise ValueError("Human annotation must retain native shape: " + image_id)
                # Cross-check the optimized RGB loader against the complete
                # shared taxonomy once per source format in this run.
                source_format = json.loads(source_annotation["params"] or "{}").get("format")
                if source_format not in checked_formats:
                    classes = load_classes(source_annotation)
                    if not all(np.array_equal(masks[name] >= .5, classes == cls)
                               for name, cls in (("lane", 2), ("my_car", 5), ("movable", 4))):
                        raise ValueError("Boundary loader differs from shared taxonomy: " + source_format)
                    checked_formats.add(source_format)
                original_snapshot = canonical_sha256(original)
                risk = risk_diagnostics(original)
                repaired = (repair_geometry_v3(original, paint, my_car=masks["my_car"], movable=masks["movable"])
                            if risk["risky"] else deepcopy(original))
                if canonical_sha256(original) != original_snapshot:
                    raise ValueError("Converter mutated its original geometry input: " + image_id)
                repaired["image_id"] = image_id
                if (repaired["height"], repaired["width"]) != paint.shape:
                    raise ValueError("Converter changed native image dimensions: " + image_id)
                candidate_path = geometry_output / (image_id + ".json")
                candidate_path.write_text(json.dumps(repaired, indent=2, sort_keys=True), encoding="utf-8")
                before_score, after_score = score(original, human), score(repaired, human)
                record = {
                    "image_id": image_id, "decision": row["review_status"], "phase": phase,
                    "group_key": group["group_key"], "stratum": group["stratum"], "split": row["split"],
                    "risk_diagnostics": risk, "reextracted": risk["risky"],
                    "old": before_score, "v3": after_score,
                    "old_slots": sorted(slotted(original)), "v3_slots": sorted(slotted(repaired)),
                    "human_slots": sorted(slotted(human)),
                    "source_paint_support": {"old": support(original, paint), "v3": support(repaired, paint)},
                    "observed_extents": observed_extents(original, repaired),
                    "source_converter": repaired.get("source_converter", {}),
                    "fingerprints": {"geometry_path": str(geometry_path), "geometry_sha256": geometry_sha,
                                     "source_path": str(source_path), "source_sha256": source_sha,
                                     "human_path": str(human_path), "human_sha256": human_sha,
                                     "human_annotation_id": human_annotation["id"], "source_annotation_id": source_annotation["id"],
                                     "candidate_path": str(candidate_path), "candidate_sha256": sha256(candidate_path)},
                }
                records.append(record)
    finally:
        connection.rollback()
        connection.close()
    if sha256(code_path) != code_sha or any(sha256(p) != value for p, value in dependencies.items()):
        raise ValueError("Converter module changed during evaluation; rerun with stable code")
    for record in records:
        fingerprints = record["fingerprints"]
        for kind in ("geometry", "source", "human"):
            if sha256(fingerprints[kind + "_path"]) != fingerprints[kind + "_sha256"]:
                raise ValueError("Immutable annotation changed during evaluation: " + record["image_id"])
    failed_group = lambda r: r["phase"] == "repair" and r["stratum"] == "train:risk-unchanged:source-positive"
    subsets = {
        "all": records,
        "original": [r for r in records if r["phase"] == "original"],
        "repair": [r for r in records if r["phase"] == "repair"],
        "approved": [r for r in records if r["decision"] == "approved"],
        "corrected": [r for r in records if r["decision"] == "corrected"],
        "new_failed_2": [r for r in records if failed_group(r) and r["decision"] == "corrected"],
        "new_approved_42": [r for r in records if failed_group(r) and r["decision"] == "approved"],
    }
    regressions = [
        {"image_id": r["image_id"], "decision": r["decision"], "phase": r["phase"], "old": r["old"], "v3": r["v3"],
         "lost_slots": sorted(set(r["old_slots"]) - set(r["v3_slots"])),
         "shortened_slots": [slot for slot, extent in r["observed_extents"].items() if not extent["preserved_with_2px_tolerance"]]}
        for r in records
        if r["v3"]["tp"] < r["old"]["tp"] or r["v3"]["fp"] > r["old"]["fp"] or r["v3"]["fn"] > r["old"]["fn"]
    ]
    bindings = [{"image_id": r["image_id"], "decision": r["decision"], "group_key": r["group_key"],
                 **r["fingerprints"]} for r in records]
    report = {"metric": METRIC, "limitation": LIMITATION,
              "application_policy": "Re-extract only when risk_diagnostics(original)['risky']; preserve original geometry otherwise. Predicate: slotted lane has gap > 0.08 * image height AND curvature > 0.04 AND at least one adjacent observed component spans fewer than four rows. No human geometry is consumed by converter or risk selection.",
              "source_support_metric": "Observed lane centers within four horizontal native pixels of source paint, excluding points marked unobserved",
              "extent_metric": "Previously slotted observed y span retained within two native pixels; clipping changes are reported, not automatically errors",
              "created_at": datetime.now(timezone.utc).isoformat(), "converter_version": VERSION,
              "converter_code_path": str(code_path), "converter_code_sha256": code_sha,
              "dependency_sha256": dependencies,
              "evaluator_code_sha256": evaluator_sha, "input_manifest_sha256": canonical_sha256(bindings),
              "unique_audit_samples": len(seen), "excluded": skipped,
              "safety_checks": {"database_opened_read_only": True, "database_writes": False,
                                "original_annotations_unchanged": True, "source_fingerprints_verified": True,
                                "converter_and_dependencies_stable": True, "boundary_loader_verified_against_shared_taxonomy": True,
                                "risk_selected_images": sum(r["reextracted"] for r in records),
                                "approved_sample_regressions": sum(r["decision"] == "approved" for r in regressions)},
              "totals": {name: totals(rows) for name, rows in subsets.items()},
              "regressions": regressions, "records": records}
    report_path = output / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    regression_path = output / "regressions.json"
    regression_path.write_text(json.dumps({"metric": METRIC, "limitation": LIMITATION, "regressions": regressions}, indent=2), encoding="utf-8")
    print(json.dumps({"report_path": str(report_path), "report_sha256": sha256(report_path),
                      "converter_code_sha256": code_sha, "totals": report["totals"], "regressions": regressions}))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path(os.environ.get("BRAKECHECK_DATA_ROOT", PIPELINE_ROOT.parent.parent / "BrakeCheck_Dataset")))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    data_root = args.data_root.resolve()
    evaluate(data_root, (args.output or data_root / "reports" / "source_repair_v3_evaluation").resolve())


if __name__ == "__main__":
    main()
