# IntelRoad — road-segmentation-adas-0001 (Apache-2.0)

Bundled road-surface segmentation used for AR corridor display, road-supported
lead selection and camera-aligned navigation guidance. It replaces the earlier
NVIDIA SegFormer B0/Cityscapes package, whose licence was non-commercial.

## Source

- Model: `road-segmentation-adas-0001`, Intel Open Model Zoo 2023.0
- Card: https://github.com/openvinotoolkit/open_model_zoo/tree/master/models/intel/road-segmentation-adas-0001
- Licence: Apache License 2.0 (`Apache-2.0.txt` in this folder);
  https://github.com/openvinotoolkit/open_model_zoo/blob/master/LICENSE
- Copyright (c) 2023-2024 Intel Corporation.
- Architecture: ENet-style multiclass segmentation (background, road, curb,
  marking) with depthwise convolutions; input `[1,3,512,896]` BGR.
- Source files (FP32 OpenVINO IR), verified against the SHA-384 checksums in the
  upstream `model.yml`:
  - `road-segmentation-adas-0001.xml` —
    sha384 `76248abe41e6cc1e4286c21d1d2447a73d29dc86d7343056d37dccefccee65793ab7b3a9bfeda4f17c89c2978112e1ee`,
    sha256 `228e4ef841a6cea468fc8cdf2c17b2cb27e7acc153ea81cf18773094043eadf9`
  - `road-segmentation-adas-0001.bin` —
    sha384 `00391dd817115d2b076cade11062a4ff8c70659eb629d30b4babae3b6888f7c180e7ef306e8e5f14fde33f0bb23e1207`,
    sha256 `e708aff5c4cd9813b575c72011f2cef9a4f5adb7aa382d81dc7adbb395dc9d8c`

## BrakeCheck modifications

Converted with `tools/model_migration/convert_intel_road.py` (OpenVINO IR → ONNX →
PyTorch → Core ML ML Program, FP16, iOS 17). The wrapper graph accepts the app's
existing `image` input (RGB bytes, 640×384 full-frame stretch), swaps to BGR and
resizes to 896×512 for the network, sums the road and marking channels, resizes
back to 640×384 and emits `road_scores` `[1,2,384,640]` as (non-road, road)
probabilities. Learned parameters are unchanged. The converted output was checked
against the native OpenVINO reference (mean absolute error ≤ 0.03).

## Status

Commercially redistributable under Apache-2.0 with this notice retained. The
model is a general ADAS road segmenter, not a BrakeCheck-trained model: on-device
accuracy at intersections, in rain, at night and around parked vehicles still
needs labeled validation (see `tools/model_migration/ROAD-CANDIDATE-COMPARISON.md`).
Phone latency, thermal behaviour and AR alignment must be measured on the device.
