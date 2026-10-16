# BrakeCheck third-party models

Camera navigation in this test build bundles comma.ai's `comma10k-segnet`
semantic segmentation checkpoint for lane-marking pixels and Intel Open Model
Zoo's `road-segmentation-adas-0001` for road pixels. The former is designated
MIT by its publisher and was trained on comma10k, whose images and masks are
published under MIT. Intel Open Model Zoo identifies the latter with
Apache-2.0. Both converted Core ML packages retain source and hash records.

Neither checkpoint has yet passed on-device lane, road, AR or thermal
validation. Lane-marking segmentation does not independently identify the
correct driving corridor. This build is for evaluation, not production driving.

comma.ai checkpoint: https://huggingface.co/commaai/comma10k-segnet
comma10k dataset: https://github.com/commaai/comma10k
Intel checkpoint: https://github.com/openvinotoolkit/open_model_zoo/tree/master/models/intel/road-segmentation-adas-0001
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
