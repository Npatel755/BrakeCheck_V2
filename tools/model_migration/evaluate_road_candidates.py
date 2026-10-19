"""Compare road-mask candidates on identical frames from BrakeCheck recordings.

Outputs masks, latency, and frame-to-frame road-area variation. The recordings
have no ground-truth labels, so this is a diagnostic comparison, not an IoU test.
"""

import argparse
import hashlib
import json
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np
import openvino as ov
import torch
from transformers import SegformerForSemanticSegmentation


INTEL_BASE = "https://storage.openvinotoolkit.org/repositories/open_model_zoo/2023.0/models_bin/1/road-segmentation-adas-0001/FP32/road-segmentation-adas-0001"
INTEL_SHA256 = {
    ".xml": "76248abe41e6cc1e4286c21d1d2447a73d29dc86d7343056d37dccefccee65793ab7b3a9bfeda4f17c89c2978112e1ee",
    ".bin": "00391dd817115d2b076cade11062a4ff8c70659eb629d30b4babae3b6888f7c180e7ef306e8e5f14fde33f0bb23e1207",
}
SEGFORMER = "nvidia/segformer-b0-finetuned-cityscapes-1024-1024"
REVISION = "21b3847fae21ddee674abd31129307b6a1235bd9"


def intel_model(cache: Path):
    cache.mkdir(parents=True, exist_ok=True)
    for suffix, digest in INTEL_SHA256.items():
        target = cache / f"road-segmentation-adas-0001{suffix}"
        if not target.exists():
            urllib.request.urlretrieve(INTEL_BASE + suffix, target)
        if hashlib.sha384(target.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Intel model checksum mismatch: {target}")
    return ov.Core().compile_model(str(cache / "road-segmentation-adas-0001.xml"), "CPU")


def frames_from_video(path: Path, count: int, crop: str):
    cap = cv2.VideoCapture(str(path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total <= 0:
        raise ValueError(f"Cannot decode {path}")
    for index in np.linspace(total * .08, total * .92, count, dtype=int):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(index))
        ok, frame = cap.read()
        if not ok:
            continue
        if crop == "landscape-app":
            # The camera preview in v19/V20 occupies the center of the screen;
            # exclude the surrounding HUD and bottom tab strip.
            h, w = frame.shape[:2]
            frame = frame[:int(h * .82), int(w * .16):int(w * .80)]
        yield int(index), frame
    cap.release()


def segformer_road(model, bgr):
    rgb = cv2.cvtColor(cv2.resize(bgr, (640, 384)), cv2.COLOR_BGR2RGB)
    image = torch.from_numpy(rgb.copy()).permute(2, 0, 1).unsqueeze(0).float()
    mean = torch.tensor([.485, .456, .406]).reshape(1, 3, 1, 1)
    std = torch.tensor([.229, .224, .225]).reshape(1, 3, 1, 1)
    with torch.inference_mode():
        logits = model((image / 255 - mean) / std).logits
        road = torch.nn.functional.interpolate(logits, (384, 640), mode="bilinear", align_corners=False).softmax(1)[0, 0]
    return road.numpy()


def intel_road(model, bgr):
    image = cv2.resize(bgr, (896, 512)).transpose(2, 0, 1)[None].astype(np.float32)
    output = model([image])[model.output(0)][0]
    # Intel output is BG, road, curb, mark probabilities. Include markings in
    # traversable road surface, but leave curbs out.
    road = output[1] + output[3]
    return cv2.resize(road, (640, 384))


def overlay(bgr, road):
    frame = cv2.resize(bgr, (640, 384))
    tint = np.zeros_like(frame)
    tint[:, :, 0] = 255
    alpha = np.clip(road, 0, 1)[..., None] * .48
    return np.uint8(frame * (1 - alpha) + tint * alpha)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("videos", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--frames-per-video", type=int, default=8)
    parser.add_argument("--crop", choices=["landscape-app", "none"], default="landscape-app")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(4)
    intel = intel_model(args.output / "intel-cache")
    segformer = SegformerForSemanticSegmentation.from_pretrained(
        SEGFORMER, revision=REVISION, local_files_only=True).eval()
    assert segformer.config.id2label[0] == "road"
    records = []
    for video in args.videos:
        for index, bgr in frames_from_video(video, args.frames_per_video, args.crop):
            masks = {}
            timings = {}
            for name, predict in (("SegFormer", lambda: segformer_road(segformer, bgr)),
                                  ("Intel", lambda: intel_road(intel, bgr))):
                started = time.perf_counter()
                masks[name] = predict()
                timings[name] = time.perf_counter() - started
            top = cv2.resize(bgr, (640, 384))
            sheet = np.hstack([top, overlay(bgr, masks["SegFormer"]), overlay(bgr, masks["Intel"])])
            cv2.imwrite(str(args.output / f"{video.stem}_{index:06d}.jpg"), sheet)
            records.append({"video": video.name, "frame": index,
                            "segformer_seconds": timings["SegFormer"],
                            "intel_seconds": timings["Intel"],
                            "segformer_road_fraction": float(np.mean(masks["SegFormer"] > .5)),
                            "intel_road_fraction": float(np.mean(masks["Intel"] > .5)),
                            "mask_disagreement_fraction": float(np.mean((masks["SegFormer"] > .5) != (masks["Intel"] > .5)))})
            print(json.dumps(records[-1]), flush=True)
    (args.output / "metrics.json").write_text(json.dumps(records, indent=2) + "\n")


if __name__ == "__main__":
    main()
