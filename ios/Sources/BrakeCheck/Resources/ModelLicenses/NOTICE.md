# BrakeCheck third-party models

Every model bundled in the app ships under terms that permit commercial
redistribution. Checkpoint sources, preprocessing and file hashes accompany each
model package; this file summarizes them.

## Lane detection — UFLDv2 ResNet34 (MIT architecture, locally trained weights)

UFLDv2 architecture code is MIT (`UFLD-MIT.txt`). The bundled checkpoint is the
epoch-3 post-label-fix run trained on the frozen BrakeCheck reviewed export
(commercial pool: comma10k, MIT — `Comma10k-MIT.txt`), initialized from a
torchvision ImageNet ResNet34 backbone. The previous CULane-trained checkpoint is
not included. Dataset and initialization provenance are recorded in
`LaneModelData/UFLDv2Lane-provenance.json`; confirm the training-pool records
before each release.

UFLDv2 upstream: https://github.com/cfzd/Ultra-Fast-Lane-Detection-v2

## Road segmentation — Intel road-segmentation-adas-0001 (Apache-2.0)

Intel Open Model Zoo `road-segmentation-adas-0001`, converted to Core ML with
unchanged learned parameters. See `IntelRoad-MODEL-CARD.md` for source hashes
and the conversion wrapper, and `Apache-2.0.txt` for the licence.
Copyright (c) 2023-2024 Intel Corporation. This replaces the earlier NVIDIA
SegFormer package, which was non-commercial and is no longer distributed.

## Vehicle detection — RTMDet-tiny (day) and RTMDet-small (low light), Apache-2.0

These are general COCO checkpoints; the low-light profile is not a night-trained
model. Copyright (c) 2018-2023 OpenMMLab. Architecture and weights originate from
MMDetection, repackaged by LibreYOLO. LibreYOLO licenses its framework under MIT;
the ported RTMDet architecture and designated RTMDet weights remain Apache-2.0.
`Apache-2.0.txt`, `LibreYOLO-MIT.txt` and upstream notices retain attribution. The
broad LibreYOLO notices describe other models; those models are not included.

BrakeCheck modifications: conversion to Core ML FP16; integrated RGB-to-BGR
mean/std normalization; normalized center/size boxes; Apple's per-class NMS
pipeline with the 80 COCO labels; Swift Vision integration and model caching.
No Ultralytics implementation or weights are included anywhere in the app or
repository.

Upstream:
https://github.com/open-mmlab/mmdetection
https://github.com/LibreYOLO/libreyolo
https://huggingface.co/LibreYOLO/LibreRTMDett
https://huggingface.co/LibreYOLO/LibreRTMDets

## Vehicle orientation — CFV viewpoint classifier (Apache-2.0)

See `ViewpointModelData/CFV-LICENSE.txt`.

See the bundled model cards and provenance JSON for the checkpoint and converted
resource hashes. Phone inference, power use and labeled accuracy must still be
validated; commercial permissions do not establish performance.
