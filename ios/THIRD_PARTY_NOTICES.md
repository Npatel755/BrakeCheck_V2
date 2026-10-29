# Third-party models

N31 bundles converted **RTMDet-tiny** and **RTMDet-small** COCO detectors under Apache-2.0 in `Sources/BrakeCheck/Resources/DetectorModelData`. The source checkpoint cards, conversion hashes and Apache/MIT notices ship in `Resources/ModelLicenses`. See `N31-MODEL-MIGRATION.md` for provenance and validation limits. CFV viewpoint inference retains its existing license.

N66 camera navigation bundles **UFLDv2 ResNet34 trained on the reviewed local BrakeCheck lane dataset** and the existing **NVIDIA SegFormer B0/Cityscapes** road model. UFLDv2's architecture code is MIT. This lane checkpoint descends from ImageNet ResNet34 initialization and local training; the old CULane-trained UFLD ResNet18 checkpoint and Intel road model are excluded from the N66 resources and IPA. The separate SegFormer checkpoint remains subject to the bundled NVIDIA non-commercial research/evaluation license.

The lane checkpoint SHA256, frozen training-data manifest, converted package hashes, preprocessing and CPU eager/TorchScript parity evidence accompany `Resources/LaneModelData/UFLDv2Lane-provenance.json` and `manifest.json`. Local training and an MIT architecture do not establish commercial clearance for the training data or the entire application. Retain and assess each dataset's provenance and applicable permissions before distribution. See `N62-UFLDV2.md` and `AR-NAVIGATION.md` for the provisional validation result and the Apple execution/device-performance checks still required.
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
