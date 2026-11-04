"""Export to the CULane layout that Ultra-Fast-Lane-Detection-v2 actually loads (dataset='CULane').

Checked against cfzd/Ultra-Fast-Lane-Detection-v2 (data/dali_data.py, scripts/cache_culane_ponits.py):
  * images are read at 1640x590 (TrainCollect hard-codes that size for CULane), so every frame is
    cropped (per-source box in config) and resized to that canvas;
  * list/train_gt.txt lines: "/<img>.jpg /<seg>.png e1 e2 e3 e4";
  * <img>.lines.txt next to each image: one lane per line, "x y x y ..." on rows 250..580 (step 10);
  * laneseg_label_w16/...png: lane k drawn 16 px wide with value k (1=left-left .. 4=right-right);
    the cache script reads this map at each lane's middle point to recover the lane order;
  * culane_anno_cache.json: {"<img>.jpg": [4][35][2]} with rows 250..590 and x=-99999 for "no lane".
Source annotations are never modified; everything here is derived output.
"""
from __future__ import annotations

import csv
import json
import logging
import runpy
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np

from .config import Settings
from .db import Registry
from .geometry import dense_canvas_polyline, make_crop, sample_rows
from .lanes import current_geometry
from .quality import read_gray_u8, read_rgb, write_jpg, write_png

log = logging.getLogger(__name__)
ROWS_CACHE = list(range(250, 600, 10))          # 35 anchors, as in cache_culane_ponits.py
ROWS_LINES = list(range(580, 249, -10))          # written bottom-up like CULane (590 is outside the image)
NO_LANE = -99999.0


def eligible_rows(settings: Settings, registry: Registry, pools: list[str] | None = None,
                  tiers: list[str] | None = None) -> list:
    pools = pools or settings.get("export.ufld.pools", ["commercial"])
    tiers = tiers or settings.get("export.ufld.include_tiers", ["source_label", "high", "medium", "human"])
    q = f"""SELECT i.* , q.tier AS qc_tier FROM images i LEFT JOIN qc q ON q.image_id=i.image_id
            WHERE i.status='curated' AND i.split IS NOT NULL AND i.review_status NOT IN ('rejected')
              AND i.pool IN ({','.join('?' * len(pools))})"""
    rows = registry.conn.execute(q + " ORDER BY i.image_id", pools).fetchall()
    out = []
    for r in rows:
        if settings.get("export.reviewed_only", False) and (
            r["review_status"] not in ("approved", "corrected") or not r["locked"]
        ):
            continue
        tier = "human" if r["review_status"] in ("approved", "corrected") else r["qc_tier"]
        if tier in tiers:
            out.append(r)
    return out


