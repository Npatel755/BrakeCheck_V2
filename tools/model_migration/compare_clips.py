"""Reproducible clip screening, not an accuracy or iPhone performance claim.

Benchmarks CPU PyTorch latency and saves vehicle detections/contact sheets.
Existing screen recordings contain captions and old boxes; they are not ground truth.
"""
from pathlib import Path
import json
import time
import argparse
import cv2
import numpy as np
import torch
from torchvision.ops import batched_nms
from PIL import Image, ImageDraw
from libreyolo import LibreYOLO
from export_rtmdet import VisionRTMDet
from ultralytics import YOLO
from rfdetr import RFDETRNano

VEHICLES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}


def main(video, output):
    torch.set_num_threads(4)
    output.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video))
    frames = []
    # Day, close lead, dark-looking recording segments, and intersections.
    # User confirmed neither supplied clip is night-driving validation footage.
    for second in (7, 13, 21, 81, 121, 131, 141, 151, 181, 201, 211, 215, 219, 221, 225, 231):
        cap.set(cv2.CAP_PROP_POS_MSEC, second * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        # Screen recording: remove the phone's black side gutters. Captions
        # and old overlay boxes remain; retain this limitation in the report.
        w = rgb.shape[1]
        rgb = rgb[:, int(w * .083):int(w * .915)]
        image = Image.fromarray(rgb)
        scale = min(640 / image.width, 640 / image.height)
        resized = image.resize((round(image.width * scale), round(image.height * scale)))
        canvas = Image.new("RGB", (640, 640), "black")
        canvas.paste(resized, ((640 - resized.width) // 2, (640 - resized.height) // 2))
        tensor = torch.from_numpy(np.asarray(canvas).copy()).permute(2, 0, 1).float().unsqueeze(0)
        frames.append((second, canvas, tensor))
    cap.release()
    report = {"video": str(video), "runtime": "CPU PyTorch, four threads; not iPhone Core ML",
              "limitations": "Unannotated screen recording with old boxes/captions; counts do not measure recall or false positives.", "models": {}}
    for size in ("t", "s"):
        wrapper = VisionRTMDet(LibreYOLO(f"LibreRTMDet{size}.pt", device="cpu").model).eval()
        with torch.no_grad():
            for _ in range(3): wrapper(frames[0][2])
        elapsed, results, tiles = [], [], []
        for second, canvas, tensor in frames:
            start = time.perf_counter()
            with torch.no_grad(): scores, coordinates = wrapper(tensor)
            elapsed.append((time.perf_counter() - start) * 1000)
            confidence, labels = scores.max(dim=1)
            mask = (confidence >= .28) & torch.isin(labels, torch.tensor(list(VEHICLES)))
            coords, confidence, labels = coordinates[mask], confidence[mask], labels[mask]
            xyxy = torch.cat((coords[:, :2] - coords[:, 2:] / 2, coords[:, :2] + coords[:, 2:] / 2), dim=1) * 640
            keep = batched_nms(xyxy, confidence, labels, .5)
            detections = [{"label": VEHICLES[int(labels[k])], "confidence": float(confidence[k]), "xyxy": xyxy[k].tolist()} for k in keep]
            results.append({"second": second, "detections": detections})
            tile = canvas.copy()
            draw = ImageDraw.Draw(tile)
            for d in detections:
                draw.rectangle(d["xyxy"], outline="cyan", width=2)
                draw.text((d["xyxy"][0], d["xyxy"][1]), f'{d["label"]} {d["confidence"]:.2f}', fill="cyan")
            draw.text((8, 8), f"RTMDet-{size} {second}s", fill="white")
            tile.thumbnail((320, 320))
            tiles.append(tile)
        sheet = Image.new("RGB", (1280, 320 * ((len(tiles) + 3) // 4)), "black")
        for i, tile in enumerate(tiles): sheet.paste(tile, ((i % 4) * 320, (i // 4) * 320))
        sheet.save(output / f"rtmdet-{size}-contact-sheet.jpg")
        report["models"][f"rtmdet-{size}"] = {"median_cpu_ms": float(np.median(elapsed)), "p95_cpu_ms": float(np.percentile(elapsed, 95)), "frames": results}
    # Reference/challenger use their public end-to-end Python APIs. Latencies
    # include their preprocessing and NMS and are reported separately.
    for name, model in (("yolo11n-reference", YOLO("yolo11n.pt")),
                        ("rf-detr-nano-challenger", RFDETRNano())):
        elapsed, results, tiles = [], [], []
        for second, canvas, tensor in frames:
            start = time.perf_counter()
            if name.startswith("yolo"):
                result = model.predict(canvas, device="cpu", conf=.28, iou=.5, verbose=False)[0]
                detections = [{"label": VEHICLES[int(b.cls)], "confidence": float(b.conf), "xyxy": b.xyxy[0].tolist()}
                              for b in result.boxes if int(b.cls) in VEHICLES]
            else:
                result = model.predict(canvas, threshold=.28)
                # RF-DETR's checkpoint uses sparse COCO category IDs.
                sparse = {3: "car", 4: "motorcycle", 6: "bus", 8: "truck"}
                detections = [{"label": sparse[int(label)], "confidence": float(conf), "xyxy": box.tolist()}
                              for box, conf, label in zip(result.xyxy, result.confidence, result.class_id)
                              if int(label) in sparse]
            elapsed.append((time.perf_counter() - start) * 1000)
            results.append({"second": second, "detections": detections})
            tile = canvas.copy()
            draw = ImageDraw.Draw(tile)
            for d in detections:
                draw.rectangle(d["xyxy"], outline="cyan", width=2)
                draw.text((d["xyxy"][0], d["xyxy"][1]), f'{d["label"]} {d["confidence"]:.2f}', fill="cyan")
            draw.text((8, 8), f"{name} {second}s", fill="white")
            tile.thumbnail((320, 320))
            tiles.append(tile)
        sheet = Image.new("RGB", (1280, 320 * ((len(tiles) + 3) // 4)), "black")
        for i, tile in enumerate(tiles): sheet.paste(tile, ((i % 4) * 320, (i // 4) * 320))
        sheet.save(output / f"{name}-contact-sheet.jpg")
        report["models"][name] = {"median_cpu_end_to_end_ms": float(np.median(elapsed[1:])),
                                  "p95_cpu_end_to_end_ms": float(np.percentile(elapsed[1:], 95)), "frames": results}
    (output / "screening.json").write_text(json.dumps(report, indent=2))
    print({name: {k: v for k, v in data.items() if k != "frames"} for name, data in report["models"].items()})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    main(args.video, args.output)
