# BrakeCheck third-party models

Camera navigation bundles locally trained UFLDv2 ResNet34 lane detection and NVIDIA
SegFormer B0/Cityscapes road segmentation. The SegFormer checkpoint is for
non-commercial research and evaluation under the bundled NVIDIA license.
Checkpoint sources, preprocessing and file hashes accompany the model packages.

UFLDv2 architecture code is MIT. N66 uses the epoch-3 post-label-fix checkpoint trained on
the frozen BrakeCheck reviewed export, initialized through local checkpoints
from a torchvision ImageNet ResNet34 backbone. The previous CULane-trained
UFLD checkpoint is excluded from this build. Dataset and initialization
provenance remain recorded with the training run; this is not a blanket
commercial clearance. SegFormer's non-commercial terms remain applicable.

UFLDv2 upstream: https://github.com/cfzd/Ultra-Fast-Lane-Detection-v2
SegFormer: https://huggingface.co/nvidia/segformer-b0-finetuned-cityscapes-1024-1024
Day: RTMDet-tiny COCO. Low light: RTMDet-small COCO. These are general COCO
checkpoints; the low-light profile is not a night-trained model.

Copyright (c) 2018-2023 OpenMMLab. Licensed under Apache License 2.0.
Architecture and weights originate from MMDetection, repackaged by LibreYOLO.
LibreYOLO framework copyright holders license its framework under MIT; the
ported RTMDet architecture and designated RTMDet weights remain Apache-2.0.
Included Apache-2.0.txt, LibreYOLO-MIT.txt and upstream notices retain attribution.
The broad LibreYOLO notices describe other models; those models are not included
in BrakeCheck. Only RTMDet-tiny and RTMDet-small are used for vehicle detection.

BrakeCheck modifications: conversion to Core ML FP16; integrated RGB-to-BGR
mean/std normalization; normalized center/size boxes; Apple's per-class NMS
pipeline with the 80 COCO labels; Swift Vision integration and model caching.
No Ultralytics implementation or weights are included in these detector graphs.
The old Ultralytics resource directory remains in the development workspace for
comparison but is excluded from the N31 Swift package and IPA.

Upstream:
https://github.com/open-mmlab/mmdetection
https://github.com/LibreYOLO/libreyolo
https://huggingface.co/LibreYOLO/LibreRTMDett
https://huggingface.co/LibreYOLO/LibreRTMDets

See the bundled model cards and provenance JSON for the checkpoint and converted
resource hashes. Phone inference, power use and labeled night-driving accuracy
must still be validated; commercial permissions do not establish performance.
