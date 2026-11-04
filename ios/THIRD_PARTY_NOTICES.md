# Third-party models

N31 bundles converted **RTMDet-tiny** and **RTMDet-small** COCO detectors under Apache-2.0 in `Sources/BrakeCheck/Resources/DetectorModelData`. The source checkpoint cards, conversion hashes and Apache/MIT notices ship in `Resources/ModelLicenses`. See `N31-MODEL-MIGRATION.md` for provenance and validation limits. CFV viewpoint inference retains its existing license.

Camera navigation bundles **UFLDv2 ResNet34 trained on the reviewed local BrakeCheck lane dataset** and the **Intel `road-segmentation-adas-0001`** road model (Apache-2.0, Open Model Zoo). UFLDv2's architecture code is MIT. The lane checkpoint descends from ImageNet ResNet34 initialization and local training on the comma10k (MIT) commercial pool; the old CULane-trained UFLD ResNet18 checkpoint and the NVIDIA SegFormer road model (non-commercial) are no longer distributed. See `Resources/ModelLicenses/IntelRoad-MODEL-CARD.md` for the road model's source hashes and conversion.

The lane checkpoint SHA256, frozen training-data manifest, converted package hashes, preprocessing and CPU eager/TorchScript parity evidence accompany `Resources/LaneModelData/UFLDv2Lane-provenance.json` and `manifest.json`. Local training and an MIT architecture do not by themselves establish commercial clearance for the training data; retain and assess each dataset's provenance before distribution. See `N62-UFLDV2.md` and `AR-NAVIGATION.md` for the provisional validation result and the Apple execution/device-performance checks still required.

## Historical prototype detectors (removed)

Earlier prototype builds bundled converted **Ultralytics YOLO11n** and a traffic-trained **YOLOv8s** detector (AGPL-3.0). Those resource files and the Ultralytics licence text have been removed from the repository; no Ultralytics code or weights remain in the app or its history-independent sources. `MODEL_INFO.json` is retained only as a historical provenance record.

The iPhone app uses Apple's system frameworks SwiftUI, UIKit, Core Location, AVFoundation, Vision, Core ML and Core Motion. Those frameworks, xtool, the Swift toolchain and Xcode SDK files are not redistributed in this ZIP. There is no Python runtime dependency in the app.

## Fonts

- Red Hat Display (Light, Regular, Medium, SemiBold, Bold, ExtraBold, Black), Copyright 2021 The Red Hat Project Authors.
- License: SIL Open Font License 1.1. Text bundled at `Sources/BrakeCheck/Resources/Fonts/OFL.txt`.
- Source: https://github.com/RedHatOfficial/RedHatFont (`fonts/Proportional/RedHatDisplay/ttf`), unmodified.
