# Third-party models

N31 bundles converted **RTMDet-tiny** and **RTMDet-small** COCO detectors under Apache-2.0 in `Sources/BrakeCheck/Resources/DetectorModelData`. The source checkpoint cards, conversion hashes and Apache/MIT notices ship in `Resources/ModelLicenses`. See `N31-MODEL-MIGRATION.md` for provenance and validation limits. CFV viewpoint inference retains its existing license.

Camera navigation bundles **UFLD ResNet18 trained on CULane** and Intel Open Model Zoo **road-segmentation-adas-0001**. Intel's model manifest links its pretrained road weights to the Apache-2.0 license; the bundled Core ML model is a test candidate pending iPhone accuracy and performance validation. The source, preprocessing, and SHA256 hashes are recorded in `Resources/LaneModelData/manifest.json`. UFLD code is MIT, but CULane's dataset terms restrict commercial use of its trained checkpoint. Replacing road segmentation does not clear the full AR pipeline for commercial distribution. See `AR-NAVIGATION.md` for conversion and device-validation limits.
## Historical prototype detectors (excluded from N31)

Earlier prototype builds bundled converted **Ultralytics YOLO11n** and a traffic-trained **YOLOv8s** detector. These old resource files remain in the development workspace and are excluded from the N31 IPA.

- Upstream project: https://github.com/ultralytics/ultralytics
- Source weights: https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.pt
- Upstream licensing information: https://www.ultralytics.com/license
- Included upstream license text: `licenses/Ultralytics-AGPL-3.0.txt`

The model was converted using Ultralytics 8.4.175 and Apple's coremltools 9.0 with `format="coreml"`, `nms=True`, `imgsz=640`, `conf=0.4` and `iou=0.5`. Provenance and SHA-256 hashes are in `MODEL_INFO.json`. This conversion does not change the model's upstream licensing terms. Review those terms when distributing a product that incorporates it.

The iPhone app uses Apple's system frameworks SwiftUI, UIKit, Core Location, AVFoundation, Vision, Core ML and Core Motion. Those frameworks, xtool, the Swift toolchain and Xcode SDK files are not redistributed in this ZIP. There is no Python runtime dependency in the app.

## Fonts

- Red Hat Display (Light, Regular, Medium, SemiBold, Bold, ExtraBold, Black), Copyright 2021 The Red Hat Project Authors.
- License: SIL Open Font License 1.1. Text bundled at `Sources/BrakeCheck/Resources/Fonts/OFL.txt`.
- Source: https://github.com/RedHatOfficial/RedHatFont (`fonts/Proportional/RedHatDisplay/ttf`), unmodified.
