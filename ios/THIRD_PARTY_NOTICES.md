# Third-party models

N31 bundles converted **RTMDet-tiny** and **RTMDet-small** COCO detectors under Apache-2.0 in `Sources/BrakeCheck/Resources/DetectorModelData`. The source checkpoint cards, conversion hashes and Apache/MIT notices ship in `Resources/ModelLicenses`. See `N31-MODEL-MIGRATION.md` for provenance and validation limits. CFV viewpoint inference retains its existing license.

Camera navigation replaces YOLOP with **UFLD ResNet18 trained on CULane** (MIT, cfzd/Ultra-Fast-Lane-Detection) and **SegFormer B0 trained on Cityscapes** (NVIDIA checkpoint, custom NVIDIA Source Code License for SegFormer, non-commercial research/evaluation only). The checkpoint card declares `license: other`; its upstream license restricts use in section 3.3. Hugging Face Transformers implementation is Apache-2.0, which does not relicense NVIDIA's pretrained weights. Both FP16 Core ML packages are in `Resources/LaneModelData`, alongside a manifest containing checkpoint provenance, model contracts and SHA256 hashes. Full upstream notices are bundled in `Resources/ModelLicenses/UFLD-MIT.txt` and `SegFormer-NVIDIA-License.txt`. The derived road model is subject to the original use restriction; a commercial release requires suitable rights or replacement weights. See `AR-NAVIGATION.md` for conversion and device-validation limits.

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