def write_sample(rgb: np.ndarray, geo: dict, crop, line_width: int, jpeg_quality: int, img_path: Path,
                 seg_path: Path) -> tuple[list[int], list[list[tuple[float, int]]], np.ndarray]:
    H, W = rgb.shape[:2]
    x0, y0, x1, y1 = int(crop.x0), int(crop.y0), int(crop.x1), int(crop.y1)
    canvas = cv2.resize(rgb[y0:y1, x0:x1], (crop.canvas_w, crop.canvas_h), interpolation=cv2.INTER_AREA)
    write_jpg(img_path, canvas, jpeg_quality)
    label = np.zeros((crop.canvas_h, crop.canvas_w), np.uint8)
    exists = [0, 0, 0, 0]
    lanes_rows: list[list[tuple[float, int]]] = [[], [], [], []]
    by_slot = {l["slot"]: l for l in geo["lanes"] if l.get("slot") in (1, 2, 3, 4) and l.get("physical", True)}
    for slot in sorted(by_slot):
        pts = sample_rows(by_slot[slot], crop, ROWS_LINES)
        if len(pts) < 2:
            continue
        exists[slot - 1] = 1
        lanes_rows[slot - 1] = pts
        poly = dense_canvas_polyline(by_slot[slot], crop)
        cv2.polylines(label, [np.round(poly).astype(np.int32)], False, int(slot), thickness=line_width)
    # make each lane's middle point carry its own value even where lines overlap (the cache script reads it)
    for k, pts in enumerate(lanes_rows):
        if pts:
            mx, my = pts[len(pts) // 2]
            cx, cy = int(mx) - 1, int(my) - 1
            label[max(0, cy - 1):cy + 2, max(0, cx - 1):cx + 2] = k + 1
    write_png(seg_path, label)
    return exists, lanes_rows, label


def cache_entry(lanes_rows: list[list[tuple[float, int]]]) -> list:
    arr = np.zeros((4, 35, 2), np.float64)
    arr[:, :, 1] = np.tile(np.array(ROWS_CACHE, np.float64), (4, 1))
    arr[:, :, 0] = NO_LANE
    for k, pts in enumerate(lanes_rows):
        for x, y in pts:
            arr[k, int((y - 250) / 10), 0] = float(x)
    return arr.tolist()


def export(settings: Settings, registry: Registry, out: Path | None = None, include_synthetic: bool = False,
           pools: list[str] | None = None) -> dict:
    cfg = settings.get("export.ufld", {})
    out = Path(out or settings.path("exports", "ufld_v2", "BrakeCheck_CULane"))
    cw, ch = int(cfg.get("canvas_width", 1640)), int(cfg.get("canvas_height", 590))
    rows = eligible_rows(settings, registry, pools)
    lists: dict[str, list[str]] = {"train_gt": [], "val_gt": [], "test": [], "test_gt": [], "train_gt_synthetic": []}
    cache: dict[str, list] = {}
    manifest = []
    counts = {"eligible": len(rows), "exported": 0, "no_geometry": 0, "no_lanes": 0, "train": 0, "val": 0, "test": 0, "synthetic": 0}
    for r in rows:
        cur = current_geometry(registry, r["image_id"])
        if cur is None:
            counts["no_geometry"] += 1
            continue
        origin, geo = cur
        rgb = read_rgb(r["path"])
        if rgb is None:
            continue
        crop_cfg = (cfg.get("crop") or {}).get(r["source"]) or (cfg.get("crop") or {}).get("default", {})
        crop = make_crop(crop_cfg, rgb.shape[1], rgb.shape[0], cw, ch)
        rel_img = f"images/{r['split']}/{r['image_id']}.jpg"
        rel_seg = f"laneseg_label_w16/images/{r['split']}/{r['image_id']}.png"
        exists, lanes_rows, _ = write_sample(rgb, geo, crop, int(cfg.get("line_width", 16)), int(cfg.get("jpeg_quality", 95)),
                                             out / rel_img, out / rel_seg)
        if not any(exists):
            counts["no_lanes"] += 1
        with open(out / rel_img.replace(".jpg", ".lines.txt"), "w", encoding="utf-8") as f:
            for pts in lanes_rows:
                if pts:
                    f.write(" ".join(f"{x:.3f} {y}" for x, y in pts) + " \n")
        flags = " ".join(map(str, exists))
        if r["synthetic"]:
            counts["synthetic"] += 1
            lists["train_gt_synthetic"].append(f"/{rel_img} /{rel_seg} {flags}")
            if include_synthetic and r["split"] == "train":
                lists["train_gt"].append(f"/{rel_img} /{rel_seg} {flags}")
        elif r["split"] == "train":
            lists["train_gt"].append(f"/{rel_img} /{rel_seg} {flags}")
        elif r["split"] == "val":
            lists["val_gt"].append(f"/{rel_img} /{rel_seg} {flags}")
        else:
            lists["test"].append(f"/{rel_img}")
            lists["test_gt"].append(f"/{rel_img} /{rel_seg} {flags}")
        if r["split"] == "train" and (not r["synthetic"] or include_synthetic):
            cache[rel_img] = cache_entry(lanes_rows)
        counts[r["split"]] += 1
        counts["exported"] += 1
        manifest.append({"image": rel_img, "image_id": r["image_id"], "split": r["split"], "source": r["source"],
                         "session_id": r["session_id"], "license": r["license_id"], "pool": r["pool"],
                         "synthetic": r["synthetic"], "label_origin": origin, "qc_tier": r["qc_tier"],
                         "geometry_version": geo.get("version"), "lanes": int(sum(exists)), "source_ref": r["source_ref"]})
    (out / "list").mkdir(parents=True, exist_ok=True)
    for name, lines in lists.items():
        (out / "list" / f"{name}.txt").write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    (out / "culane_anno_cache.json").write_text(json.dumps(cache))
    with open(out / "manifest.csv", "w", newline="", encoding="utf-8") as f:
        if manifest:
            w = csv.DictWriter(f, fieldnames=list(manifest[0]))
            w.writeheader()
            w.writerows(manifest)
    write_config(out, counts["train"], settings)
    write_notice(settings, out, {m["license"] for m in manifest})
    counts["out"] = str(out)
    return counts


def write_config(out: Path, n_train: int, settings: Settings) -> Path:
    backbone = str(settings.get("export.ufld.backbone", "18"))
    if backbone not in ("18", "34"):
        raise ValueError("Choose a verified UFLDv2 configuration: ResNet18 or ResNet34")
    iters = max(1, -(-n_train // 32))
    text = f"""# Ultra-Fast-Lane-Detection-v2 config for the BrakeCheck export (CULane layout).
# Generated by bcdata. Train from ImageNet backbone weights only: UFLD's released CULane/TuSimple/CurveLanes
# checkpoints come from non-commercial datasets, so do not set `finetune` to them for the App Store model.
dataset= 'CULane'
data_root= r'{out.resolve()}'
epoch= 50
batch_size= 32
optimizer= 'SGD'
learning_rate= 0.05 / 8   # single GPU (UFLD README: divide the 8-GPU rate by 8)
weight_decay= 0.0001
momentum= 0.9
scheduler= 'multi'
steps= [25,38]
gamma= 0.1
warmup= 'linear'
warmup_iters= {min(695, iters * 2)}
use_aux= False
griding_num= 200
backbone= '{backbone}'
sim_loss_w= 0.0
shp_loss_w= 0.0
note= 'brakecheck'
log_path= ''
finetune= None
resume= None
test_model= ''
test_work_dir= ''
tta= True
num_lanes= 4
var_loss_power= 2.0
auto_backup= True
num_row= 72
num_col= 81
train_width= 1600
train_height= 320
num_cell_row= 200
num_cell_col= 100
mean_loss_w= 0.05
fc_norm= True
crop_ratio= 0.6
"""
    p = out / "configs" / f"brakecheck_culane_res{backbone}.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def write_notice(settings: Settings, out: Path, licenses_used: set[str]) -> None:
    from .licenses import LicenseRegistry
    reg = LicenseRegistry(settings.licenses_path)
    lines = ["BrakeCheck lane dataset export - sources and obligations", ""]
    for key in sorted(licenses_used):
        e = reg.get(key)
        lines.append(f"- {key}: {e.data.get('license', '?')} ({e.status})")
        for ob in e.data.get("obligations", []) or []:
            lines.append(f"    * {ob}")
    if "zod" in licenses_used:
        from .sources.zod_adapter import ZOD_NOTICE
        lines += ["", ZOD_NOTICE]
    if "a2d2" in licenses_used:
        # Keep Audi attribution and the complete licence with private derived
        # exports. The original source notice must not be lost at conversion.
        source_files = [settings.path("licenses", name) for name in
                        ("a2d2-LICENSE.txt", "a2d2-NOTICE.txt")]
        for source in source_files:
            if not source.is_file() or not source.read_text(encoding="utf-8").strip():
                raise ValueError(f"Missing Audi export licence/attribution: {source}")
        target = out / "licenses"
        target.mkdir(parents=True, exist_ok=True)
        for source in source_files:
            shutil.copy2(source, target / source.name)
        lines += ["", source_files[1].read_text(encoding="utf-8").strip()]
    lines += ["", "Labels: automatic labels are provisional unless manifest.csv says label_origin=human."]
    (out / "NOTICE.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---- validation ----------------------------------------------------------------------------------

def validate(out: Path, ufld_repo: Path | None = None) -> dict:
    """Read the export the way UFLD's train iterator does, and (optionally) rebuild the cache with UFLD's
    own scripts/cache_culane_ponits.py and compare it with ours value by value."""
    report: dict = {"errors": [], "checked": 0}
    cache = json.loads((out / "culane_anno_cache.json").read_text())
    lines = (out / "list" / "train_gt.txt").read_text().splitlines()
    for line in lines:
        info = line.split()
        img_name, seg_name = info[0].lstrip("/"), info[1].lstrip("/")
        if len(info) != 6:
            report["errors"].append(f"bad list line: {line}")
        img = read_rgb(out / img_name)
        seg = read_gray_u8(out / seg_name)
        if img is None or img.shape[:2] != (590, 1640):
            report["errors"].append(f"{img_name}: image missing or not 1640x590")
        if seg is None or seg.shape[:2] != (590, 1640) or not set(np.unique(seg)) <= {0, 1, 2, 3, 4}:
            report["errors"].append(f"{seg_name}: label missing, wrong size or bad values")
        pts = np.array(cache.get(img_name, []))
        if pts.shape != (4, 35, 2):
            report["errors"].append(f"{img_name}: cache entry shape {pts.shape}")
            continue
        flags = list(map(int, info[2:6]))
        for k in range(4):
            has = bool((pts[k, :, 0] > NO_LANE).any())
            if has != bool(flags[k]):
                report["errors"].append(f"{img_name}: lane {k + 1} existence flag {flags[k]} but cache has={has}")
        for i, lane in enumerate((out / img_name.replace("jpg", "lines.txt")).read_text().splitlines()):
            vals = lane.strip().split(" ")
            xs, ys = vals[::2], vals[1::2]
            mid = len(xs) // 2
            mx, my = int(float(xs[mid])), int(float(ys[mid]))
            if seg is not None and seg[my - 1, mx - 1] == 0:
                report["errors"].append(f"{img_name}: lane line {i} middle point is unlabelled (UFLD cache would stop)")
        report["checked"] += 1
    if ufld_repo is not None and not report["errors"]:     # the script drops into pdb on unlabelled mid points
        report["ufld_cache_script"] = _compare_with_ufld_script(out, Path(ufld_repo), cache)
        if not report["ufld_cache_script"].get("identical"):
            report["errors"].append("UFLD's own cache script disagrees with our cache")
    report["ok"] = not report["errors"]
    return report


def _compare_with_ufld_script(out: Path, ufld_repo: Path, ours: dict) -> dict:
    script = ufld_repo / "scripts" / "cache_culane_ponits.py"
    if not script.exists():
        return {"identical": False, "error": f"{script} not found"}
    tmp = out / "_ufld_cache_check"
    if tmp.exists():
        shutil.rmtree(tmp)
    backup = out / "culane_anno_cache.json"
    saved = backup.read_bytes()
    if not hasattr(np, "float"):
        np.float = float                         # the script uses np.float, removed in NumPy 1.24
    argv = sys.argv
    try:
        sys.argv = [str(script), "--root", str(out)]
        runpy.run_path(str(script), run_name="__main__")
        theirs = json.loads(backup.read_text())
    finally:
        sys.argv = argv
        backup.write_bytes(saved)
    keys_equal = set(theirs) == set(ours)
    max_diff = 0.0
    for k in ours:
        if k in theirs:
            max_diff = max(max_diff, float(np.abs(np.array(ours[k]) - np.array(theirs[k])).max()))
    return {"identical": keys_equal and max_diff < 1e-3, "keys_equal": keys_equal, "max_abs_diff": max_diff,
            "entries": len(theirs)}
