"""Geometry stage: fused (or human) masks -> lane_geometry/<image_id>.json, plus one file per teacher
so reviewers can flip between what each model would have produced."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

from .config import Settings
from .db import Registry
from .fusion import load_fused
from .geometry import GeoParams, extract_lanes
from .teachers.base import load_output
from .source_converter import VERSION as SOURCE_CONVERTER_VERSION, repair_geometry, source_candidate
from .source_converter_v3 import VERSION as SOURCE_CONTINUATION_VERSION, repair_geometry_v3, risk_diagnostics
from .source_converter_v4 import VERSION as SOURCE_BOUNDARIES_VERSION, repair_geometry_v4
from .source_converter_a2d2 import VERSION as A2D2_CONVERTER_VERSION, repair_geometry as repair_geometry_a2d2
from .source_boundary_maps import load_boundary_maps
from .teachers.base import file_sha256

GEOMETRY_CODE_VERSION = ("geo-4:" + SOURCE_CONVERTER_VERSION + ":" + SOURCE_CONTINUATION_VERSION
                         + ":" + SOURCE_BOUNDARIES_VERSION)


def params_for(settings: Settings) -> GeoParams:
    p = GeoParams.from_cfg(settings.get("geometry", {}))
    p.threshold = float(settings.get("fusion.threshold", 0.5))
    return p


def geometry_version(fused_version: str, params: GeoParams, *, source: str | None = None) -> str:
    code = GEOMETRY_CODE_VERSION + (":" + A2D2_CONVERTER_VERSION if source == "a2d2" else "")
    blob = json.dumps([fused_version, dataclasses.asdict(params), code], sort_keys=True)
    return "geometry:" + hashlib.sha1(blob.encode()).hexdigest()[:12]


def human_geometry(registry: Registry, image_id: str) -> dict | None:
    row = registry.image(image_id)
    if row is None or not row["locked"] or row["review_status"] not in ("approved", "corrected"):
        return None
    ann = registry.annotation(image_id, "human")
    if ann is None or not Path(ann["path"]).exists():
        return None
    return json.loads(Path(ann["path"]).read_text())


def current_geometry(registry: Registry, image_id: str) -> tuple[str, dict] | None:
    """Human geometry if reviewed, else the latest automatic geometry. Returns (origin, data)."""
    h = human_geometry(registry, image_id)
    if h is not None:
        return "human", h
    ann = registry.annotation(image_id, "geometry", "geometry")
    if ann is None or not Path(ann["path"]).exists():
        return None
    return "auto", json.loads(Path(ann["path"]).read_text())


def run(settings: Settings, registry: Registry, rows, force: bool = False, per_teacher: bool = True) -> dict:
    params = params_for(settings)
    out_dir = settings.path("lane_geometry")
    counts = {"images": 0, "written": 0, "skipped": 0, "no_fused": 0, "locked": 0, "lanes": 0}
    for r in rows:
        counts["images"] += 1
        image_id = r["image_id"]
        if r["locked"] and human_geometry(registry, image_id) is not None:
            counts["locked"] += 1
            continue
        fused = load_fused(registry, image_id)
        if fused is None:
            counts["no_fused"] += 1
            continue
        record, maps = fused
        version = geometry_version(record["version"], params,
                                   source=r["source"] if record.get("mode") == "source_label" else None)
        if not force and registry.has_annotation(image_id, "geometry", "geometry", version):
            counts["skipped"] += 1
            continue
        geo = extract_lanes(maps["lane"], params, maps.get("my_car"), maps.get("movable"))
        # Repair only trusted source-mask conversions. Model proposals retain
        # their own geometry and never become labels through this path.
        if r["source"] in ("comma10k", "a2d2") and record.get("mode") == "source_label":
            original = geo
            continuation_risk = risk_diagnostics(original)
            if continuation_risk['risky']:
                geo = repair_geometry_v3(original,maps['lane'],maps.get('my_car'),maps.get('movable'))
                if risk_diagnostics(geo)['risky']:
                    geo['source_converter']['warnings'] = ['unresolved_dash_association']
            else:
                geo = repair_geometry(geo, maps["lane"])
            source = registry.annotation(image_id, "source_label")
            if source and (continuation_risk['risky'] or geo.get("source_converter", {}).get("short_recovery_candidates")):
                source_sha = file_sha256(source["path"])
                proposal_input = geo if continuation_risk['risky'] else original
                original_sha = hashlib.sha256(json.dumps(proposal_input, sort_keys=True).encode()).hexdigest()
                candidate = source_candidate(proposal_input, maps["lane"], source_sha, original_sha)
                atomic_path = out_dir / "teachers" / "source_refined" / (image_id + ".json")
                atomic_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_path.write_text(json.dumps(candidate))
            geo = repair_geometry_v4(geo, maps["lane"], maps.get("my_car"), maps.get("movable"))
            if r["source"] == "a2d2" and source is not None:
                original_maps = load_boundary_maps(source)
                geo = repair_geometry_a2d2(geo, original_maps["lane"], original_maps.get("my_car"),
                                          original_maps.get("movable"), params=params,
                                          solid_mask=original_maps.get("solid"), dashed_mask=original_maps.get("dashed"))
        geo.update(image_id=image_id, version=version, fused_version=record["version"], mode=record.get("mode"),
                   fusion_metrics=record.get("metrics", {}), teacher_vs_source=record.get("teacher_vs_source"))
        path = out_dir / f"{image_id}.json"
        path.write_text(json.dumps(geo, indent=1))
        lic = json.loads(registry.annotation(image_id, "fused", "fusion")["license_ids"] or "[]")
        registry.add_annotation(image_id, "geometry", "geometry", version, str(path), license_ids=lic,
                                params={"lanes": len(geo["lanes"])})
        registry.merge_tags(image_id, geo["tags"], origin="geometry")
        counts["written"] += 1
        counts["lanes"] += sum(1 for l in geo["lanes"] if l.get("slot"))
        if per_teacher:
            for name in record.get("teacher_versions", {}):
                if name == "source_labels":
                    continue
                ann = registry.annotation(image_id, "teacher", name)
                if ann is None or not Path(ann["path"]).exists():
                    continue
                out = load_output(ann["path"])
                if out.lane is None:
                    continue
                tg = extract_lanes(out.lane, params, maps.get("my_car"), maps.get("movable"))
                tp = out_dir / "teachers" / name / f"{image_id}.json"
                tp.parent.mkdir(parents=True, exist_ok=True)
                tp.write_text(json.dumps({**tg, "teacher": name, "teacher_version": ann["model_version"]}))
    return counts
