"""Run a pinned, licensed semantic teacher without promoting predictions to labels."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time

import numpy as np
from PIL import Image
import requests

from .pipeline import setup


MODEL_NAME = "comma10k-segnet"


def fetch_comma_model(project: Path) -> dict:
    """Fetch only the reviewed, pinned MIT checkpoint and its configuration."""
    entry = json.loads((project / "config/models.json").read_text(encoding="utf-8"))[MODEL_NAME]
    if entry["license_status"] != "cleared":
        raise PermissionError("Model is not cleared")
    destination = project / "models/comma10k-segnet"
    destination.mkdir(parents=True, exist_ok=True)
    base = f"https://huggingface.co/commaai/comma10k-segnet/resolve/{entry['checkpoint_revision']}"
    checkpoint_file = destination / "model.safetensors"
    if not checkpoint_file.is_file() or hashlib.sha256(checkpoint_file.read_bytes()).hexdigest() != entry["checkpoint_sha256"]:
        temporary = checkpoint_file.with_suffix(".part")
        try:
            with requests.get(f"{base}/model.safetensors", stream=True, timeout=(15, 90)) as response:
                response.raise_for_status()
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(1 << 20):
                        if chunk:
                            output.write(chunk)
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != entry["checkpoint_sha256"]:
                raise ValueError("Downloaded checkpoint hash mismatch")
            os.replace(temporary, checkpoint_file)
        finally:
            temporary.unlink(missing_ok=True)
    for name in ("config.json", "README.md"):
        target = destination / name
        if not target.exists():
            response = requests.get(f"{base}/{name}", timeout=45)
            response.raise_for_status()
            target.write_bytes(response.content)
    config = json.loads((destination / "config.json").read_text(encoding="utf-8"))
    if config.get("classes") != 5:
        raise ValueError("Unexpected model class count")
    if "license: mit" not in (destination / "README.md").read_text(encoding="utf-8").lower():
        raise ValueError("Pinned model card no longer declares MIT")
    shutil.copyfile(project / "config/COMMA-MIT-NOTICE.txt", project / entry["local_notice"])
    return {"model": MODEL_NAME, "revision": entry["checkpoint_revision"],
            "sha256": entry["checkpoint_sha256"], "destination": str(destination)}


def checkpoint(project: Path) -> tuple[Path, str]:
    registry = json.loads((project / "config/models.json").read_text(encoding="utf-8"))
    entry = registry[MODEL_NAME]
    if entry["license_status"] != "cleared":
        raise PermissionError(f"{MODEL_NAME} has not been cleared")
    source = project / "models/comma10k-segnet/model.safetensors"
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    if digest.hexdigest() != entry["checkpoint_sha256"]:
        raise ValueError("Checkpoint hash differs from reviewed model registry")
    if not (project / entry["local_notice"]).is_file():
        raise ValueError("Checkpoint license notice is missing")
    return source, digest.hexdigest()


def annotate_comma10k(project: Path, root: Path, limit: int = 100,
                      device: str = "cpu") -> dict:
    if limit < 1 or device not in ("cpu", "cuda"):
        raise ValueError("Invalid inference budget or device")
    checkpoint_path, checksum = checkpoint(project)
    import torch
    import segmentation_models_pytorch as smp
    from safetensors.torch import load_file

    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is unavailable in this Python environment")
    torch.set_num_threads(4)
    config = json.loads((checkpoint_path.parent / "config.json").read_text(encoding="utf-8"))
    config.pop("_model_class", None)
    model = smp.Unet(**config).eval().to(device)
    model.load_state_dict(load_file(checkpoint_path, device=device), strict=True)
    db = setup(root)
    images = db.execute("SELECT image_id,image_path FROM images WHERE source='comma10k' ORDER BY image_id LIMIT ?",
                        (limit,)).fetchall()
    start = time.monotonic()
    created = skipped = 0
    for image_id, relative_image in images:
        lane_path = root / "annotations/teacher_predictions/comma10k-segnet" / f"{image_id}-lane.png"
        road_path = root / "annotations/teacher_predictions/comma10k-segnet" / f"{image_id}-road.png"
        overlay_path = root / "annotations/teacher_predictions/comma10k-segnet" / f"{image_id}-overlay.jpg"
        existing = db.execute("SELECT 1 FROM predictions WHERE image_id=? AND model_name=? AND checkpoint_sha256=?",
                              (image_id, MODEL_NAME, checksum)).fetchone()
        if existing and lane_path.is_file() and road_path.is_file() and overlay_path.is_file():
            skipped += 1
            continue
        with Image.open(root / relative_image) as original:
            source = original.convert("RGB")
        resized = source.resize((512, 384), Image.Resampling.BILINEAR)
        pixels = np.asarray(resized).copy()
        tensor = torch.from_numpy(pixels).permute(2, 0, 1).unsqueeze(0).float().to(device)
        with torch.inference_mode():
            if device == "cuda":
                with torch.autocast("cuda", dtype=torch.float16):
                    labels = model(tensor).argmax(1)[0].cpu().numpy().astype(np.uint8)
            else:
                labels = model(tensor).argmax(1)[0].numpy().astype(np.uint8)
        lane = labels == 1
        road = labels == 0
        lane_path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(lane.astype(np.uint8) * 255).resize(source.size, Image.Resampling.NEAREST).save(lane_path)
        Image.fromarray(road.astype(np.uint8) * 255).resize(source.size, Image.Resampling.NEAREST).save(road_path)
        overlay = pixels.copy()
        overlay[road] = (overlay[road] * .78 + np.array([0, 200, 255]) * .22).astype(np.uint8)
        overlay[lane] = (overlay[lane] * .25 + np.array([255, 0, 255]) * .75).astype(np.uint8)
        Image.fromarray(overlay).save(overlay_path, quality=90)
        db.execute("""INSERT INTO predictions
            (image_id,model_name,checkpoint_sha256,lane_mask_path,road_mask_path,lane_pixels,road_pixels,status)
            VALUES (?,?,?,?,?,?,?,'provisional')
            ON CONFLICT(image_id,model_name,checkpoint_sha256) DO UPDATE SET
              lane_mask_path=excluded.lane_mask_path, road_mask_path=excluded.road_mask_path,
              lane_pixels=excluded.lane_pixels, road_pixels=excluded.road_pixels""",
            (image_id, MODEL_NAME, checksum, lane_path.relative_to(root).as_posix(),
             road_path.relative_to(root).as_posix(), int(lane.sum()), int(road.sum())))
        db.commit()
        created += 1
        if created % 10 == 0:
            print(f"Generated {created} provisional masks", flush=True)
    result = {"images_considered": len(images), "predictions_created": created,
              "predictions_skipped": skipped, "checkpoint_sha256": checksum,
              "seconds": round(time.monotonic() - start, 2), "device": device,
              "status": "provisional; not human-reviewed; source overlap with checkpoint training data"}
    (root / "reports/comma-baseline.json").write_text(json.dumps(result, indent=2) + "\n")
    db.close()
    return result
