"""Freeze representative repair audits without changing the College import."""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import json
from pathlib import Path
import shutil
from itertools import combinations
from bisect import bisect_right

from .bootstrap import atomic_json
from .college_group_clearance import has_observed_training_span, valid_clearances
from .college_repair_review import COLLECTION, CollegeRepairReview, SOURCE
from .import_college_station import temporal_key
from .real_source_review import RealSourceReview
from .simulation_review import _digest, _inside


def observed_slot_order_ok(geometry):
    """Check lane order only where both boundaries have observed sections."""
    def runs(lane):
        result = []
        for section in lane.get("segments", [lane["points"]]):
            points = section.get("points") if isinstance(section, dict) else section
            visible = []
            for point in points:
                if len(point) > 2 and point[2] is False:
                    if len(visible) > 1:
                        result.append(visible)
                    visible = []
                else:
                    visible.append(point)
            if len(visible) > 1:
                result.append(visible)
        return result

    prepared = []
    for lane in sorted(geometry["lanes"], key=lambda lane: lane["slot"]):
        sections = []
        for section in runs(lane):
            points = sorted(section, key=lambda point: point[1])
            ys = [point[1] for point in points]
            if any(a >= b for a, b in zip(ys, ys[1:])):
                return False
            sections.append((points, ys))
        prepared.append(sections)

    def x_at(points, ys, y):
        index = max(0, min(len(points) - 2, bisect_right(ys, y) - 1))
        a, b = points[index], points[index + 1]
        return a[0] + (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1])

    for left, right in combinations(prepared, 2):
        for a, ay in left:
            for b, by in right:
                first = max(ay[0], by[0])
                last = min(ay[-1], by[-1])
                if first >= last:
                    continue
                rows = {first, last} | {point[1] for point in a + b if first < point[1] < last}
                if any(x_at(a, ay, y) >= x_at(b, by, y) for y in rows):
                    return False
    return True


