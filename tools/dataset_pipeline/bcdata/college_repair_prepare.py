"""Combine completed repair diagnostics into fresh, isolated group audits."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from .bootstrap import atomic_json
from .college_group_clearance import has_observed_training_span
from .college_repair_batch import observed_slot_order_ok, stage_batch
from .college_repair_review import COLLECTION, SOURCE
from .real_source_review import RealSourceReview
from .simulation_review import _digest, _inside


def prepare(data_root, ordinary_report, proposal_directory):
    data_root = Path(data_root).resolve()
    ordinary_report, proposal_directory = Path(ordinary_report).resolve(), Path(proposal_directory).resolve()
    root = data_root / "experiments" / COLLECTION
    proposal_relative = proposal_directory.relative_to(root).as_posix()
    store = RealSourceReview(data_root, SOURCE)
    _, originals, _, manifest_sha, audits_sha = store.snapshot(fresh=True)
    index_raw = (proposal_directory / "proposal-index.json").read_bytes()
    index = json.loads(index_raw)
    if (index.get("state") != "complete" or index.get("failed") or index.get("total") != index.get("done")
            or len(index.get("rows", [])) != index.get("done")
            or index.get("source_manifest_sha256") != manifest_sha or index.get("source_audits_sha256") != audits_sha
            or index.get("candidate_only") is not True or index.get("training_eligible") is not False):
        raise ValueError("Complete, source-bound candidate generation is required before freezing audits")
    ordinary_raw = ordinary_report.read_bytes()
    ordinary = [json.loads(line) for line in ordinary_raw.splitlines() if line.strip()]
    candidates, seen = [], set()
    for row in ordinary:
        image_id = row["image_id"]
        original = originals[image_id]
        provenance = row["provenance"]
        if (provenance["source_sha256"] != original["source_sha256"]
                or provenance["native_sha256"] != original["source_label_sha256"]
                or provenance["source_geometry_sha256"] != original["geometry_sha256"]):
            raise ValueError("Ordinary repair evidence no longer matches its source")
        geometry = row["geometry"]
        blockers = list(row["diagnostics"]["blockers"])
        ready = row["candidate_status"] == "ready_for_audit"
        if ready and blockers:
            raise ValueError("An ordinary candidate with repair blockers cannot be staged as ready")
        candidates.append({"image_id": image_id, "geometry": geometry, "candidate_origin": "source_repair",
            "audit_mode": "ready" if ready else "needs_repair", "blockers": blockers,
            "repair_family": row["batch_key"],
            "repair_description": ("Recheck the flagged original traces and lane slots. The source coordinates have not been extended."
                if ready else "This source trace is still ambiguous or has no usable training span. " + ", ".join(blockers).replace("_", " ") + "."),
            "provenance": provenance})
        if image_id in seen:
            raise ValueError("Duplicate ordinary repair photo")
        seen.add(image_id)
    for entry in index["rows"]:
        image_id = entry["image_id"]
        if image_id in seen or entry.get("diagnostic_only"):
            raise ValueError("Do not restage duplicates or previously approved diagnostic controls")
        original = originals[image_id]
        raw = _inside(proposal_directory, entry["proposal"]).read_bytes()
        if _digest(raw) != entry["sha256"]:
            raise ValueError("A frozen model proposal changed")
        geometry = json.loads(raw)
        if (geometry["source_receipt"] != original or geometry.get("candidate_only") is not True
                or geometry.get("diagnostic_only") is True):
            raise ValueError("A model proposal lost its exact original source receipt")
        evidence = [{"path": proposal_relative + "/" + entry["proposal"], "sha256": entry["sha256"]}]
        for asset in geometry["evidence"].values():
            path = _inside(proposal_directory, asset["path"])
            if _digest(path.read_bytes()) != asset["sha256"]:
                raise ValueError("Proposal probability or comparison evidence changed")
            evidence.append({"path": proposal_relative + "/" + asset["path"], "sha256": asset["sha256"]})
        blockers = list(entry.get("repair_qc_reasons", []))
        ready = entry.get("repair_disposition") == "ready_for_audit"
        if (geometry.get("repair_disposition") != entry.get("repair_disposition")
                or geometry.get("repair_qc_reasons", []) != blockers):
            raise ValueError("Proposal and batch index disagree about their repair gate")
        if ready and blockers:
            raise ValueError("A blocked repair candidate cannot be staged as ready")
        if ready and not has_observed_training_span(geometry):
            ready = False
            blockers.append("no_observed_training_span")
        if ready and not observed_slot_order_ok(geometry):
            ready = False
            blockers.append("observed_slot_order_contradiction")
        slots = ",".join(str(lane["slot"]) for lane in geometry["lanes"])
        family = "adapted-observed-paint:slots-" + slots
        if not ready:
            # Problem groups cannot be approved. Keep the main failure family
            # together instead of asking for a new audit for every combination.
            if any("order" in reason or "cross" in reason or "duplicate" in reason for reason in blockers):
                family = "problem:ambiguous_lane_order"
            elif any("unassigned" in reason for reason in blockers):
                family = "problem:unassigned_paint"
            elif any("branch" in reason or "four" in reason for reason in blockers):
                family = "problem:branch_or_extra_boundary"
            else:
                family = "problem:incomplete_observed_paint"
        candidates.append({"image_id": image_id, "geometry": geometry, "candidate_origin": "model_proposal",
            "audit_mode": "ready" if ready else "needs_repair", "blockers": blockers,
            "repair_family": family, "evidence_files": evidence,
            "repair_description": ("Check the proposed missing paint, visible gaps and all lane slots against the photo."
                if ready else "This proposal still has ambiguous or incomplete geometry: " + ", ".join(blockers).replace("_", " ") + "."),
            "provenance": {"proposal_path": proposal_relative + "/" + entry["proposal"],
                "proposal_sha256": entry["sha256"], "model_receipt": index["model_receipt"],
                "evidence_files": evidence, "original_source_geometry_sha256": original["geometry_sha256"],
                "native_warnings_preserved": True, "human_decisions_created": 0}})
        seen.add(image_id)
    held_count = store.status()["counts"]["held_uncleared"]
    if len(candidates) != held_count:
        raise ValueError(f"Every remaining held photo needs a disposition: got {len(candidates)}, expected {held_count}")
    report = {"ordinary_report_sha256": _digest(ordinary_raw), "model_index_sha256": _digest(index_raw),
              "ordinary_photos": len(ordinary), "model_photos": len(index["rows"]),
              "audit_modes": dict(Counter(candidate["audit_mode"] for candidate in candidates)),
              "original_holds_accounted_for": held_count, "training_started": False}
    atomic_json(root / "audit-preparation-report.json", report)
    return stage_batch(data_root, candidates, original_held_count=held_count, report=report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--ordinary-report", type=Path, required=True)
    parser.add_argument("--proposal-directory", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.data_root, args.ordinary_report, args.proposal_directory), indent=2))


if __name__ == "__main__":
    main()
