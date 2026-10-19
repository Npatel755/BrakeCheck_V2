---
license: apache-2.0
library_name: libreyolo
tags:
  - object-detection
  - rtmdet
  - mmdetection
---

# LibreRTMDett

RTMDet-Tiny COCO detector, repackaged for the [LibreYOLO](https://github.com/LibreYOLO/libreyolo) framework.

## Source

Derived from [https://github.com/open-mmlab/mmdetection](https://github.com/open-mmlab/mmdetection) at commit `cfd5d3a985b0249de009b67d04f37263e11cdf3d` and upstream checkpoint:
`https://download.openmmlab.com/mmdetection/v3.0/rtmdet/rtmdet_tiny_8xb32-300e_coco/rtmdet_tiny_8xb32-300e_coco_20220902_112414-78e30dcc.pth`.

Copyright (c) 2018-2023 OpenMMLab. Licensed under the Apache License, Version 2.0.

## Modifications

EMA weights were selected from the upstream checkpoint. `data_preprocessor.*` and batch-tracking buffers were omitted, `bbox_head.` keys were renamed to `head.`, and the loaded state dict was saved with LibreYOLO checkpoint metadata schema v1.0. Learned model parameters are otherwise preserved.

## Validation

This file was validated locally with LibreYOLO checkpoint metadata schema v1.0, loaded through `LibreYOLO`, and evaluated on `coco128.yaml` with `workers=0`, `batch=8`, `conf=0.001`, `iou=0.6`.

| Metric | Value |
|---|---:|
| COCO128 mAP50-95 | 0.5127 |
| COCO128 mAP50 | 0.6802 |
| SHA256 | `79b8a0b1e22b100e36a5591b24a1a97eae2fc3f29ad73cb4809d8c9c6cd19a52` |
| File size | 18.9 MB |

## License

Apache License 2.0. See the [`LICENSE`](./LICENSE) and [`NOTICE`](./NOTICE) files in this repository.
