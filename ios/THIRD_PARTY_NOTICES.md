# Third-party model

BrakeCheck bundles a converted **Ultralytics YOLO11n** detector in `Sources/BrakeCheck/Resources/yolo11n.mlpackage`.

- Upstream project: https://github.com/ultralytics/ultralytics
- Source weights: https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.pt
- Upstream licensing information: https://www.ultralytics.com/license
- Included upstream license text: `licenses/Ultralytics-AGPL-3.0.txt`

The model was converted using Ultralytics 8.4.175 and Apple's coremltools 9.0 with `format="coreml"`, `nms=True`, `imgsz=640`, `conf=0.4` and `iou=0.5`. Provenance and SHA-256 hashes are in `MODEL_INFO.json`. This conversion does not change the model's upstream licensing terms. Review those terms when distributing a product that incorporates it.

The iPhone app uses Apple's system frameworks SwiftUI, UIKit, Core Location, AVFoundation, Vision, Core ML and Core Motion. Those frameworks, xtool, the Swift toolchain and Xcode SDK files are not redistributed in this ZIP. There is no Python runtime dependency in the app.
