"""Resumable Audi import: hidden staging, source conversions, then human audits.

This worker never trains a model or creates a human approval. A pinned plan and
the original source bytes identify its ownership; existing curated images and
all human history remain outside automatic preparation.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from urllib.parse import quote

from filelock import FileLock

from .bootstrap import atomic_json
from .config import Settings
from .db import Registry
from . import fusion, lanes, qc, source_validation, source_converter_a2d2
from .licenses import LicenseRegistry, LicenseError
from .local import local_settings
from .paint_proposals import NAME, REVISION, VERSION, generate_candidate
from .quality import hamming
from .duplicate_verification import verify_pair
from .review_store import ReviewStore
from .sources import a2d2
from .sources.base import check_download_budget
from .teachers.base import file_sha256, run_teacher
from .teachers.comma_segnet import CommaSegnetTeacher
from .teachers.source_labels import SourceLabelTeacher


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",", ":")).encode()).hexdigest()


def read_plan(path):
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    if plan.get("schema") != "a2d2_import_plan:v1" or plan.get("source") != "a2d2":
        raise ValueError("Expected a pinned Audi import plan")
    if plan.get("base_url") != a2d2.BASE_URL or plan.get("camera") != "front_center":
        raise ValueError("Audi plan must use the official front-centre source")
    if canonical_sha(plan.get("items")) != plan.get("items_sha256"):
        raise ValueError("Audi plan item hash mismatch")
    unsigned = {key:value for key,value in plan.items() if key != "plan_sha256"}
    if canonical_sha(unsigned) != plan.get("plan_sha256"):
        raise ValueError("Audi plan hash mismatch")
    if not isinstance(plan["items"],list) or not plan["items"]:
        raise ValueError("Audi plan is empty")
    if plan.get("selected") != len(plan["items"]):
        raise ValueError("Audi plan selection count is inconsistent")
    refs = [item["img"] for item in plan["items"]]
    if len(refs) != len(set(refs)):
        raise ValueError("Audi plan contains repeated images")
    sessions = {}
    for item in plan["items"]:
        if item["split"] not in ("train","val","test"):
            raise ValueError("Audi plan has an invalid split")
        if sessions.setdefault(item["session"],item["split"]) != item["split"]:
            raise ValueError("Audi plan crosses recording splits")
    return plan


def _ref(item):
    return a2d2.BASE_URL + quote(item["img"],safe="/")


def _human_or_claim(reg,image_id):
    return (reg.conn.execute("SELECT 1 FROM annotations WHERE image_id=? AND kind='human' LIMIT 1",(image_id,)).fetchone()
            or reg.conn.execute("SELECT 1 FROM review_events WHERE image_id=? LIMIT 1",(image_id,)).fetchone()
            or reg.conn.execute("SELECT 1 FROM reviews WHERE image_id=? LIMIT 1",(image_id,)).fetchone()
            or reg.conn.execute("SELECT 1 FROM review_claims WHERE image_id=? AND expires>?",(image_id,time.time())).fetchone())


def _owned(row,plan_sha):
    return (row["source"] == "a2d2" and
            json.loads(row["tags"] or "{}").get("a2d2_import_plan",{}).get("value") == plan_sha)


def _source_matches(settings,reg,row,item):
    ann = reg.annotation(row["image_id"],"source_label","a2d2")
    expected = a2d2.safe_path(settings.path("raw","a2d2"),item["mask_meta"]["key"])
    return bool(ann and ann["model_version"] == "s3:"+a2d2._etag(item["mask_meta"]["etag"])
                and Path(ann["path"]).resolve() == expected)


def _preparation_evidence(settings,reg,row):
    geometry = reg.annotation(row["image_id"],"geometry","geometry")
    source = reg.annotation(row["image_id"],"source_label","a2d2")
    quality = dict(reg.conn.execute("SELECT * FROM qc WHERE image_id=?",(row["image_id"],)).fetchone())
    quality.pop("updated_at",None)
    return {"qc_code_sha256":file_sha256(qc.__file__),"qc_record_sha256":canonical_sha(quality),
            "a2d2_converter_code_sha256":file_sha256(source_converter_a2d2.__file__),
            "geometry_code_version":lanes.GEOMETRY_CODE_VERSION,
            "params_sha256":canonical_sha({key:settings.get(key,{}) for key in ("geometry","fusion","qc")}),
            "geometry_id":geometry["id"],"geometry_sha256":file_sha256(geometry["path"]),
            "source_id":source["id"],"source_sha256":file_sha256(source["path"])}


def _prepared(settings,reg,row,verify_provenance=True):
    if not reg.annotation(row["image_id"],"source_label","a2d2"):
        return False
    for kind,producer in (("teacher","source_labels"),("fused","fusion"),("geometry","geometry"),("teacher",NAME)):
        ann = reg.annotation(row["image_id"],kind,producer)
        if not ann or not Path(ann["path"]).is_file():
            return False
    geo = reg.annotation(row["image_id"],"geometry","geometry")
    quality = reg.conn.execute("SELECT geometry_version FROM qc WHERE image_id=?",(row["image_id"],)).fetchone()
    candidate = settings.path("lane_geometry","teachers",NAME,row["image_id"]+".json")
    ready = bool(quality and quality[0] == geo["model_version"] and candidate.is_file()
                 and json.loads(candidate.read_text())["teacher_version"] == VERSION)
    if not ready or not verify_provenance:
        return ready
    saved = reg.tags(reg.image(row["image_id"])).get("a2d2_preparation",{}).get("value")
    return saved == _preparation_evidence(settings,reg,row)


def _deduplicate(reg,rows):
    """Use pHash to find candidates; require same-capture pixel verification."""
    owned_ids = {row["image_id"] for row in rows}
    buckets = [defaultdict(list) for _ in range(8)]
    identities = {}
    def remember(row):
        identities[row['image_id']] = dict(row)
        value = int(row["phash"],16)
        for band in range(8):
            buckets[band][(value >> (8*band)) & 255].append((row["image_id"],value))
    for old in reg.images("status NOT IN ('rejected','dropped') AND phash IS NOT NULL"):
        if old["image_id"] not in owned_ids:
            remember(old)
    keep,excluded = [],{}
    for row in rows:
        if not row["phash"]:
            excluded[row["image_id"]] = "missing_phash"
            continue
        value = int(row["phash"],16)
        candidates = {}
        for band in range(8):
            candidates.update(buckets[band].get((value >> (8*band)) & 255,()))
        duplicate = next((image_id for image_id,other in sorted(candidates.items())
                          if hamming(value,other) <= 6 and verify_pair(dict(row), identities[image_id])['duplicate']),None)
        if duplicate:
            excluded[row["image_id"]] = "near_duplicate_phash:" + duplicate
        else:
            keep.append(row)
            remember(row)
    return keep,excluded


def _load_cpu_teacher(settings):
    # Resolve the already cached, pinned weights before importing the network.
    from huggingface_hub import snapshot_download
    import torch
    import cv2
    snapshot_download("commaai/comma10k-segnet",revision=REVISION,local_files_only=True)
    torch.set_num_threads(2)
    cv2.setNumThreads(1)
    teacher = CommaSegnetTeacher(settings)
    teacher.load()
    return teacher


def _prepare(settings,reg,licenses,teacher,row):
    # Callers supply only this plan's hidden, human-untouched images.
    run_teacher(settings,reg,licenses,SourceLabelTeacher(settings,reg),[row])
    generate_candidate(settings,reg,teacher,row)
    fusion.fuse_image(settings,reg,row,[NAME])
    lanes.run(settings,reg,[reg.image(row["image_id"])],per_teacher=False)
    qc.run(settings,reg,[reg.image(row["image_id"])])
    if not _prepared(settings,reg,row,verify_provenance=False):
        raise RuntimeError("Source conversion or independent comparison was not completed")


def _check_preparation_space(settings):
    """Leave working room if outputs grow after the download budget check."""
    reserve = 5_000_000_000
    free = shutil.disk_usage(settings.root).free
    if free < reserve:
        raise OSError(f"Audi preparation needs at least {reserve / 1e9:.0f} GB working space; "
                      f"{free / 1e9:.2f} GB free. Existing staged files can resume unchanged.")


def publish(settings,reg,image_ids,plan_sha):
    """One transaction opens only fully checked staged images to the phone UI."""
    published = []
    with reg.tx() as con:
        con.execute("BEGIN IMMEDIATE")
        for image_id in image_ids:
            row = reg.image(image_id)
            if row["status"] == "curated":
                continue  # Publication completed before an interrupted status write.
            if not _owned(row,plan_sha) or row["status"] != "ingested" or row["locked"] or _human_or_claim(reg,image_id):
                raise RuntimeError("Audi publication encountered changed ownership or human history: " + image_id)
            if not _prepared(settings,reg,row):
                raise RuntimeError("Audi publication is missing prepared annotations: " + image_id)
            check = con.execute("SELECT * FROM source_conversion_checks WHERE image_id=?",(image_id,)).fetchone()
            if not check or not check["batch_id"].startswith(source_validation.A2D2_POLICY+":"):
                raise RuntimeError("Audi publication is missing its human audit gate: " + image_id)
            for kind,producer,key in (("geometry","geometry","geometry"),("source_label",None,"source")):
                ann = reg.annotation(image_id,kind,producer)
                if (ann["id"] != check[key+"_id"] or ann["path"] != check[key+"_path"]
                        or ann["created_at"] > check["created_at"]
                        or file_sha256(ann["path"]) != check[key+"_sha256"]):
                    raise RuntimeError("Audi publication fingerprint changed: " + image_id)
            if check["state"] != "flagged":
                group = con.execute("SELECT 1 FROM source_validation_groups WHERE group_key=?",(check["group_key"],)).fetchone()
                if not group:
                    raise RuntimeError("Audi publication is missing its source cohort: " + image_id)
            con.execute("UPDATE images SET status='curated',updated_at=? WHERE image_id=?",(time.time(),image_id))
            published.append(image_id)
        if published:
            con.execute("UPDATE local_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
    return published


def run(settings,plan_file,approved_gb,chunk_size=64,workers=4,limit=None):
    plan = read_plan(plan_file)
    if chunk_size < 1 or not 1 <= workers <= 16 or (limit is not None and limit < 1):
        raise ValueError("Use positive chunk/selection sizes and 1..16 workers")
    items = plan["items"][:limit] if limit else plan["items"]
    selection_sha = canonical_sha(items)
    run_id = plan["plan_sha256"][:12]+"-"+selection_sha[:12]
    report_path = settings.path("reports","a2d2_import",run_id,"status.json")
    licenses = LicenseRegistry(settings.licenses_path)
    if not licenses.get("a2d2").commercial or licenses.pool_for_source("a2d2") != "commercial":
        raise LicenseError("Audi source conditions must be cleared for this private commercial-development pipeline")
    licenses.check_model("comma_segnet")
    for name in ("a2d2-LICENSE.txt","a2d2-NOTICE.txt"):
        path = settings.path("licenses",name)
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            raise LicenseError("Audi attribution/licence file is missing: " + str(path))
    ReviewStore(settings)  # Review metadata tables must exist before staging.
    copied = deepcopy(settings.cfg)
    copied.setdefault("teachers",{}).update(device="cpu",amp=False)
    copied["teachers"].setdefault("comma_segnet",{})["revision"] = REVISION
    settings = Settings(copied,settings.config_path,settings.licenses_path)
    code = {str(Path(__file__).name):file_sha256(__file__)}
    for module in (a2d2,lanes,qc,source_validation,fusion,source_converter_a2d2):
        code[Path(module.__file__).name] = file_sha256(module.__file__)
    state = {"schema":"a2d2_import_status:v1","state":"starting","pid":os.getpid(),"run_id":run_id,
             "plan_path":str(Path(plan_file).resolve()),"plan_sha256":plan["plan_sha256"],
             "items_sha256":plan["items_sha256"],"selection_sha256":selection_sha,"total":len(items),
             "full_plan_total":len(plan["items"]),"pilot":limit is not None,"downloaded":0,
             "code_sha256":code,"config_sha256":canonical_sha(copied),"model_revision":REVISION,
             "prepared":0,"excluded":0,"failed":0,"published":0,"errors":{},"started_at":time.time()}
    def update(**changes):
        state.update(changes,updated_at=time.time())
        atomic_json(report_path,state)
        atomic_json(settings.path("reports","a2d2_import.json"),state)
    with FileLock(str(settings.path("logs","a2d2-import.lock")),timeout=0):
        if report_path.exists():
            previous = json.loads(report_path.read_text(encoding="utf-8"))
            if previous.get("state") == "ready":
                atomic_json(settings.path("reports","a2d2_import.json"),previous)
                return previous
            state["started_at"] = previous.get("started_at",state["started_at"])
        reg = Registry(settings.db_path)
        teacher = None
        try:
            objects = {item[key+"_meta"]["key"]:item[key+"_meta"] for item in items
                       for key in ("img","mask","metadata","palette","calibration") if item.get(key+"_meta")}
            needed = sum(obj["size"] for obj in objects.values()
                         if not a2d2.safe_path(settings.path("raw","a2d2"),obj["key"]).exists())
            check_download_budget(settings,needed,settings.path("raw","a2d2"),approved_gb)
            update(state="downloading",planned_bytes=sum(obj["size"] for obj in objects.values()),needed_bytes=needed)
            selected_refs = {_ref(item) for item in items}
            for row in reg.images("source='a2d2' AND status='ingested'"):
                selection = reg.tags(row).get("a2d2_import_selection",{}).get("value")
                if _owned(row,plan["plan_sha256"]) and row["source_ref"] in selected_refs and selection not in (None,selection_sha):
                    raise ValueError("Finish the existing Audi pilot/selection before changing its selection")
            processed_refs = set()
            for start in range(0,len(items),chunk_size):
                chunk = items[start:start+chunk_size]
                old = {row["source_ref"]:row for row in reg.images("source='a2d2'")}
                eligible = [item for item in chunk if _ref(item) not in old or
                            (old[_ref(item)]["status"] == "ingested" and not old[_ref(item)]["locked"]
                             and not _human_or_claim(reg,old[_ref(item)]["image_id"]))]
                result = a2d2.ingest(settings,reg,licenses,items=eligible,approved_gb=approved_gb,workers=workers)
                failed_images = {error["image"] for error in result.get("errors",[])}
                for error in result.get("errors",[]):
                    state["errors"][error["image"]] = error
                current = {row["source_ref"]:row for row in reg.images("source='a2d2'")}
                for item in chunk:
                    row = current.get(_ref(item))
                    if row is None:
                        continue
                    processed_refs.add(row["source_ref"])
                    if item["img"] in failed_images:
                        continue
                    if (row["status"] == "ingested" and not row["locked"] and not _human_or_claim(reg,row["image_id"])
                            and reg.tags(row).get("a2d2_import_plan",{}).get("value") in (None,plan["plan_sha256"])):
                        if not _source_matches(settings,reg,row,item):
                            state["errors"][item["img"]] = {"error":"Pinned source-label version/path is missing or changed"}
                            continue
                        state["errors"].pop(item["img"],None)
                        reg.merge_tags(row["image_id"],{"review_batch":"a2d2-"+plan["plan_sha256"][:12],
                                       "a2d2_import_plan":plan["plan_sha256"],"a2d2_import_selection":selection_sha},origin="source")
                update(downloaded=len(processed_refs),download_chunks=(start//chunk_size)+1,
                       failed=len(state["errors"]))
            by_ref = {row["source_ref"]:row for row in reg.images("source='a2d2'")}
            exact_duplicates = {item["img"]:"exact_duplicate_owned_elsewhere" for item in items
                                if _ref(item) not in by_ref and item["img"] not in state["errors"]}
            staged = [by_ref[_ref(item)] for item in items if _ref(item) in by_ref and
                      by_ref[_ref(item)]["status"] == "ingested" and _owned(by_ref[_ref(item)],plan["plan_sha256"])
                      and item["img"] not in state["errors"] and _source_matches(settings,reg,by_ref[_ref(item)],item)
                      and not _human_or_claim(reg,by_ref[_ref(item)]["image_id"])]
            staged,excluded = _deduplicate(reg,staged)
            for image_id,reason in excluded.items():
                reg.update_image(image_id,status="dropped",reject_reason=reason)
            excluded.update({row["image_id"]:row["reject_reason"] for ref,row in by_ref.items()
                             if ref in selected_refs and row["status"] in ("dropped","rejected")})
            excluded.update(exact_duplicates)
            update(state="preparing",excluded=len(excluded),excluded_images=excluded,
                   downloaded=len(processed_refs)+len(exact_duplicates),
                   existing_curated=sum(row["status"] == "curated" for ref,row in by_ref.items() if ref in selected_refs))
            prepared = []
            for index,row in enumerate(staged):
                image_id = row["image_id"]
                needs_preparation = not _prepared(settings,reg,row)
                if needs_preparation:
                    _check_preparation_space(settings)
                if needs_preparation and teacher is None:
                    update(state="loading_cpu_model",device="cpu",model_revision=REVISION)
                    teacher = _load_cpu_teacher(settings)
                    update(state="preparing")
                try:
                    if needs_preparation:
                        if reg.conn.execute("SELECT 1 FROM source_conversion_checks WHERE image_id=?",(image_id,)).fetchone():
                            raise RuntimeError("A staged audited image changed; preserve it for investigation")
                        _prepare(settings,reg,licenses,teacher,row)
                        reg.merge_tags(image_id,{"a2d2_preparation":_preparation_evidence(settings,reg,row)},origin="source")
                    prepared.append(image_id)
                except Exception as error:
                    state["errors"][image_id] = {"error_type":type(error).__name__,"error":str(error)}
                if (index+1)%chunk_size == 0 or index+1 == len(staged):
                    update(prepared=len(prepared),failed=len(state["errors"]),last_image=image_id)
            update(state="creating_audits",prepared_ids=prepared)
            unchecked = [image_id for image_id in prepared if not reg.conn.execute(
                "SELECT 1 FROM source_conversion_checks WHERE image_id=?",(image_id,)).fetchone()]
            audit = source_validation.run(settings,apply=True,source_name="a2d2",image_ids=unchecked,include_staged=True) if unchecked else None
            update(state="publishing",audit=audit)
            published = publish(settings,reg,prepared,plan["plan_sha256"])
            ready = [row for row in reg.images("source='a2d2' AND status='curated'")
                     if row["source_ref"] in selected_refs and _owned(row,plan["plan_sha256"])]
            checks = [reg.conn.execute("SELECT state FROM source_conversion_checks WHERE image_id=?",(row["image_id"],)).fetchone() for row in ready]
            source_rejected = sum(row["status"] == "rejected" for ref,row in by_ref.items() if ref in selected_refs)
            update(state="partial_ready" if state["errors"] else "ready",published=len(ready),newly_published=len(published),
                   ready_images=len(ready),excluded=len(excluded),source_rejected=source_rejected,
                   audit_counts=dict(Counter(check[0] for check in checks if check)),finished_at=time.time(),
                   meaning="Source conversions require real human sample checks; no automated human approvals were created.")
            return state
        except BaseException as error:
            update(state="interrupted" if isinstance(error,KeyboardInterrupt) else "error",error_type=type(error).__name__,error=str(error))
            raise
        finally:
            if teacher is not None:
                teacher.unload()
            reg.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-file",type=Path,required=True)
    parser.add_argument("--approve-download-gb",type=float,required=True)
    parser.add_argument("--chunk-size",type=int,default=64)
    parser.add_argument("--workers",type=int,default=4)
    parser.add_argument("--limit",type=int)
    parser.add_argument("--data-root",type=Path)
    args = parser.parse_args()
    print(json.dumps(run(local_settings(args.data_root),args.plan_file,args.approve_download_gb,
                         args.chunk_size,args.workers,args.limit)))


if __name__ == "__main__":
    main()
