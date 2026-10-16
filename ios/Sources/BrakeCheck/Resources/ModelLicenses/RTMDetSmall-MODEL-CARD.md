---
license: apache-2.0
library_name: libreyolo
tags:
  - object-detection
  - rtmdet
  - mmdetection
---

# LibreRTMDets

RTMDet-S COCO detector, repackaged for the [LibreYOLO](https://github.com/LibreYOLO/libreyolo) framework.

## Source

Derived from [https://github.com/open-mmlab/mmdetection](https://github.com/open-mmlab/mmdetection) at commit `cfd5d3a985b0249de009b67d04f37263e11cdf3d` and upstream checkpoint:
`https://download.openmmlab.com/mmdetection/v3.0/rtmdet/rtmdet_s_8xb32-300e_coco/rtmdet_s_8xb32-300e_coco_20220905_161602-387a891e.pth`.

Copyright (c) 2018-2023 OpenMMLab. Licensed under the Apache License, Version 2.0.

## Modifications

EMA weights were selected from the upstream checkpoint. `data_preprocessor.*` and batch-tracking buffers were omitted, `bbox_head.` keys were renamed to `head.`, and the loaded state dict was saved with LibreYOLO checkpoint metadata schema v1.0. Learned model parameters are otherwise preserved.

## Validation

This file was validated locally with LibreYOLO checkpoint metadata schema v1.0, loaded through `LibreYOLO`, and evaluated on `coco128.yaml` with `workers=0`, `batch=8`, `conf=0.001`, `iou=0.6`.

| Metric | Value |
|---|---:|
| COCO128 mAP50-95 | 0.5261 |
| COCO128 mAP50 | 0.7070 |
| SHA256 | `e352310fc6a8a48b862afbbeaf5ede67002fcdb4a488f87633e2a913a9ccaa85` |
| File size | 34.2 MB |

## License

Apache License 2.0. See the [`LICENSE`](./LICENSE) and [`NOTICE`](./NOTICE) files in this repository.
