"""Add an auditable MIT windshield-view proxy batch without changing existing reviews."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import logging
from pathlib import Path

import requests

from . import fusion, lanes, qc
from .bootstrap import atomic_json
from .db import Registry
from .licenses import LicenseRegistry
from .local import local_settings
from .quality import read_rgb
from .duplicate_verification import verify_pair
from .sources import comma10k
from .sources.base import check_download_budget, download, register_image, UA
from .teachers.base import run_teacher
from .teachers.source_labels import SourceLabelTeacher


def select_unseen(items, existing_sessions, limit):
    """One road-facing image per entirely new recording; preserve deterministic order."""
    seen = set(existing_sessions)
    selected = []
    for item in items:
        if item["session"] in seen or not item["img"].startswith("imgs/"):
            continue
        seen.add(item["session"])
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def split_for(session):
    # A separate, session-disjoint validation extension. Never reassign old splits.
    return "val" if int(hashlib.sha256(session.encode()).hexdigest()[:8], 16) % 10 == 0 else "train"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--data-root")
    parser.add_argument("--batch", default="windshield-proxy-20261017")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--approve-download-gb", type=float)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--chunk-size", type=int, default=64)
    parser.add_argument("--paint-model", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.limit <= 10000 or Path(args.batch).name != args.batch or not 1 <= args.workers <= 16 or not 1 <= args.chunk_size <= 256:
        parser.error("Use a simple batch name, limit 1–10000, workers 1–16 and chunk size 1–256")
    logging.basicConfig(level=logging.INFO)
    import cv2
    from filelock import FileLock
    cv2.setNumThreads(1)
    settings = local_settings(args.data_root)
    folder = settings.path("sources", args.batch)
    folder.mkdir(parents=True, exist_ok=True)
    report = settings.path("reports", "dashcam_expansion.json")
    status = {"state": "planning", "batch": args.batch, "source": "comma10k", "license": "MIT",
              "capture_domain": "windshield-mounted proxy; exact phone hardware unverified", "auto_approve": False,
              "requested": args.limit, "ready": [], "failed": [], "excluded": []}
    def update(**values):
        status.update(values)
        atomic_json(report, status)
    with FileLock(str(settings.path("logs", "dashcam-expansion.lock")), timeout=0):
        reg = Registry(settings.db_path)
        paint_teacher = None
        try:
            plan_file = folder / "plan.json"
            if plan_file.exists():
                planned = json.loads(plan_file.read_text())
            else:
                response = requests.get("https://api.github.com/repos/commaai/comma10k/commits/master", headers=UA, timeout=60)
                response.raise_for_status()
                revision = response.json()["sha"]
                settings.cfg["sources"]["comma10k"].update(branch=revision, folders=["imgs"], limit=0)
                items = comma10k.plan(settings)
                existing = reg.images()
                selected = select_unseen(items, {r["session_id"] for r in existing}, args.limit)
                planned = {"repo": "commaai/comma10k", "revision": revision, "items": selected,
                           "existing_sessions": sorted({r["session_id"] for r in existing}),
                           "existing_ids": [r["image_id"] for r in existing]}
                atomic_json(plan_file, planned)
            revision, items = planned["revision"], planned["items"]
            existing_ids = set(planned["existing_ids"])
            update(state="downloading", revision=revision, selected=len(items))
            planned_bytes = sum(i[k]["size"] for i in items for k in ("img_meta", "mask_meta"))
            update(planned_gb=round(planned_bytes / 1e9, 3))
            if args.plan_only:
                update(state="planned")
                print(json.dumps({"selected": len(items), "planned_gb": round(planned_bytes / 1e9, 3), "revision": revision}))
                return
            check_download_budget(settings, planned_bytes, folder, args.approve_download_gb)
            license_path = folder / "LICENSE.txt"
            download(comma10k.RAW.format(repo=planned["repo"], branch=revision, path="LICENSE"), license_path)
            if "Permission is hereby granted" not in license_path.read_text():
                raise RuntimeError("Unexpected source license")
            raw = settings.path("raw", "comma10k")
            if args.paint_model:
                import torch
                from .paint_proposals import REVISION as PAINT_REVISION
                from .teachers.comma_segnet import CommaSegnetTeacher
                torch.set_num_threads(4)
                settings.cfg["teachers"].update(device="cpu", amp=False)
                settings.cfg["teachers"]["comma_segnet"]["revision"] = PAINT_REVISION
                paint_teacher = CommaSegnetTeacher(settings)
                paint_teacher.load()
            def fetch(item):
                for key in ("img", "mask"):
                    download(comma10k.RAW.format(repo=planned["repo"], branch=revision, path=item[key]), raw / item[key],
                             expected_size=item[key + "_meta"]["size"], git_sha=item[key + "_meta"]["sha"])
                return item
            licenses = LicenseRegistry(settings.licenses_path)
            original_candidates = [(int(r['phash'], 16), dict(r)) for r in reg.images()
                                   if r['phash'] and r['image_id'] in existing_ids]
            def publish(rows):
                if not rows:
                    return
                update(state="source_proposals", preparing=len(rows))
                teacher = run_teacher(settings, reg, licenses, SourceLabelTeacher(settings, reg), rows)
                if teacher.get("failed"):
                    raise RuntimeError("Source label preparation failed")
                fusion.run(settings, reg, rows)
                lanes.run(settings, reg, rows, per_teacher=False)
                qc.run(settings, reg, rows)
                for row in rows:
                    if lanes.current_geometry(reg, row["image_id"]) is None:
                        raise RuntimeError("Queue image has no geometry")
                    if paint_teacher:
                        from .paint_proposals import generate_candidate
                        generate_candidate(settings, reg, paint_teacher, row)
                    reg.update_image(row["image_id"], status="curated")
                    status["ready"].append(row["image_id"])
                    update(count=len(status["ready"]))
                with reg.tx() as con:
                    con.execute("UPDATE local_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
                update(state="downloading", preparing=0)

            def register(item):
                result = register_image(settings, reg, licenses, raw / item["img"], source="comma10k",
                                        session_id=item["session"], source_ref=f"commaai/comma10k@{revision}:{item['img']}",
                                        frame_index=item["frame"], extra_tags={"review_batch": args.batch,
                                            "camera_domain": "windshield_proxy", "phone_capture_verified": False})
                if not result.image_id or result.image_id in existing_ids:
                    status["excluded"].append({"image": item["img"], "reason": "existing exact duplicate"})
                    return None
                row = reg.image(result.image_id)
                expected_ref = f"commaai/comma10k@{revision}:{item['img']}"
                if result.status == "duplicate" and row["source_ref"] != expected_ref:
                    status["excluded"].append({"image": item["img"], "reason": "exact duplicate within import"})
                    return None
                if row["review_status"] in ("approved", "corrected", "rejected"):
                    # A user may already have reviewed an image from this batch.
                    status["ready"].append(result.image_id)
                    return None
                if row["status"] == "rejected":
                    status["excluded"].append({"image": item["img"], "reason": row["reject_reason"]})
                    return None
                value = int(row["phash"], 16)
                if any((value ^ old).bit_count() <= 6 and verify_pair(dict(row), previous)['duplicate']
                       for old, previous in original_candidates):
                    reg.update_image(result.image_id, status="dropped", reject_reason="near_duplicate_expansion")
                    status["excluded"].append({"image": item["img"], "reason": "near duplicate"})
                    return None
                original_candidates.append((value, dict(row)))
                if reg.annotation(result.image_id, "source_label") is None:
                    reg.add_annotation(result.image_id, "source_label", "comma10k", "git:" + item["mask_meta"]["sha"],
                                       str(raw / item["mask"]), license_ids=["comma10k"], params={"format": "comma10k_rgb", "human": True})
                reg.update_image(result.image_id, has_source_label=1, split=split_for(item["session"]))
                return reg.image(result.image_id)

            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                # Consume pinned plan order so perceptual duplicate selection
                # remains deterministic regardless of download completion order.
                futures = [(pool.submit(fetch, item), item) for item in items]
                rows = []
                for future, item in futures:
                    try:
                        item = future.result()
                    except Exception as error:
                        status["failed"].append({"image": item["img"], "error": type(error).__name__})
                        update()
                        continue
                    update(downloaded=status.get("downloaded", 0) + 1)
                    row = register(item)
                    if row is not None:
                        rows.append(row)
                    if len(rows) >= args.chunk_size:
                        publish(rows)
                        rows = []
                publish(rows)
            update(state="ready" if not status["failed"] else "partial_error", count=len(status["ready"]))
            atomic_json(folder / "result.json", status)
        except Exception as error:
            update(state="error", error_type=type(error).__name__, error=str(error))
            raise
        finally:
            if paint_teacher:
                paint_teacher.unload()
            reg.close()


if __name__ == "__main__":
    main()
