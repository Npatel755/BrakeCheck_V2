"""CVAT Community Edition round trip (format: "CVAT for images 1.1").

Export a review batch as a task package:
  images/            the original frames
  annotations.xml    `lane` polylines (editable, with slot/kind/physical attributes), the fused
                     `lane_mask`, and read-only `pred_<teacher>` polylines so reviewers can toggle models
  labels.json        paste into CVAT's label "Raw" editor when creating the task
Reviewers fix `lane` polylines and add ONE image tag: review_approved, review_rejected or review_difficult.

Import reads CVAT's export back: an approved image whose lanes changed is recorded as "corrected".
Human labels are written once to annotations/reviewed/ and locked; models never overwrite them.
"""
from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from .config import Settings
from .db import Registry
from .fusion import load_fused
from .geometry import lane_xs
from .lanes import current_geometry

TAGS = {"review_approved": "approved", "review_rejected": "rejected", "review_difficult": "difficult"}


def labels_spec(teachers: list[str]) -> list[dict]:
    lane_attrs = [
        {"name": "slot", "mutable": False, "input_type": "select", "default_value": "none", "values": ["none", "1", "2", "3", "4"]},
        {"name": "kind", "mutable": False, "input_type": "select", "default_value": "solid", "values": ["solid", "dashed", "double"]},
        {"name": "physical", "mutable": False, "input_type": "checkbox", "default_value": "true", "values": ["true"]},
    ]
    labels = [{"name": "lane", "type": "polyline", "color": "#ff2a2a", "attributes": lane_attrs},
              {"name": "lane_mask", "type": "mask", "color": "#ffd000", "attributes": []}]
    labels += [{"name": f"pred_{t}", "type": "polyline", "color": c, "attributes": []}
               for t, c in zip(teachers, ["#2aa1ff", "#2aff8e", "#c62aff", "#ff8e2a"])]
    labels += [{"name": t, "type": "tag", "color": "#ffffff", "attributes": []} for t in TAGS]
    return labels


def mask_rle(mask: np.ndarray) -> tuple[str, int, int, int, int] | None:
    """CVAT XML mask encoding: run lengths over the bounding box, row-major, starting with background."""
    ys, xs = np.nonzero(mask)
    if not len(ys):
        return None
    top, left, bottom, right = int(ys.min()), int(xs.min()), int(ys.max()), int(xs.max())
    crop = mask[top:bottom + 1, left:right + 1].astype(np.uint8).flatten()
    runs, current, count = [], 0, 0
    for v in crop:
        if v == current:
            count += 1
        else:
            runs.append(count)
            current, count = v, 1
    runs.append(count)
    return ",".join(map(str, runs)), left, top, right - left + 1, bottom - top + 1


def _points_attr(points) -> str:
    return ";".join(f"{float(x):.2f},{float(y):.2f}" for x, y, *_ in points)


