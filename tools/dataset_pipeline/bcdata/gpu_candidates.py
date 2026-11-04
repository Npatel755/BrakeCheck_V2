"""Large, pinned, commercially permissible annotation candidates on one RTX 4090.

Inference only. Source-guided SAM2 results are identified explicitly and never
reported as independent accuracy. Neither model changes a reviewed annotation.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import gc
import hashlib
import json
import logging
import os
import time

import numpy as np
import requests
import yaml

from .bootstrap import atomic_json
from .config import REPO_ROOT
from .fusion import load_fused
from .geometry import extract_lanes
from .lanes import params_for
from .local import local_settings
from .db import Registry
from .quality import read_rgb
from .road_geometry import road_edges_from_probability
from .teachers.base import TeacherOutput, amp_context, save_output
from .teachers.sam2_refine import component_prompts
from .teachers.sam3_teacher import masks_to_prob

log = logging.getLogger(__name__)
PROFILE_PATH = REPO_ROOT / "config" / "rtx4090.yaml"


def profile():
    return yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))


def candidate_version(name, model_cfg):
    return name + ":" + model_cfg["revision"] + ":" + hashlib.sha256(json.dumps(model_cfg, sort_keys=True).encode()).hexdigest()[:12]


def inspect_models(settings, fetch_licenses=True):
    import torch
    from huggingface_hub import HfApi, get_token
    cfg = profile()
    try:
        previous = json.loads(settings.path("reports", "model_status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    result = {"state": "configured", "pid": os.getpid(), "training": "deferred", "cuda": torch.cuda.is_available(), "torch": torch.__version__,
              "hf_signed_in": bool(get_token()), "models": {}, "checked_at": time.time(), "profile": str(PROFILE_PATH)}
    if result["cuda"]:
        gpu = torch.cuda.get_device_properties(0)
        result.update(gpu=gpu.name, vram_gib=round(gpu.total_memory / 2**30, 2), bf16=torch.cuda.is_bf16_supported())
    else:
        result["state"] = "cuda_setup_required"
    api = HfApi()
    for name, model in cfg["models"].items():
        item = {key: model[key] for key in ("hub_id", "revision", "license", "commercial_status", "role")}
        item["teacher_version"] = candidate_version(name, model)
        old = previous.get("models", {}).get(name, {})
        same_version = old.get("teacher_version") == item["teacher_version"] or (
            name == "sam2_large" and not old.get("teacher_version") and old.get("hub_id") == model["hub_id"] and old.get("revision") == model["revision"]
        )
        if same_version and old.get("state") in ("complete", "partial_error"):
            item = {**old, **item}
        if fetch_licenses:
            response = requests.get(model["license_url"], timeout=30)
            response.raise_for_status()
            license_bytes = response.content
            path = settings.path("licenses", name + "-LICENSE.txt")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(license_bytes)
            item.update(license_url=model["license_url"], license_sha256=hashlib.sha256(license_bytes).hexdigest(), license_file=str(path))
        try:
            api.auth_check(repo_id=model["hub_id"], repo_type="model")
            item["access"] = "available"
        except Exception as exc:
            # Do not serialize exception strings that might contain auth headers.
            item["access"] = "login_or_model_access_required"
            item["access_error_type"] = type(exc).__name__
        item["state"] = "deferred_no_trained_decoder" if name == "dinov3" else "ready_to_load" if item["access"] == "available" and result["cuda"] else "waiting_for_access" if item["access"] != "available" else "waiting_for_cuda"
        if same_version and old.get("state") in ("complete", "partial_error"):
            item["state"] = old["state"]
        if item["access"] == "available":
            item.pop("access_error_type", None)
        result["models"][name] = item
    atomic_json(settings.path("reports", "model_status.json"), result)
    atomic_json(settings.path("licenses", "model_provenance.json"), result)
    return result


def filter_exclusion_instances(masks, scores, max_area_fraction):
    """Reject implausible individual vehicle masks before taking their union."""
    if not 0 < max_area_fraction <= 1:
        raise ValueError("exclusion_max_area_fraction must lie in (0, 1]")
    if masks is None or not len(scores):
        return masks, scores, {"instances_before_filter": 0, "dropped_oversized_instances": 0,
                               "maximum_instance_area_fraction": 0.0,
                               "exclusion_max_area_fraction": max_area_fraction}
    h, w = masks.shape[-2:]
    shaped = masks.reshape(-1, h, w)
    if hasattr(shaped, "detach"):
        fractions = (shaped > .5).sum(dim=(-2, -1)).float() / (h * w)
        keep = fractions <= max_area_fraction
        dropped = int((~keep).sum().item())
        maximum = float(fractions.max().item())
    else:
        fractions = (np.asarray(shaped) > .5).mean(axis=(-2, -1))
        keep = fractions <= max_area_fraction
        dropped = int(np.count_nonzero(~keep))
        maximum = float(fractions.max())
    return shaped[keep], scores.reshape(-1)[keep], {
        "instances_before_filter": int(len(fractions)), "dropped_oversized_instances": dropped,
        "maximum_instance_area_fraction": maximum, "exclusion_max_area_fraction": max_area_fraction,
    }


class Sam3Candidate:
    def __init__(self, cfg):
        self.cfg = cfg

    def load(self):
        import torch
        from transformers import Sam3Model, Sam3Processor
        self.model = Sam3Model.from_pretrained(self.cfg["hub_id"], revision=self.cfg["revision"], dtype=torch.bfloat16).to("cuda").eval()
        self.processor = Sam3Processor.from_pretrained(self.cfg["hub_id"], revision=self.cfg["revision"])

    def predict(self, rgb, reference=None):
        import torch
        from PIL import Image
        if reference is not None:
            raise ValueError("Independent SAM3 proposals must not receive a source or human reference")
        image = Image.fromarray(rgb)
        threshold = float(self.cfg["threshold"])
        groups = {"lane": self.cfg["prompts"], "road": self.cfg.get("road_prompts", []),
                  "occlusion": self.cfg.get("exclusion_prompts", [])}
        self.last_counts = {"image_encoder_forwards": 0, "model_forwards": 0, "prompt_forwards": 0}
        images = self.processor(images=image, return_tensors="pt").to("cuda")
        with torch.inference_mode(), amp_context(torch, "cuda", True):
            vision_embeds = self.model.get_vision_features(pixel_values=images["pixel_values"])
        self.last_counts["image_encoder_forwards"] = 1
        maps, prompt_results = {}, []
        for group, prompts in groups.items():
            candidates = []
            for prompt in prompts:
                inputs = self.processor(text=prompt, return_tensors="pt").to("cuda")
                with torch.inference_mode(), amp_context(torch, "cuda", True):
                    outputs = self.model(vision_embeds=vision_embeds, input_ids=inputs["input_ids"],
                                         attention_mask=inputs.get("attention_mask"))
                self.last_counts["model_forwards"] += 1
                self.last_counts["prompt_forwards"] += 1
                processed = self.processor.post_process_instance_segmentation(outputs, threshold=threshold, mask_threshold=.5,
                                                                               target_sizes=images["original_sizes"].tolist())[0]
                masks, scores, filter_info = processed["masks"], processed["scores"], {}
                if group == "occlusion":
                    masks, scores, filter_info = filter_exclusion_instances(
                        masks, scores, float(self.cfg.get("exclusion_max_area_fraction", .30)))
                probability = masks_to_prob(masks, scores, rgb.shape[:2], threshold)
                candidates.append(probability)
                prompt_results.append({"group": group, "prompt": prompt, "instances": int(len(scores)), **filter_info,
                                       "accepted_pixels": int(np.count_nonzero(probability >= threshold)),
                                       "maximum_score": float(probability.max())})
                del outputs, processed
            maps[group] = np.maximum.reduce(candidates) if candidates else None
        extra = {"occlusion": maps["occlusion"]} if maps["occlusion"] is not None else {}
        return TeacherOutput(lane=maps["lane"], road=maps["road"], extra=extra,
                             meta={"prompts": self.cfg["prompts"], "road_prompts": self.cfg.get("road_prompts", []),
                                   "exclusion_prompts": self.cfg.get("exclusion_prompts", []), "threshold": threshold,
                                   "exclusion_max_area_fraction": float(self.cfg.get("exclusion_max_area_fraction", .30)),
                                   "occlusion_dropped_oversized_instances": sum(p.get("dropped_oversized_instances", 0) for p in prompt_results),
                                   "prompt_results": prompt_results, **self.last_counts, "vision_embeddings_reused": True,
                                   "source_guided": False, "reference_inputs": [],
                                   "description": "Independent SAM3 image/text proposals. No source mask, human labels, or fitted source geometry supplied."})

    def unload(self):
        del self.model, self.processor


class Sam2LargeCandidate:
    def __init__(self, cfg):
        self.cfg = cfg

    def load(self):
        import torch
        from transformers import Sam2Model, Sam2Processor
        self.model = Sam2Model.from_pretrained(self.cfg["hub_id"], revision=self.cfg["revision"], dtype=torch.bfloat16).to("cuda").eval()
        self.processor = Sam2Processor.from_pretrained(self.cfg["hub_id"], revision=self.cfg["revision"])

    def predict(self, rgb, reference=None):
        import torch
        from PIL import Image
        if reference is None:
            raise ValueError("SAM2 refinement needs an explicitly identified seed mask")
        self.last_counts = {"image_encoder_forwards": 0, "model_forwards": 0, "prompt_forwards": 0}
        source = reference >= .5
        result = source.copy()
        components = list(component_prompts(source))
        if not components:
            return TeacherOutput(lane=result.astype(np.float32), meta={"source_guided": True, "components": 0, "refined": 0,
                                 **self.last_counts, "description": "Empty source mask; no SAM2 model forward was performed."})
        image = Image.fromarray(rgb)
        pixels = self.processor(images=image, return_tensors="pt").to("cuda")
        with torch.inference_mode(), amp_context(torch, "cuda", True):
            embeddings = self.model.get_image_embeddings(pixels["pixel_values"])
        self.last_counts["image_encoder_forwards"] = 1
        refined = 0
        for comp, box, pts in components:
            # Reuse native-resolution image features across prompts. One
            # component at a time bounds mask memory on a 24-GB card.
            inputs = self.processor(images=image, input_points=[[pts.tolist()]], input_labels=[[[1] * len(pts)]],
                                    input_boxes=[[box.tolist()]], return_tensors="pt").to("cuda")
            with torch.inference_mode(), amp_context(torch, "cuda", True):
                outputs = self.model(image_embeddings=embeddings, input_points=inputs["input_points"], input_labels=inputs["input_labels"],
                                     input_boxes=inputs["input_boxes"], multimask_output=True)
            self.last_counts["model_forwards"] += 1
            self.last_counts["prompt_forwards"] += 1
            masks = self.processor.post_process_masks(outputs.pred_masks.float(), inputs["original_sizes"])[0]
            best = int(outputs.iou_scores[0, 0].float().argmax().item())
            candidate = masks[0, best].detach().cpu().numpy() > 0
            union = np.logical_or(candidate, comp).sum()
            iou = float(np.logical_and(candidate, comp).sum() / union) if union else 0.
            if candidate.sum() <= float(self.cfg["max_area_growth"]) * comp.sum() and iou >= float(self.cfg["min_iou"]):
                result[comp] = False
                result |= candidate
                refined += 1
        return TeacherOutput(lane=result.astype(np.float32), meta={"source_guided": True, "components": len(components), "refined": refined, **self.last_counts,
                             "description": "SAM2.1 Hiera Large refined existing source-paint prompts; not an independent test prediction."})

    def unload(self):
        del self.model, self.processor


def completed_candidate(settings, reg, image_id, name, version):
    """Resume only complete probability metadata/maps and matching geometry."""
    ann = reg.annotation(image_id, "teacher", name, version)
    if ann is None:
        return None
    geometry_path = settings.path("lane_geometry", "teachers", name, image_id + ".json")
    try:
        metadata = json.loads(Path(ann["path"]).read_text(encoding="utf-8"))
        geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
        maps = metadata.get("maps", {})
        required = ("lane", "road", "occlusion") if name == "sam3" else ("lane",)
        if geometry.get("teacher_version") != version or not all(key in maps for key in required):
            return None
        if not all(Path(path).is_file() for key, path in maps.items() if not key.startswith("_")):
            return None
        return geometry, metadata.get("meta", {})
    except (OSError, ValueError, TypeError):
        return None


def record_completed(model_info, geometry, meta):
    """Count output content separately from actual model execution."""
    model_info["done"] += 1
    independent = meta.get("source_guided") is False
    model_info["independent_completed"] += int(independent)
    painted = any(lane.get("slot") for lane in geometry.get("lanes", []))
    model_info["painted_nonempty" if painted else "painted_empty"] += 1
    if "road_edges" in geometry:
        road = any(edge.get("status") == "visible" for edge in geometry["road_edges"].values())
        model_info["road_edge_nonempty" if road else "road_edge_empty"] += 1
    else:
        model_info["road_not_requested"] += 1
    if "model_forwards" in meta:
        forward = meta["model_forwards"] > 0 or meta.get("image_encoder_forwards", 0) > 0
        model_info["verified_forward_images" if forward else "verified_zero_forward_images"] += 1
    elif meta.get("source_guided") is True and meta.get("components") == 0:
        # The original SAM2 empty-source branch returned without invoking it.
        model_info["verified_zero_forward_images"] += 1
    else:
        model_info["historical_forward_count_unknown"] += 1


def record_run_forwards(model_info, counts):
    for key in ("model_forwards", "image_encoder_forwards", "prompt_forwards"):
        model_info[key + "_this_run"] += int(counts.get(key, 0))
    if counts.get("model_forwards", 0) or counts.get("image_encoder_forwards", 0):
        model_info["model_forward_images_this_run"] += 1
    else:
        model_info["zero_forward_images_this_run"] += 1


def run_candidates(settings, names=("sam3", "sam2_large"), limit=None, split=None):
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA PyTorch is required. Run Setup-Large-Models.cmd; the reviewer itself stays usable on CPU.")
    status = inspect_models(settings)
    status["state"] = "running"
    cfg = profile()
    total_memory = torch.cuda.get_device_properties(0).total_memory
    torch.cuda.set_per_process_memory_fraction(min(.96, cfg["hardware"]["max_allocated_gib"] * 2**30 / total_memory))
    reg = Registry(settings.db_path)
    failures = []
    try:
        where, args = "status='curated'", []
        if split:
            where += " AND split=?"
            args.append(split)
        all_rows = reg.images(where, args)
        locked_at_start = sum(bool(row["locked"]) for row in all_rows)
        # Candidates remain separate from current_geometry, including when a
        # person has already accepted the paint and wants to review edges later.
        rows = list(all_rows)
        split_order = {"test": 0, "val": 1, "train": 2}
        if limit is None:
            priorities = dict(reg.conn.execute("SELECT image_id,priority FROM qc"))
            # Match the review queue so difficult photos receive new proposals
            # first; limited smoke checks retain stable image-ID selection.
            rows.sort(key=lambda row: (split_order.get(row["split"], 3),
                                       -float(priorities.get(row["image_id"], 0) or 0), row["image_id"]))
        else:
            rows.sort(key=lambda row: (split_order.get(row["split"], 3), row["image_id"]))
        if limit:
            rows = rows[:limit]
        for name in names:
            if name not in ("sam3", "sam2_large"):
                raise ValueError(f"Not an enabled inference teacher: {name}")
            model_info = status["models"][name]
            if model_info["access"] != "available":
                failures.append({"model": name, "reason": "Hugging Face login/model approval required"})
                model_info["state"] = "waiting_for_access"
                continue
            model_cfg = cfg["models"][name]
            version = candidate_version(name, model_cfg)
            model_info.update(state="loading", teacher_version=version, total=len(rows), done=0, failed=0,
                              human_locked_at_start=locked_at_start, skipped_human_locked_during_run=0,
                              human_reviews_preserved=locked_at_start, candidate_only=True,
                              discarded_after_human_lock=0, independent_completed=0, painted_nonempty=0, painted_empty=0,
                              road_edge_nonempty=0, road_edge_empty=0, road_not_requested=0,
                              verified_forward_images=0, verified_zero_forward_images=0, historical_forward_count_unknown=0,
                              model_forwards_this_run=0, image_encoder_forwards_this_run=0, prompt_forwards_this_run=0,
                              model_forward_images_this_run=0, zero_forward_images_this_run=0,
                              prompt_count_per_image=sum(len(model_cfg.get(key, [])) for key in ("prompts", "road_prompts", "exclusion_prompts")) if name == "sam3" else None)
            todo = []
            preserved_review_ids = {row["image_id"] for row in all_rows if row["locked"]}
            for row in rows:
                completed = completed_candidate(settings, reg, row["image_id"], name, version)
                if completed is None:
                    todo.append(row)
                else:
                    record_completed(model_info, *completed)
            model_info.update(todo=len(todo), resumed_completed=model_info["done"])
            atomic_json(settings.path("reports", "model_status.json"), status)
            if not todo:
                model_info["state"] = "complete"
                continue
            teacher = Sam3Candidate(model_cfg) if name == "sam3" else Sam2LargeCandidate(model_cfg)
            loaded = False
            try:
                torch.cuda.reset_peak_memory_stats()
                teacher.load()
                loaded = True
                model_info.update(state="running", parameters=sum(p.numel() for p in teacher.model.parameters()))
                for row in todo:
                    # Human state is observed for reporting only. Separate
                    # candidate records never alter that state or its geometry.
                    if reg.image(row["image_id"])["locked"]:
                        preserved_review_ids.add(row["image_id"])
                        model_info["human_reviews_preserved"] = len(preserved_review_ids)
                    started = time.monotonic()
                    teacher.last_counts = {}
                    forwards_recorded = False
                    try:
                        rgb = read_rgb(row["path"])
                        if rgb is None:
                            raise ValueError("Source photo is missing or unreadable")
                        reference = load_fused(reg, row["image_id"])[1]["lane"] if name == "sam2_large" else None
                        output = teacher.predict(rgb, reference)
                        record_run_forwards(model_info, teacher.last_counts)
                        forwards_recorded = True
                        if reg.image(row["image_id"])["locked"]:
                            preserved_review_ids.add(row["image_id"])
                            model_info["human_reviews_preserved"] = len(preserved_review_ids)
                        output.meta.update(hub_id=model_cfg["hub_id"], repository_revision=model_cfg["revision"],
                                           license=model_cfg["license"], license_sha256=model_info["license_sha256"], precision="bfloat16")
                        params = params_for(settings)
                        if name == "sam3":
                            params.threshold = float(model_cfg.get("geometry_threshold", .5))
                            output.meta.update(pipeline_version=model_cfg.get("pipeline_version"),
                                               geometry_threshold=params.threshold,
                                               road_geometry_threshold=float(model_cfg.get("road_geometry_threshold", .5)))
                        geo = extract_lanes(output.lane, params)
                        if name == "sam3" and output.road is not None:
                            geo["road_edges"] = road_edges_from_probability(
                                output.road, horizon_y=params.horizon_y,
                                confidence_threshold=float(model_cfg.get("road_geometry_threshold", .5)),
                                occlusion_mask=output.extra.get("occlusion"))
                            geo["road_edge_proposal_kind"] = "model_road_contour_requires_human_review"
                        geo.update(image_id=row["image_id"], teacher=name, teacher_version=version,
                                   source_guided=name == "sam2_large", model_meta=output.meta)
                        if reg.image(row["image_id"])["locked"]:
                            preserved_review_ids.add(row["image_id"])
                            model_info["human_reviews_preserved"] = len(preserved_review_ids)
                        paths = save_output(settings, name, version, row["image_id"], output)
                        reg.add_annotation(row["image_id"], "teacher", name, version, paths["_meta"], license_ids=[row["license_id"], "sam3" if name == "sam3" else "sam2"],
                                           checkpoint_sha=model_cfg["revision"], params={"source_guided": name == "sam2_large", "license_sha256": model_info["license_sha256"], "candidate_only": True})
                        atomic_json(settings.path("lane_geometry", "teachers", name, row["image_id"] + ".json"), geo)
                        model_info.update(last_image=row["image_id"], last_seconds=round(time.monotonic() - started, 2), peak_vram_gib=round(torch.cuda.max_memory_allocated() / 2**30, 2))
                        record_completed(model_info, geo, output.meta)
                    except torch.cuda.OutOfMemoryError:
                        # Never silently replace a large teacher with a small one.
                        raise RuntimeError(f"{name} exceeded 24-GB GPU memory; inspect the recorded native-resolution settings before retrying")
                    except Exception as exc:
                        model_info["failed"] += 1
                        model_info["last_error"] = type(exc).__name__ + ": " + str(exc)[:250]
                        log.exception("Candidate inference failed: %s / %s", name, row["image_id"])
                    finally:
                        if not forwards_recorded:
                            record_run_forwards(model_info, teacher.last_counts)
                    atomic_json(settings.path("reports", "model_status.json"), status)
                model_info["state"] = "complete" if not model_info["failed"] else "partial_error"
            except Exception as exc:
                model_info.update(state="error", error=type(exc).__name__ + ": " + str(exc)[:250])
                failures.append({"model": name, "reason": model_info["error"]})
                log.exception("Model unavailable: %s", name)
            finally:
                if loaded:
                    teacher.unload()
                del teacher
                gc.collect()
                torch.cuda.empty_cache()
            atomic_json(settings.path("reports", "model_status.json"), status)
        status.update(state="complete" if not failures and all(status["models"][n].get("state") == "complete" for n in names) else "needs_attention", failures=failures, finished=time.time())
        atomic_json(settings.path("reports", "model_status.json"), status)
        return status
    finally:
        reg.close()


def main():
    parser = argparse.ArgumentParser(description="Large annotation models on RTX 4090; never trains UFLD")
    parser.add_argument("command", choices=["inspect", "run"], default="inspect", nargs="?")
    parser.add_argument("--data-root")
    parser.add_argument("--teachers", default="sam3,sam2_large")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--split", choices=["train", "val", "test"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = local_settings(args.data_root)
    from filelock import FileLock, Timeout
    lock = FileLock(str(settings.path("logs", "large-models.lock")))
    if args.command == "inspect":
        try:
            with lock.acquire(timeout=0):
                result = inspect_models(settings)
        except Timeout:
            # A status check must not replace a live worker's progress report.
            result = json.loads(settings.path("reports", "model_status.json").read_text(encoding="utf-8"))
            result["inspection_notice"] = "An inference pass is running; showing its saved progress without changing it."
    else:
        try:
            with lock.acquire(timeout=0):
                result = run_candidates(settings, args.teachers.split(","), args.limit, args.split)
        except Timeout:
            raise SystemExit("Another large-model pass is already running for this dataset.")
    print(json.dumps(result, indent=2))
    if result["state"] in ("needs_attention", "cuda_setup_required"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
