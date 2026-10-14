# Third-party models

N31 bundles converted **RTMDet-tiny** and **RTMDet-small** COCO detectors under Apache-2.0 in `Sources/BrakeCheck/Resources/DetectorModelData`. The source checkpoint cards, conversion hashes and Apache/MIT notices ship in `Resources/ModelLicenses`. See `N31-MODEL-MIGRATION.md` for provenance and validation limits. YOLOP lane inference and CFV viewpoint inference remain unchanged with their existing MIT/Apache notices.

## Historical prototype detectors (excluded from N31)

Earlier prototype builds bundled converted **Ultralytics YOLO11n** and a traffic-trained **YOLOv8s** detector. These old resource files remain in the development workspace and are excluded from the N31 IPA.

- Upstream project: https://github.com/ultralytics/ultralytics
- Source weights: https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.pt
- Upstream licensing information: https://www.ultralytics.com/license
- Included upstream license text: `licenses/Ultralytics-AGPL-3.0.txt`

The model was converted using Ultralytics 8.4.175 and Apple's coremltools 9.0 with `format="coreml"`, `nms=True`, `imgsz=640`, `conf=0.4` and `iou=0.5`. Provenance and SHA-256 hashes are in `MODEL_INFO.json`. This conversion does not change the model's upstream licensing terms. Review those terms when distributing a product that incorporates it.

The iPhone app uses Apple's system frameworks SwiftUI, UIKit, Core Location, AVFoundation, Vision, Core ML and Core Motion. Those frameworks, xtool, the Swift toolchain and Xcode SDK files are not redistributed in this ZIP. There is no Python runtime dependency in the app.
