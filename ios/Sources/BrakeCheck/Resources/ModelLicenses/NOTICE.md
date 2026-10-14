# BrakeCheck RTMDet detectors

Camera navigation additionally bundles UFLD ResNet18/CULane (MIT) and NVIDIA
SegFormer B0/Cityscapes (custom NVIDIA SegFormer license: non-commercial
research/evaluation only). Full upstream notices and SegFormer's checkpoint card
are included as UFLD-MIT.txt, SegFormer-NVIDIA-License.txt and
SegFormer-MODEL-CARD.md. The converted FP16 road model remains subject to its
original use restriction. Hugging Face Transformers' Apache-2.0 implementation
license does not relicense NVIDIA's pretrained weights. BrakeCheck modifications
embed RGB ImageNet normalization; expose UFLD lane logits; and expose SegFormer
softmax road/non-road probabilities restored to 640×384. Checkpoint revisions,
sources, preprocessing and file hashes accompany the model packages.

UFLD upstream: https://github.com/cfzd/Ultra-Fast-Lane-Detection
SegFormer checkpoint: https://huggingface.co/nvidia/segformer-b0-finetuned-cityscapes-1024-1024
SegFormer license: https://github.com/NVlabs/SegFormer/blob/master/LICENSE

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
