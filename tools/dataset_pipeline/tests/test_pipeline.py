"""End-to-end on synthetic scenes: ingest -> curate -> split -> source labels -> fuse -> geometry -> QC ->
CVAT round trip -> UFLD export validated against UFLD's own cache script (if a checkout is available)."""
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

from bcdata import fusion, lanes, qc
from bcdata.cvat import export_batch, import_review
from bcdata.curate import curate
from bcdata.export_ufld import export as export_ufld, validate
from bcdata.export_seg import export as export_seg
from bcdata.quality import write_png
from bcdata.sources.base import register_image
from bcdata.splits import assign
from bcdata.teachers.base import run_teacher
from bcdata.teachers.source_labels import SourceLabelTeacher
from tests.synth import scene

UFLD_REPO = os.environ.get("UFLD_REPO")


def build(settings, registry, licenses, n=12):
    raw = settings.path("raw", "comma10k")
    variants = [dict(), dict(curve=25.0), dict(curve=-25.0), dict(stop_line=True), dict(arrow=True),
                dict(lanes=(("solid", 300), ("dashed", 520), ("solid", 880)))]
    # synthetic scenes are far more alike than real frames; only exact-ish duplicates should merge here
    settings.cfg["quality"]["phash_near_dup"] = 1
    settings.cfg["quality"]["embed_near_dup"] = 0.99999
    for i in range(n):
        v = dict(variants[i % len(variants)])
        shift = (i * 37) % 120 - 60
        v["lanes"] = tuple((k, x + shift) for k, x in v.get("lanes", (("solid", 230), ("dashed", 470), ("dashed", 700), ("solid", 940))))
        rgb, cls, _ = scene(seed=i, **v)
        img_p = raw / f"s{i:03d}.png"
        cls_p = raw / f"s{i:03d}_cls.png"
        write_png(img_p, rgb[:, :, ::-1])
        write_png(cls_p, cls)
        res = register_image(settings, registry, licenses, img_p, source="comma10k", session_id=f"sess{i % 6}",
                             source_ref=f"test:{i}", frame_index=i)
        assert res.status == "added", res
        registry.update_image(res.image_id, has_source_label=1)
        registry.add_annotation(res.image_id, "source_label", "comma10k", "test", str(cls_p), ["comma10k"],
                                params={"format": "class_map", "human": True})


def run_auto(settings, registry, licenses):
    curate(settings, registry, target=100)
    assign(settings, registry)
    rows = registry.images("status='curated'")
    run_teacher(settings, registry, licenses, SourceLabelTeacher(settings, registry), rows)
    fusion.run(settings, registry, rows)
    lanes.run(settings, registry, rows)
    return qc.run(settings, registry, rows)


def test_full_pipeline_and_ufld_export(settings, registry, licenses, tmp_path):
    build(settings, registry, licenses)
    counts = run_auto(settings, registry, licenses)
    assert counts.get("source_label", 0) >= 10, counts
    # the dashed/solid scenes give four lanes; the three-lane scene three
    geo = json.loads(next(settings.path("lane_geometry").glob("*.json")).read_text())
    assert geo["lanes"] and geo["mode"] == "source_label"
    # force every split to have data for the export check
    with registry.tx() as c:
        ids = [r["image_id"] for r in registry.images("status='curated'")]
        for k, i in enumerate(ids):
            c.execute("UPDATE images SET split=? WHERE image_id=?", (["train", "train", "val", "test"][k % 4], i))
    res = export_ufld(settings, registry, tmp_path / "ufld")
    assert res["exported"] == len(ids) and res["train"] >= 5
    report = validate(tmp_path / "ufld", Path(UFLD_REPO) if UFLD_REPO else None)
    assert report["ok"], report["errors"][:5]
    if UFLD_REPO:
        assert report["ufld_cache_script"]["identical"], report["ufld_cache_script"]
    cache = json.loads((tmp_path / "ufld" / "culane_anno_cache.json").read_text())
    first = np.array(next(iter(cache.values())))
    assert first.shape == (4, 35, 2)
    assert (first[:, :, 0] > -99999).sum() > 30          # lanes actually sampled
    seg = export_seg(settings, registry, tmp_path / "seg")
    assert seg["exported"] == len(ids)


def test_cvat_round_trip_locks_human_labels(settings, registry, licenses):
    build(settings, registry, licenses, n=6)
    run_auto(settings, registry, licenses)
    with registry.tx() as c:                       # make everything reviewable
        c.execute("UPDATE qc SET tier='low'")
    batch = qc.review_queue(settings, registry, batch_size=3, name="b1")
    pkg = export_batch(settings, registry, batch)
    tree = ET.parse(pkg / "annotations.xml")
    imgs = tree.getroot().findall("image")
    assert len(imgs) == 3
    # image 0: approve untouched; image 1: move one lane 30 px and approve; image 2: reject
    ET.SubElement(imgs[0], "tag", label="review_approved", source="manual")
    pl = [p for p in imgs[1].findall("polyline") if p.get("label") == "lane"][0]
    pts = [tuple(map(float, p.split(","))) for p in pl.get("points").split(";")]
    pl.set("points", ";".join(f"{x + 30:.2f},{y:.2f}" for x, y in pts))
    ET.SubElement(imgs[1], "tag", label="review_approved", source="manual")
    ET.SubElement(imgs[2], "tag", label="review_rejected", source="manual")
    out = batch / "reviewed.xml"
    tree.write(out)
    counts = import_review(settings, registry, batch, out, "reviewer_1", minutes=3)
    assert counts["approved"] == 1 and counts["corrected"] == 1 and counts["rejected"] == 1, counts
    corrected_id = Path(imgs[1].get("name")).stem
    row = registry.image(corrected_id)
    assert row["locked"] == 1 and row["review_status"] == "corrected"
    human = json.loads(Path(registry.annotation(corrected_id, "human")["path"]).read_text())
    # re-running automatic stages must not touch the human label
    rows = registry.images("status='curated'")
    lanes.run(settings, registry, rows, force=True)
    after = json.loads(Path(registry.annotation(corrected_id, "human")["path"]).read_text())
    assert after == human
    assert lanes.current_geometry(registry, corrected_id)[0] == "human"
    # a teacher cannot overwrite a locked image either
    assert run_teacher(settings, registry, licenses, SourceLabelTeacher(settings, registry), [row], force=True)["todo"] == 0
    stats = __import__("bcdata.reports", fromlist=["review_stats"]).review_stats(registry)
    assert stats["reviews"] == 3 and stats["mean_seconds_per_image"] == 60.0


def test_evaluate_scores_source_teacher_against_itself(settings, registry, licenses):
    from bcdata.evaluate import evaluate
    build(settings, registry, licenses, n=6)
    run_auto(settings, registry, licenses)
    with registry.tx() as c:
        c.execute("UPDATE images SET split='test'")
    # pretend a teacher produced exactly the source label: its metrics must be perfect
    rows = registry.images("status='curated'")
    from bcdata.teachers.base import Teacher, TeacherOutput

    class Echo(Teacher):
        name = "echo"
        license_ids = []

        def predict(self, rgb, row):
            return SourceLabelTeacher(settings, registry).predict(rgb, row)
    run_teacher(settings, registry, licenses, Echo(settings), rows)
    res = evaluate(settings, registry, "test", teachers=["echo"], write=False)
    m = res["teachers"]["echo"]
    assert m["n"] == 6 and m["lane_iou"] > 0.999 and m["f1_culane"] > 0.999 and m["fp"] == 0
