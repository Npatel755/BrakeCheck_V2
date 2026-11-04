"""Independent CPU model comparisons for ZOD audit samples; inference only."""
import argparse
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import time

from filelock import FileLock

from .bootstrap import atomic_json
from .zod_mirror import connect, encoded, sha
from .zod_repair_batch import REPAIR


def shape_for_batch(geometry, version, batch):
    if batch.name in ('continuous-boundaries-v5', 'boundary-consolidation-v6'):
        from .lane_boundary_completion import complete_lane_paths
        geometry = complete_lane_paths(geometry)
        version += ':continuous-v5'
    if batch.name == 'boundary-consolidation-v6':
        from .lane_boundary_deduplication import consolidate_boundaries
        repaired = consolidate_boundaries(geometry)
        if repaired != geometry:
            version += ':physical-boundaries-v6'
        geometry = repaired
    geometry['teacher_version'] = version
    return geometry, version


def run(data_root, device="cpu"):
    if device == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    os.environ["HF_HUB_OFFLINE"] = "1"
    root = Path(data_root).resolve() / "experiments/zod_mirror"
    from .zod_active_batch import active_batch
    target = active_batch(root)
    if target == root:
        target = root / REPAIR  # Keep initial-import waiting for the first repair batch.
    with FileLock(str(target / ".model-proposals.lock"), timeout=0):
        while not (target / "repair_status.json").exists() or json.loads((target / "repair_status.json").read_text()).get("state") != "audit_ready":
            if (target / "repair_status.json").exists() and json.loads((target / "repair_status.json").read_text()).get("state") == "error":
                raise RuntimeError("Converter repair failed")
            time.sleep(2)
        import cv2
        import numpy as np
        import torch
        from .annotation_training import cached_model
        from .college_paint_proposals import observed_geometry
        from .geometry import GeoParams
        from .teachers.base import file_sha256
        torch.set_num_threads(2)
        cv2.setNumThreads(1)
        db = connect(target)
        db.execute("CREATE TABLE IF NOT EXISTS zod_model_proposals (image_id TEXT,model TEXT,version TEXT,geometry_json TEXT,geometry_sha TEXT,source_binding TEXT,PRIMARY KEY(image_id,model))")
        db.commit()
        priority = ["zod-000001", "zod-000002"]
        ids = priority + [row[0] for row in db.execute("SELECT image_id FROM zod_audit_members WHERE is_sample=1 ORDER BY image_id") if row[0] not in priority]
        status = {"state": "loading", "samples": len(ids), "total": 2 * len(ids), "done": 0,
                  "device": device, "source_guided": False, "human_decisions_created": 0,
                  "training_performed": False, "models": ["comma_segnet_fresh", "comma_segnet_adapted"], "failed": []}
        atomic_json(target / "proposal_status.json", status)
        base, provenance = cached_model()
        base.eval()
        checkpoint = Path(data_root) / "models/comma_segnet_adapted/run-20261018-233159/best.pt"
        checkpoint_sha = file_sha256(checkpoint)
        if checkpoint_sha != "77fe3e8cb22654e0fd4a0fc07ad9b9c9c5ebfff5671f9aa3dcdc83b40e203438":
            raise ValueError("Adapted checkpoint changed")
        state = torch.load(checkpoint, map_location="cpu", weights_only=True)
        adapted = deepcopy(base)
        adapted.load_state_dict(state["model"])
        adapted.eval()
        base.to(device)
        adapted.to(device)
        params = GeoParams(horizon_y=.36, threshold=.5, min_segment_rows=8,
                           dash_max_dx_px=30, dash_max_dslope=.9)
        models = [("comma_segnet_fresh", "Road-marking model · pretrained", base, [512, 384], provenance),
                  ("comma_segnet_adapted", "Road-marking model · locally trained", adapted,
                   [state["config"]["width"], state["config"]["height"]],
                   {"checkpoint_sha256": checkpoint_sha, "checkpoint": str(checkpoint),
                    "training_cohort_sha256": state["config"].get("cohort_sha256"), "license": "MIT"})]
        del state
        for image_id in ids:
            row = db.execute("SELECT native_sha,image_sha,image_path FROM frames WHERE image_id=?", (image_id,)).fetchone()
            binding = sha(encoded([row["native_sha"], row["image_sha"]]).encode())
            image = cv2.imread(str(root / row["image_path"]))
            if image is None or file_sha256(root / row["image_path"]) != row["image_sha"]:
                raise ValueError("Source photo changed")
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            for name, label, model, size, receipt in models:
                version = name + ":zod-audit-v1:" + sha(encoded([receipt, size, asdict(params)]).encode())[:16]
                cached = db.execute("SELECT * FROM zod_model_proposals WHERE image_id=? AND model=?", (image_id, name)).fetchone()
                if cached:
                    allowed = {version, version + ':continuous-v5', version + ':continuous-v5:physical-boundaries-v6'}
                    if cached["version"] not in allowed or cached["source_binding"] != binding or sha(cached["geometry_json"].encode()) != cached["geometry_sha"]:
                        raise ValueError("Cached proposal no longer matches recipe/source")
                    status["done"] += 1
                    continue
                resized = cv2.resize(image, tuple(size), interpolation=cv2.INTER_LINEAR)
                tensor = torch.from_numpy(resized).permute(2, 0, 1)[None].float().to(device)
                with torch.inference_mode():
                    probability = torch.softmax(model(tensor).float(), dim=1)[0].cpu().numpy()
                maps = np.stack([cv2.resize(p, (1280, 720)) for p in probability])
                geometry = observed_geometry(maps[1], np.clip(maps[0] + maps[1], 0, 1), maps[3], params)
                for lane in geometry["lanes"]:
                    lane.update(physical_evidence="independent_segmentation_model_unverified",
                                flags=["model_proposal_requires_sample_audit"])
                geometry.update(image_id=image_id, model_label=label, teacher_version=version,
                                source_guided=False, reference_inputs=[], model_receipt=receipt,
                                source_binding=binding, input_dimensions=size,
                                training_eligible=False, candidate_only=True)
                geometry, version = shape_for_batch(geometry, version, target)
                raw = encoded(geometry)
                db.execute("INSERT INTO zod_model_proposals VALUES(?,?,?,?,?,?)", (image_id, name, version, raw, sha(raw.encode()), binding))
                db.commit()
                status.update(state="running", done=status["done"] + 1, last_image=image_id)
                atomic_json(target / "proposal_status.json", status)
        status["state"] = "complete"
        atomic_json(target / "proposal_status.json", status)
        db.close()
        return status


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.data_root, args.device)))
    except Exception as error:
        from .zod_active_batch import active_batch
        target = active_batch(args.data_root.resolve() / 'experiments/zod_mirror')
        path = target / 'proposal_status.json'
        status = json.loads(path.read_text()) if path.exists() else {}
        status.update(state='error', error=str(error))
        atomic_json(path, status)
        raise
