# Third-party models

BrakeCheck bundles four machine-learning models. Provenance below comes from each model's embedded Core ML metadata and its upstream page, checked 2026-10-13.

| Model | Used for | Bundled at | Upstream | License |
|---|---|---|---|---|
| Ultralytics **YOLO11n** (COCO) | Daytime vehicle detection | `Sources/BrakeCheck/Resources/YOLOModelData/` | https://github.com/ultralytics/ultralytics (weights: `v8.4.0/yolo11n.pt`) | **AGPL-3.0** (`licenses/Ultralytics-AGPL-3.0.txt`) |
| **Night traffic YOLOv8s** | Low-light vehicle detection | `Sources/BrakeCheck/Resources/YOLOModelData/Night/` | https://huggingface.co/G2mus/night-vehicle-yolov8s (fine-tuned from Ultralytics `yolov8s.pt` on BDD100K) | Model card says MIT; the Core ML metadata and the Ultralytics base weights say **AGPL-3.0**. Treat as AGPL-3.0. Dataset: BDD100K (BSD-3-Clause). |
| **YOLOP** lane and drivable-road segmentation | Lane geometry | `Sources/BrakeCheck/Resources/LaneModelData/` | https://github.com/hustvl/YOLOP | MIT (`LaneModelData/YOLOP-LICENSE.txt`) |
| **CFV** ConvNeXt-Small car viewpoint classifier (Andy Catruna et al.) | Lead-vehicle heading | `Sources/BrakeCheck/Resources/ViewpointModelData/` | Upstream repository not located yet | Apache-2.0 per embedded metadata (`ViewpointModelData/CFV-LICENSE.txt`). Confirm the weights' and training data's terms with whoever converted it. |

## Distribution note

The two YOLO detectors are AGPL-3.0. Shipping them inside a closed-source app needs either an Ultralytics Enterprise license or releasing the app's source under AGPL-3.0. Replacing them with permissively licensed detectors also works. This is an open launch decision.

## Conversion details

YOLO11n was converted with Ultralytics 8.4.175 and coremltools 9.0 (`format="coreml"`, `nms=True`, `imgsz=640`, `conf=0.4`, `iou=0.5`). Hashes are in `MODEL_INFO.json`. The night detector was exported with Ultralytics 8.3.207. Conversion doesn't change any model's upstream license terms.

The iPhone app also uses Apple's system frameworks (SwiftUI, UIKit, Core Location, AVFoundation, Vision, Core ML, Core Motion). Those frameworks, xtool, the Swift toolchain and Xcode SDK files are not redistributed here.
