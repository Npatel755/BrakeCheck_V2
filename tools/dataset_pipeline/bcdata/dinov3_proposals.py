"""Compare trained DINO candidates on frozen validation; never replace human labels."""
import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np

from .bootstrap import atomic_json
from .db import Registry
from .geometry import extract_lanes
from .lanes import params_for
from .licenses import LicenseRegistry
from .local import local_settings
from .quality import read_rgb
from .road_geometry import road_edges_from_probability
from .teachers.base import file_sha256, load_output, save_output
from .teachers.dinov3_seg import Dinov3SegTeacher
from .teachers.source_labels import load_classes

NAME = "dinov3_seg"


def binary_iou(probability, target, valid, threshold=.5):
    predicted = (probability >= threshold) & valid
    target = target & valid
    union = np.count_nonzero(predicted | target)
    return float(np.count_nonzero(predicted & target) / union) if union else None


def summarize(records):
    return {key: {"mean": float(np.mean(values)) if values else None, "n": len(values)}
            for key in ("lane_iou", "road_iou")
            for values in [[r[key] for r in records if r.get(key) is not None]]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--stage", choices=("validation", "all"), default="validation")
    parser.add_argument("--data-root")
    args = parser.parse_args()
    import torch
    from filelock import FileLock
    torch.set_num_threads(4)
    cv2.setNumThreads(1)
    settings = local_settings(args.data_root)
    checkpoint = args.checkpoint.resolve()
    info = json.loads(checkpoint.with_suffix(".json").read_text())
    cohort_file = checkpoint.parent / "training_cohort.json"
    if file_sha256(cohort_file) != info.get("training_cohort_sha256"):
        raise ValueError("Frozen training cohort does not match the checkpoint")
    cohort = json.loads(cohort_file.read_text())
    if cohort.get("test_images_used") != 0:
        raise ValueError("Checkpoint used test images")
    settings.cfg["teachers"].update(device="cuda", amp=True)
    settings.cfg["teachers"][NAME].update(checkpoint=str(checkpoint), enabled=True)
    for model in ("dinov3", "dinov3_seg"):
        LicenseRegistry(settings.licenses_path).check_model(model)
    teacher = Dinov3SegTeacher(settings)
    version = teacher.version
    checksum = file_sha256(checkpoint)
    report = settings.path("reports", "dinov3_proposals.json")
    status = {"state": "loading", "stage": args.stage, "checkpoint": str(checkpoint), "checkpoint_sha256": checksum,
              "version": version, "done": 0, "failed": [], "auto_approve": False, "candidate_only": True}
    def update(**values):
        status.update(values, updated_at=time.time())
        atomic_json(report, status)
    with FileLock(str(settings.path("logs", "large-models.lock")), timeout=0):
        reg = Registry(settings.db_path)
        try:
            validation_ids = [r["image_id"] for r in cohort["cohorts"]["val"]]
            rows = [reg.image(i) for i in validation_ids] if args.stage == "validation" else reg.images("status='curated'")
            if any(row is None for row in rows):
                raise ValueError("Missing frozen validation image")
            update(total=len(rows))
            teacher.load()
            update(state="running")
            scores = {name: [] for name in (NAME, "comma_segnet_fresh", "sam3")}
            per_image = []
            for row in rows:
                image_id = row["image_id"]
                try:
                    path = settings.path("lane_geometry", "teachers", NAME, image_id + ".json")
                    existing = reg.annotation(image_id, "teacher", NAME, version)
                    if existing and path.exists() and json.loads(path.read_text()).get("teacher_version") == version:
                        output = load_output(existing["path"])
                    else:
                        photo = read_rgb(row["path"])
                        if photo is None:
                            raise ValueError("Photo is unreadable")
                        output = teacher.predict(photo, row)
                        output.meta.update(source_guided=False, reference_inputs=[], checkpoint_sha256=checksum,
                                           backbone_revision=info.get("backbone_revision"), license_sha256=info.get("license_sha256"),
                                           trained_steps=info["trained_steps"], model_forwards=1,
                                           evaluation_caveat="Selected on original comma10k validation; not independent phone footage")
                        geometry = extract_lanes(output.lane, params_for(settings), output.extra.get("my_car"), output.extra.get("movable"))
                        geometry.update(image_id=image_id, teacher=NAME, teacher_version=version, source_guided=False,
                                        model_meta=output.meta, road_edges=road_edges_from_probability(output.road, occlusion_mask=output.extra.get("movable")))
                        paths = save_output(settings, NAME, version, image_id, output)
                        atomic_json(path, geometry)
                        reg.add_annotation(image_id, "teacher", NAME, version, paths["_meta"],
                                           license_ids=[row["license_id"], "dinov3", "dinov3_seg"], checkpoint_sha=checksum,
                                           params={"candidate_only": True, "source_guided": False})
                    if args.stage == "validation":
                        ann = reg.annotation(image_id, "source_label")
                        if ann is None:
                            raise ValueError("Validation source label missing")
                        gt = load_classes(ann)
                        candidates = {NAME: output}
                        for name in ("comma_segnet_fresh", "sam3"):
                            ann = reg.annotation(image_id, "teacher", name)
                            if ann:
                                candidates[name] = load_output(ann["path"])
                        record = {"image_id": image_id, "source_lane_pixels": int(np.count_nonzero(gt == 2)), "scores": {}}
                        for name, candidate in candidates.items():
                            result = {}
                            for key, prob, target in (("lane_iou", candidate.lane, gt == 2),
                                                      ("road_iou", candidate.road, (gt == 1) | (gt == 2))):
                                if prob is None:
                                    result[key] = None
                                    continue
                                if prob.shape != gt.shape:
                                    prob = cv2.resize(prob, (gt.shape[1], gt.shape[0]), interpolation=cv2.INTER_LINEAR)
                                result[key] = binary_iou(prob, target, gt > 0)
                            scores[name].append(result)
                            record["scores"][name] = result
                        per_image.append(record)
                    update(done=status["done"] + 1, last_image=image_id)
                except Exception as error:
                    status["failed"].append({"image_id": image_id, "error": type(error).__name__, "detail": str(error)[:200]})
                    update()
            if args.stage == "validation":
                positive = [r for r in per_image if r["source_lane_pixels"] > 0]
                positive_scores = {name: summarize([r["scores"][name] for r in positive if name in r["scores"]]) for name in scores}
                comparison = {"checkpoint": str(checkpoint), "checkpoint_sha256": checksum,
                              "method": "Mean per-image binary IoU at native resolution, threshold 0.5, unknown pixels ignored; not the checkpoint's resized argmax metric",
                              "split": "frozen original validation", "test_images_used": 0,
                              "caveats": ["DINO checkpoint selected on this validation split", "Fresh comma model may have trained on these comma10k images", "Mask IoU does not measure lane polyline accuracy or independent phone performance"],
                              "models": {name: summarize(values) for name, values in scores.items()},
                              "lane_positive_images": len(positive), "lane_positive_models": positive_scores, "per_image": per_image,
                              "failed": status["failed"]}
                atomic_json(settings.path("reports", "dinov3_comparison.json"), comparison)
            update(state="complete" if not status["failed"] else "partial_error")
        except Exception as error:
            update(state="error", error_type=type(error).__name__, detail=str(error)[:300])
            raise
        finally:
            teacher.unload()
            reg.close()


if __name__ == "__main__":
    main()
