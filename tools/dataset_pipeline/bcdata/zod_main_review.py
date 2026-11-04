"""Adapt isolated ZOD audit samples to Studio's existing main review canvas."""
from fastapi import HTTPException

from .simulation_review import SimulationDecision
from .zod_mirror_review import ZodCorrection, ZodDecision


def detail(store, image_id, owner=None):
    if owner:
        store.claim(image_id, owner)
    item = store.detail(image_id)
    options = item["teacher_options"]
    return {**item, "width": item["geometry"]["width"], "height": item["geometry"]["height"],
            "origin": "human" if item["correction"] else "source_derived_proposal",
            "review_status": {"approve": "approved", "exclude": "rejected", "skip": "none"}.get((item["decision"] or {}).get("action"), "none"),
            "teacher": options[0]["geometry"] if options else None,
            "source_paint_available": False, "tags": {}, "crop": None,
            "review_later": (item["decision"] or {}).get("action") == "skip",
            "qc": {"issues": [{"code": warning} for warning in item["qc_reasons"]]},
            "source_conversion": {"state": "sample", "repair_policy": item["geometry"]["version"]},
            "zod_audit": {"fingerprint": item["fingerprint"], "warnings": item["qc_reasons"],
                          "group_images": item["group_images"], "group_state": item["group_state"],
                          "training_eligible": False}}


def next_sample(store, split="all", status="pending", exclude=(), owner=None):
    after = ""
    excluded = set(exclude)
    while True:
        items = store.queue(split=split, status=status, after=after, limit=12)["items"]
        if not items:
            return None
        for item in items:
            if item["image_id"] not in excluded:
                try:
                    if owner:
                        store.claim(item['image_id'], owner, revision=item['revision'], fingerprint=item['fingerprint'])
                    return detail(store, item["image_id"])
                except HTTPException as error:
                    if error.status_code != 409:
                        raise
        after = items[-1]["image_id"]


def decide(store, body, reviewer, owner=None):
    item = store.detail(body.image_id)
    if body.fingerprint is None:
        raise HTTPException(422, "Refresh this ZOD sample before saving")
    if body.action not in ("approved", "corrected", "rejected"):
        raise HTTPException(422, "Unknown ZOD audit action")
    if body.action == "rejected":
        saved = store.decide(ZodDecision(image_id=body.image_id, action="exclude", event_id=body.event_id,
                            revision=body.revision, fingerprint=body.fingerprint, notes=body.notes), reviewer, owner)
    elif body.action == "approved" and not item["source_qc_reasons"]:
        saved = store.decide(ZodDecision(image_id=body.image_id, action="approve", event_id=body.event_id,
                            revision=body.revision, fingerprint=body.fingerprint, notes=body.notes), reviewer, owner)
    else:
        if not body.complete_annotation:
            raise HTTPException(422, "Check the whole-photo confirmation before approving this ZOD conversion or correction")
        if body.candidate:
            candidates = {option["name"]: option for option in item["teacher_options"]}
            if body.candidate not in candidates or candidates[body.candidate]["version"] != body.candidate_version:
                raise HTTPException(409, "The selected model proposal changed; refresh it")
        saved = store.correct(ZodCorrection(image_id=body.image_id, revision=body.revision,
                             fingerprint=body.fingerprint, event_id=body.event_id,
                             lanes=body.lanes if body.action == "corrected" else item["geometry"]["lanes"],
                             resolved_qc_reasons=item["source_qc_reasons"], approve=True,
                             complete_annotation=True, no_visible_lane_paint=body.no_visible_lane_paint,
                             notes=body.notes, candidate=body.candidate, candidate_version=body.candidate_version), reviewer, owner)
    return {"event_id": body.event_id, "image_id": body.image_id, "revision": saved["revision"],
            "group_state": saved["group_state"], "training_eligible": False}