def export_batch(settings: Settings, registry: Registry, batch_dir: Path) -> Path:
    ids = json.loads((batch_dir / "queue.json").read_text())
    pkg = batch_dir / "cvat_task"
    (pkg / "images").mkdir(parents=True, exist_ok=True)
    teachers = [t for t in settings.get("teachers.order", []) if t != "source_labels"]
    root = ET.Element("annotations")
    ET.SubElement(root, "version").text = "1.1"
    sent: dict[str, dict] = {}
    for k, image_id in enumerate(ids):
        row = registry.image(image_id)
        src = Path(row["path"])
        name = f"{image_id}{src.suffix.lower()}"
        dst = pkg / "images" / name
        if not dst.exists():
            shutil.copy2(src, dst)
        img_el = ET.SubElement(root, "image", id=str(k), name=name, width=str(row["width"]), height=str(row["height"]))
        cur = current_geometry(registry, image_id)
        if cur is not None:
            _, geo = cur
            sent[image_id] = geo
            for l in geo["lanes"]:
                if len(l["points"]) < 2:
                    continue
                pl = ET.SubElement(img_el, "polyline", label="lane", source="auto", occluded="0",
                                   points=_points_attr(l["points"]), z_order="1")
                ET.SubElement(pl, "attribute", name="slot").text = str(l.get("slot") or "none")
                ET.SubElement(pl, "attribute", name="kind").text = l.get("kind", "solid")
                ET.SubElement(pl, "attribute", name="physical").text = "true" if l.get("physical", True) else "false"
        fused = load_fused(registry, image_id)
        if fused is not None:
            enc = mask_rle(fused[1]["lane"] >= 0.5)
            if enc:
                rle, left, top, w, h = enc
                ET.SubElement(img_el, "mask", label="lane_mask", source="auto", occluded="0", rle=rle,
                              left=str(left), top=str(top), width=str(w), height=str(h), z_order="0")
        for t in teachers:
            tp = settings.path("lane_geometry", "teachers", t, f"{image_id}.json")
            if tp.exists():
                for l in json.loads(tp.read_text())["lanes"]:
                    if l.get("slot") and len(l["points"]) >= 2:
                        ET.SubElement(img_el, "polyline", label=f"pred_{t}", source="auto", occluded="0",
                                      points=_points_attr(l["points"]), z_order="0")
    ET.ElementTree(root).write(pkg / "annotations.xml", encoding="utf-8", xml_declaration=True)
    (pkg / "labels.json").write_text(json.dumps(labels_spec(teachers), indent=2))
    (pkg / "sent_geometry.json").write_text(json.dumps(sent))
    (pkg / "README.txt").write_text(
        "1. CVAT > Tasks > + > Create a new task. Labels: Raw tab, paste labels.json.\n"
        "2. Select files: everything in images/. Submit.\n"
        "3. Open the task > Actions > Upload annotations > format 'CVAT 1.1' > annotations.xml.\n"
        "4. Review: fix red `lane` polylines (slot attribute 1=left-left 2=left 3=right 4=right-right),\n"
        "   delete wrong ones, draw missing ones. Blue/green `pred_*` lines are model guesses for comparison only.\n"
        "   Add exactly one tag per image: review_approved / review_rejected / review_difficult.\n"
        "5. Actions > Export task dataset > 'CVAT for images 1.1' (no images) and run\n"
        "   bcdata review-import --batch <this batch folder> --xml <exported annotations.xml> --reviewer <name> --minutes <time spent>\n")
    return pkg


def _lane_from_points(points: list[tuple[float, float]], attrs: dict, width: int, height: int) -> dict:
    pts = sorted(points, key=lambda p: -p[1])
    ys = np.array([p[1] for p in pts], float)
    xs = np.array([p[0] for p in pts], float)
    # a reviewer's polyline is used exactly as drawn (piecewise linear between their points)
    order = np.argsort(ys)
    fit = {"basis": "interp", "ys": [float(v) for v in ys[order]], "xs": [float(v) for v in xs[order]]}
    slot = attrs.get("slot", "none")
    return {"fit": fit, "y_top": int(ys.min()), "y_bottom": int(ys.max()),
            "points": [[float(x), int(round(y)), True] for x, y in pts], "segments": 1, "gaps": [],
            "kind": attrs.get("kind", "solid"), "physical": attrs.get("physical", "true") == "true",
            "slot": int(slot) if slot not in ("none", "", None) else None, "flags": [], "origin": "human",
            "polyline_points": [[float(x), float(y)] for x, y in pts]}


def _geometry_shift(sent: dict | None, lanes: list[dict]) -> tuple[int, float]:
    if not sent:
        return len(lanes), float("nan")
    a = {l.get("slot"): l for l in sent["lanes"] if l.get("slot")}
    b = {l.get("slot"): l for l in lanes if l.get("slot")}
    changed, shifts = 0, []
    for slot in set(a) | set(b):
        if slot not in a or slot not in b:
            changed += 1
            continue
        lo, hi = max(a[slot]["y_top"], b[slot]["y_top"]), min(a[slot]["y_bottom"], b[slot]["y_bottom"])
        if hi - lo < 5:
            changed += 1
            continue
        ys = np.arange(lo, hi, 4)
        d = float(np.abs(lane_xs(a[slot], ys) - lane_xs(b[slot], ys)).mean())
        shifts.append(d)
        extent = abs(a[slot]["y_top"] - b[slot]["y_top"]) + abs(a[slot]["y_bottom"] - b[slot]["y_bottom"])
        if d > 2.0 or extent > 15:
            changed += 1
    return changed, (float(np.mean(shifts)) if shifts else 0.0)


