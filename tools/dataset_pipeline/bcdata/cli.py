"""Command line: `python -m bcdata <command>`. Every stage is resumable and skips work already done."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .config import ensure_layout, load_settings, setup_logging
from .db import Registry
from .licenses import LicenseRegistry

log = logging.getLogger("bcdata")


def _ctx(args):
    settings = load_settings(args.config)
    if getattr(args, "data_root", None):
        settings.cfg["data_root"] = args.data_root
    ensure_layout(settings)
    setup_logging(settings, args.verbose)
    return settings, Registry(settings.db_path), LicenseRegistry(settings.licenses_path)


def _print(obj) -> None:
    print(json.dumps(obj, indent=1, default=str))


def _rows(registry, args, default="status='curated'"):
    where = default
    params: list = []
    if getattr(args, "split", None):
        where += " AND split=?"
        params.append(args.split)
    if getattr(args, "source", None):
        where += " AND source=?"
        params.append(args.source)
    rows = registry.images(where, params)
    lim = getattr(args, "limit", None)
    return rows[:lim] if lim else rows


def enabled_teachers(settings, only: list[str] | None = None) -> list[str]:
    names = only or settings.get("teachers.order", [])
    out = []
    for n in names:
        if n == "source_labels" or only or settings.get(f"teachers.{n}.enabled", False):
            out.append(n)
    return out


def cmd_init(args):
    s, reg, lic = _ctx(args)
    _print({"data_root": str(s.root), "db": str(s.db_path), "licenses": len(lic.entries)})


def cmd_licenses(args):
    _, _, lic = _ctx(args)
    for row in lic.table():
        print(f"{row['id']:<28} {row['status']:<19} {row['license']}")


def cmd_ingest(args):
    s, reg, lic = _ctx(args)
    with reg.run(f"ingest-{args.source_name}", vars(args)) as counts:
        if args.source_name == "comma10k":
            from .sources import comma10k
            res = comma10k.ingest(s, reg, lic, limit=args.limit, approved_gb=args.approve_download_gb)
        elif args.source_name == "comma2k19":
            from .sources import comma2k19
            res = comma2k19.ingest(s, reg, lic, Path(args.path) if args.path else None, args.limit)
        elif args.source_name == "carla":
            from .sources import carla_gen
            res = carla_gen.generate(s, reg, lic, args.limit)
        elif args.source_name == "zod":
            from .sources import zod_adapter
            res = zod_adapter.ingest(s, reg, lic, args.limit)
        counts.update(res)
    _print(res)


def cmd_requalify(args):
    s, reg, _ = _ctx(args)
    from .sources.base import requalify
    _print(requalify(s, reg))


def cmd_comma2k19_info(args):
    from .sources.comma2k19 import describe
    print(describe())


def cmd_curate(args):
    s, reg, _ = _ctx(args)
    from .curate import curate
    with reg.run("curate", vars(args)) as counts:
        counts.update(curate(s, reg, args.target, args.source))
    _print(counts)


def cmd_split(args):
    s, reg, _ = _ctx(args)
    from .splits import assign
    _print(assign(s, reg))


def cmd_annotate(args):
    s, reg, lic = _ctx(args)
    from .teachers import make_teacher
    from .teachers.base import run_teacher
    rows = _rows(reg, args)
    results = []
    for name in enabled_teachers(s, args.teachers.split(",") if args.teachers else None):
        teacher = make_teacher(name, s, reg)
        rows_for = [r for r in rows if r["has_source_label"]] if name == "source_labels" else rows
        with reg.run(f"annotate-{name}", {"images": len(rows_for)}) as counts:
            try:
                counts.update(run_teacher(s, reg, lic, teacher, rows_for, force=args.force,
                                          allow_unverified=args.allow_unverified))
            except Exception as exc:          # one unavailable teacher (gated weights, no GPU) must not stop the rest
                log.error("teacher %s unavailable: %s", name, exc)
                counts.update({"teacher": name, "error": str(exc)})
        results.append(counts)
    _print(results)


def cmd_train_teacher(args):
    s, reg, _ = _ctx(args)
    from .teachers.dinov3_seg import train
    out = Path(args.out) if args.out else s.path("models", "dinov3_seg", "candidate_manual.pt")
    res = train(s, reg, out, backbone_id=args.backbone, epochs=args.epochs, batch_size=args.batch_size,
                max_steps=args.max_steps, lr=args.lr)
    if args.promote:
        from .teachers.base import file_sha256
        target = s.root / s.get("teachers.dinov3_seg.checkpoint", "models/dinov3_seg/current.pt")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(out.read_bytes())
        target.with_suffix(".json").write_text(out.with_suffix(".json").read_text())
        version = f"dinov3_seg:{file_sha256(target)[:12]}"
        reg.register_model(version, "dinov3_seg", str(target), file_sha256(target), ["dinov3", "dinov3_seg"],
                           metrics=json.loads(out.with_suffix(".json").read_text()).get("val_metrics"))
        reg.promote_model(version, "dinov3_seg")
        res["promoted"] = version
    _print(res)


def cmd_fuse(args):
    s, reg, _ = _ctx(args)
    from . import fusion
    with reg.run("fuse") as counts:
        counts.update(fusion.run(s, reg, _rows(reg, args), args.force))
    _print(counts)


def cmd_geometry(args):
    s, reg, _ = _ctx(args)
    from . import lanes
    with reg.run("geometry") as counts:
        counts.update(lanes.run(s, reg, _rows(reg, args), args.force))
    _print(counts)


def cmd_qc(args):
    s, reg, _ = _ctx(args)
    from . import qc
    with reg.run("qc") as counts:
        counts.update(qc.run(s, reg, _rows(reg, args)))
    _print(counts)


def cmd_queue(args):
    s, reg, _ = _ctx(args)
    from . import cvat, qc
    d = qc.review_queue(s, reg, args.batch_size, args.name)
    pkg = cvat.export_batch(s, reg, d)
    _print({"queue": str(d / "queue.csv"), "cvat_package": str(pkg), "images": len(json.loads((d / "queue.json").read_text()))})


def cmd_review_import(args):
    s, reg, _ = _ctx(args)
    from . import cvat
    _print(cvat.import_review(s, reg, Path(args.batch), Path(args.xml), args.reviewer, args.minutes))


def cmd_export_ufld(args):
    s, reg, _ = _ctx(args)
    from . import export_ufld
    res = export_ufld.export(s, reg, Path(args.out) if args.out else None, args.include_synthetic)
    s.path("reports", "ufld_export.json").write_text(json.dumps(res, indent=1))
    val = export_ufld.validate(Path(res["out"]), Path(args.ufld_repo) if args.ufld_repo else None)
    s.path("reports", "ufld_validation.json").write_text(json.dumps(val, indent=1))
    _print({"export": res, "validation": {k: v for k, v in val.items() if k != "errors"}, "errors": val["errors"][:20]})


def cmd_export_seg(args):
    s, reg, _ = _ctx(args)
    from . import export_seg
    res = export_seg.export(s, reg, Path(args.out) if args.out else None)
    s.path("reports", "seg_export.json").write_text(json.dumps(res, indent=1))
    _print(res)


def cmd_evaluate(args):
    s, reg, _ = _ctx(args)
    from .evaluate import evaluate
    res = evaluate(s, reg, args.split, args.teachers.split(",") if args.teachers else None)
    _print({"teachers": res["teachers"], "references": res["references"], "report": res.get("report")})


def cmd_al_cycle(args):
    s, reg, lic = _ctx(args)
    from .active import cycle
    _print(cycle(s, reg, lic, epochs=args.epochs, max_steps=args.max_steps, force=args.force))


def cmd_report(args):
    s, reg, _ = _ctx(args)
    from .reports import final_report
    print(final_report(s, reg))


def cmd_overlays(args):
    s, reg, _ = _ctx(args)
    from .viz import write_overlays
    rows = _rows(reg, args)
    teachers = [t for t in enabled_teachers(s) if t != "source_labels"]
    n = write_overlays(s, reg, rows, s.path("reports", "overlays"), teachers)
    _print({"overlays": n, "folder": str(s.path("reports", "overlays"))})


def cmd_run_all(args):
    """Milestones 1-4 end to end on what this machine can run; GPU-only teachers are skipped if unavailable."""
    # ingest ~15% more than the target so curation has duplicates and look-alikes to drop
    ns = argparse.Namespace(**{**vars(args), "source_name": "comma10k", "limit": int(args.target * 1.15), "path": None,
                               "teachers": None, "force": False, "allow_unverified": False, "split": None, "source": None,
                               "batch_size": None, "name": None, "out": None, "include_synthetic": False})
    for step in (cmd_init, cmd_ingest, cmd_curate, cmd_split):
        log.info("=== %s", step.__name__)
        step(ns)
    ns.limit = None
    for step in (cmd_annotate, cmd_fuse, cmd_geometry, cmd_qc):
        log.info("=== %s", step.__name__)
        step(ns)
    ns.limit = 100
    cmd_overlays(ns)
    ns.limit = None
    cmd_queue(ns)
    cmd_export_ufld(ns)
    cmd_export_seg(ns)
    ns.split = "test"
    cmd_evaluate(ns)
    cmd_report(ns)


def main(argv=None):
    p = argparse.ArgumentParser(prog="bcdata", description="BrakeCheck lane dataset pipeline")
    p.add_argument("--config")
    p.add_argument("--data-root")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        sp = sub.add_parser(name, **kw)
        sp.set_defaults(fn=fn)
        return sp

    add("init", cmd_init, help="create the folder layout and database")
    add("licenses", cmd_licenses, help="print the licence registry")
    sp = add("ingest", cmd_ingest, help="download/ingest a source")
    sp.add_argument("source_name", choices=["comma10k", "comma2k19", "carla", "zod"])
    sp.add_argument("--limit", type=int)
    sp.add_argument("--path", help="comma2k19: extracted chunk folder")
    sp.add_argument("--approve-download-gb", type=float)
    add("comma2k19-info", cmd_comma2k19_info, help="what to download for comma2k19")
    add("requalify", cmd_requalify, help="re-run quality checks after changing thresholds")
    sp = add("curate", cmd_curate, help="dedupe + cluster + diverse selection")
    sp.add_argument("--target", type=int)
    sp.add_argument("--source")
    add("split", cmd_split, help="assign leak-free train/val/test by session")
    for name, fn in (("annotate", cmd_annotate), ("fuse", cmd_fuse), ("geometry", cmd_geometry), ("qc", cmd_qc),
                     ("overlays", cmd_overlays)):
        sp = add(name, fn)
        sp.add_argument("--split")
        sp.add_argument("--source")
        sp.add_argument("--limit", type=int)
        if name in ("annotate", "fuse", "geometry"):
            sp.add_argument("--force", action="store_true")
        if name == "annotate":
            sp.add_argument("--teachers", help="comma list; default = enabled teachers in config")
            sp.add_argument("--allow-unverified", action="store_true")
    sp = add("train-teacher", cmd_train_teacher, help="train the DINOv3 segmentation decoder")
    sp.add_argument("--backbone")
    sp.add_argument("--epochs", type=int, default=20)
    sp.add_argument("--batch-size", type=int, default=4)
    sp.add_argument("--max-steps", type=int)
    sp.add_argument("--lr", type=float, default=3e-4)
    sp.add_argument("--out")
    sp.add_argument("--promote", action="store_true")
    sp = add("queue", cmd_queue, help="next review batch + CVAT package")
    sp.add_argument("--batch-size", type=int)
    sp.add_argument("--name")
    sp = add("review-import", cmd_review_import, help="import a reviewed CVAT export")
    sp.add_argument("--batch", required=True)
    sp.add_argument("--xml", required=True)
    sp.add_argument("--reviewer", required=True)
    sp.add_argument("--minutes", type=float)
    sp = add("export-ufld", cmd_export_ufld, help="CULane-format export for UFLD V2 + validation")
    sp.add_argument("--out")
    sp.add_argument("--include-synthetic", action="store_true")
    sp.add_argument("--ufld-repo", help="path to a UFLD V2 checkout to cross-check the cache with its own script")
    sp = add("export-seg", cmd_export_seg, help="road/lane semantic segmentation export")
    sp.add_argument("--out")
    sp = add("evaluate", cmd_evaluate, help="compare teachers on the fixed evaluation set")
    sp.add_argument("--split", default="test")
    sp.add_argument("--teachers")
    sp = add("al-cycle", cmd_al_cycle, help="one active-learning cycle")
    sp.add_argument("--epochs", type=int, default=10)
    sp.add_argument("--max-steps", type=int)
    sp.add_argument("--force", action="store_true")
    add("report", cmd_report, help="write the final dataset report")
    sp = add("run-all", cmd_run_all, help="milestones 1-4 end to end")
    sp.add_argument("--target", type=int, default=2000)
    sp.add_argument("--approve-download-gb", type=float)
    sp.add_argument("--ufld-repo")
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