def stage_batch(data_root, candidates, *, original_held_count=3833, report=None):
    """Publish a new immutable candidate cohort. Never replace reviewed audits."""
    original = RealSourceReview(data_root, SOURCE)
    _, originals, _, manifest_sha, audits_sha = original.snapshot(fresh=True)
    root = Path(data_root).resolve() / "experiments" / COLLECTION
    if (root / "manifest.jsonl").exists() or (root / "review.sqlite").exists():
        raise ValueError("A repair audit batch is already published; preserve its reviews")
    source_snapshot = {"manifest_sha256": manifest_sha, "audits_sha256": audits_sha,
                       "import_sha256": _digest((original.root / "import.json").read_bytes())}
    decisions_before, corrections_before = original.decisions(), original.corrections()
    _, _, source_groups, _, _ = original.snapshot()
    prior_clearances = valid_clearances(original, originals, source_groups, manifest_sha, audits_sha,
                                       decisions_before, corrections_before)
    clearance = original.status()["counts"]
    if clearance["held_uncleared"] != original_held_count:
        raise ValueError("Original held count changed; refresh candidate membership before staging")
    # Freeze every candidate before creating any reviewable manifest.
    selected, seen = [], set()
    for candidate in candidates:
        image_id = candidate["image_id"]
        row = originals.get(image_id)
        if image_id in seen or not row or row["split"] != "hold" or row.get("intended_split") != "train":
            raise ValueError("Repair audit must contain unique original held TRAIN candidates")
        if image_id in decisions_before or image_id in corrections_before:
            raise ValueError("Preserve personal review history instead of staging it again")
        if original._group_held_eligible(row, prior_clearances, decisions_before, corrections_before):
            raise ValueError("An already cleared cohort photo must not be staged again")
        geometry = deepcopy(candidate["geometry"])
        if geometry.get("image_id", image_id) != image_id:
            raise ValueError("Candidate geometry refers to another photo")
        geometry["image_id"] = image_id
        origin = candidate["candidate_origin"]
        if origin not in {"source_repair", "model_proposal"}:
            raise ValueError("Repair evidence must distinguish source repairs and model proposals")
        source_geometry = original._verified(row, geometry=True)
        if (geometry["width"], geometry["height"]) != (source_geometry["width"], source_geometry["height"]):
            raise ValueError("Candidate dimensions must match the original source photo")
        original._validate_lanes(geometry["lanes"], geometry["width"], geometry["height"], source_metadata=True)
        audit_mode = candidate.get("audit_mode", "ready")
        if audit_mode not in {"ready", "needs_repair"}:
            raise ValueError("Unknown repair audit mode")
        if audit_mode == "ready" and not has_observed_training_span(geometry):
            raise ValueError("A candidate with no observed training rows must remain held")
        if audit_mode == "ready" and not observed_slot_order_ok(geometry):
            raise ValueError("Candidate lane slots cross or reverse on observed paint rows")
        geometry.update(candidate_only=True, training_eligible=False, human_complete_annotation=False)
        geometry.pop("audit_resolved_qc_reasons", None)
        selected.append((row, candidate, geometry))
        seen.add(image_id)
    if not selected:
        raise ValueError("No usable repair candidates are ready for sample audits")
    root.mkdir(parents=True, exist_ok=True)
    buckets = defaultdict(list)
    for row, candidate, geometry in selected:
        # Keep source sequence, warning pattern and proposal recipe separate.
        key = (candidate["candidate_origin"], row.get("sequence_id", row["session_id"]),
               tuple(row["qc_reasons"]), candidate.get("repair_family", "visible_paint"),
               candidate.get("audit_mode", "ready"))
        buckets[key].append((row, candidate, geometry))
    rows, groups = [], []
    for index, (key, members) in enumerate(sorted(buckets.items())):
        members.sort(key=lambda entry: temporal_key(entry[0]))
        for part in range(0, len(members), 100):
            chunk = members[part:part + 100]
            group_key = f"college-repair:{index:03d}:{part // 100:03d}"
            ids = []
            for original_row, candidate, geometry in chunk:
                image_id = original_row["image_id"]
                row = deepcopy(original_row)
                for field, folder in (("image", "images"), ("source_label", "native"),
                                      ("geometry", "original_geometry")):
                    source = _inside(original.root, original_row[field])
                    relative = f"{folder}/{image_id}{source.suffix}"
                    target = _inside(root, relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    if _digest(target.read_bytes()) != original_row[{"image": "source_sha256", "source_label": "source_label_sha256", "geometry": "geometry_sha256"}[field]]:
                        raise ValueError("Original evidence changed while copying")
                    row["original_geometry" if field == "geometry" else field] = relative
                proposal_path = f"geometry/{image_id}.json"
                atomic_json(_inside(root, proposal_path), geometry)
                mode = candidate.get("audit_mode", "ready")
                blockers = list(dict.fromkeys(["repair_review_required", *candidate.get("blockers", [])]))
                row.update(split="train", original_split="hold", group_key=group_key,
                           qc_reasons=[] if mode == "ready" else blockers, audit_mode=mode,
                           original_qc_reasons=list(original_row["qc_reasons"]),
                           original_geometry_sha256=original_row["geometry_sha256"], geometry=proposal_path,
                           geometry_sha256=_digest(_inside(root, proposal_path).read_bytes()),
                           candidate_origin=candidate["candidate_origin"],
                           repair_description=candidate.get("repair_description", "Check every visible painted boundary and its lane slot."),
                           annotation_policy_risks=["fresh_repair_audit_required", *original_row["qc_reasons"]],
                           evidence_files=deepcopy(candidate.get("evidence_files", [])),
                           repair_evidence=candidate.get("provenance", {}), source_snapshot=source_snapshot)
                rows.append(row)
                ids.append(image_id)
            positions = [0, len(ids) // 2, len(ids) - 1] if len(ids) >= 3 else list(range(len(ids)))
            groups.append({"group_key": group_key, "split": "train", "count": len(ids),
                           "sample_ids": [ids[position] for position in positions],
                           "candidate_origin": key[0], "original_qc_reasons": list(key[2])})
    raw = b"".join((json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode() for row in rows)
    (root / "manifest.jsonl").write_bytes(raw)
    atomic_json(root / "audits.json", {"schema": "real_lane_audits:v1", "source": SOURCE,
                "manifest_sha256": _digest(raw), "groups": groups})
    metadata = {"schema": "real_lane_import:v1", "source": SOURCE, "license": "CC0-1.0",
                "collection": COLLECTION, "state": "awaiting_audit", "candidate_only": True,
                "training_eligible": False, "auto_promote": False, "source_snapshot": source_snapshot,
                "original_held_count": original_held_count, "manifest_sha256": _digest(raw),
                "audits_sha256": _digest((root / "audits.json").read_bytes()), "preparation_report": report or {}}
    if original.decisions() != decisions_before or original.corrections() != corrections_before:
        raise ValueError("Original reviews changed during preparation; recheck the staged batch")
    atomic_json(root / "import.json", metadata)
    result = CollegeRepairReview(data_root).status()
    if result["counts"]["approved"] or (result["counts"]["pending"] + result["counts"]["pending_problem_samples"]
                                        != sum(len(g["sample_ids"]) for g in groups)):
        raise ValueError("Fresh repair batch must contain only pending real audit decisions")
    receipt = {"collection": COLLECTION, "staged_photos": len(rows), "groups": len(groups),
               "samples": result["counts"]["pending"] + result["counts"]["pending_problem_samples"],
               "ready_samples": result["counts"]["pending"],
               "problem_samples": result["counts"]["pending_problem_samples"],
               "ready_photos": result["counts"]["ready_candidate_photos"],
               "problem_photos": result["counts"]["problem_photos"],
               "candidate_origins": dict(Counter(row["candidate_origin"] for row in rows)),
               "source_snapshot": source_snapshot, "original_reviews_unchanged": True,
               "human_decisions_created": 0, "training_started": False, "training_held": True}
    atomic_json(root / "staging-receipt.json", receipt)
    return receipt