def import_review(settings: Settings, registry: Registry, batch_dir: Path, xml_path: Path, reviewer: str,
                  minutes: float | None = None) -> dict:
    sent = json.loads((batch_dir / "cvat_task" / "sent_geometry.json").read_text()) \
        if (batch_dir / "cvat_task" / "sent_geometry.json").exists() else {}
    tree = ET.parse(xml_path)
    images = tree.getroot().findall("image")
    counts = {"images": len(images), "approved": 0, "corrected": 0, "rejected": 0, "difficult": 0, "untagged": 0, "unknown": 0}
    judged = []
    for img in images:
        image_id = Path(img.get("name")).stem
        row = registry.image(image_id)
        if row is None:
            counts["unknown"] += 1
            continue
        tags = [TAGS[t.get("label")] for t in img.findall("tag") if t.get("label") in TAGS]
        if not tags:
            counts["untagged"] += 1
            continue
        decision = "rejected" if "rejected" in tags else "difficult" if "difficult" in tags else "approved"
        lanes = []
        for pl in img.findall("polyline"):
            if pl.get("label") != "lane":
                continue
            pts = [tuple(map(float, p.split(","))) for p in pl.get("points").split(";") if p]
            attrs = {a.get("name"): (a.text or "").strip() for a in pl.findall("attribute")}
            if len(pts) >= 2:
                lanes.append(_lane_from_points(pts, attrs, row["width"], row["height"]))
        changed, shift = _geometry_shift(sent.get(image_id), lanes)
        if decision == "approved" and changed:
            decision = "corrected"
        judged.append((image_id, row, decision, lanes, changed, shift))
    per_image = (minutes * 60.0 / len(judged)) if minutes and judged else None
    for image_id, row, decision, lanes, changed, shift in judged:
        if decision in ("approved", "corrected"):
            geo = {"image_id": image_id, "width": row["width"], "height": row["height"], "lanes": lanes,
                   "horizon_y": 0, "valid_bottom": row["height"] - 1, "tags": {}, "version": f"human:{reviewer}",
                   "reviewer": reviewer, "decision": decision, "batch": batch_dir.name}
            _fill_missing_slots(geo, settings)
            p = settings.path("annotations", "reviewed", f"{image_id}.json")
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and row["locked"]:
                pass                                  # first final human answer wins; re-imports don't overwrite
            else:
                p.write_text(json.dumps(geo, indent=1))
                registry.add_annotation(image_id, "human", reviewer, f"human:{batch_dir.name}", str(p),
                                        license_ids=[row["license_id"]], params={"decision": decision})
        registry.add_review(image_id, reviewer, decision, per_image, batch_dir.name, changed,
                            None if shift != shift else shift)
        counts[decision] += 1
    return counts


def _fill_missing_slots(geo: dict, settings: Settings) -> None:
    from .geometry import GeoParams, _Geo, assign_slots
    if all(l.get("slot") for l in geo["lanes"]):
        return
    g = _Geo((geo["height"], geo["width"]), GeoParams.from_cfg(settings.get("geometry", {})), geo["height"] - 1)
    manual = {id(l): l["slot"] for l in geo["lanes"] if l.get("slot")}
    assign_slots(geo["lanes"], g)
    for l in geo["lanes"]:
        if id(l) in manual:
            l["slot"] = manual[id(l)]


# ---- optional API automation (cvat-sdk, MIT) ----------------------------------------------------

def push_task(package: Path, host: str, user: str, password: str, name: str) -> int:
    from cvat_sdk import make_client  # type: ignore
    from cvat_sdk.core.proxies.tasks import ResourceType  # type: ignore
    labels = json.loads((package / "labels.json").read_text())
    with make_client(host, credentials=(user, password)) as client:
        task = client.tasks.create_from_data(spec={"name": name, "labels": labels}, resource_type=ResourceType.LOCAL,
                                             resources=sorted(str(p) for p in (package / "images").iterdir()))
        task.import_annotations("CVAT 1.1", str(package / "annotations.xml"))
        return int(task.id)


def pull_task(task_id: int, host: str, user: str, password: str, out_zip: Path) -> Path:
    from cvat_sdk import make_client  # type: ignore
    with make_client(host, credentials=(user, password)) as client:
        client.tasks.retrieve(task_id).export_dataset("CVAT for images 1.1", str(out_zip), include_images=False)
    return out_zip
