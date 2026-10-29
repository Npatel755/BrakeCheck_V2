# N62 trained UFLDv2 lane model

N62 builds on the N61 performance changes and replaces the app's original
UFLD ResNet18/CULane lane resource with the locally trained **UFLDv2 ResNet34**
best checkpoint. The independent SegFormer B0/Cityscapes road model, vehicle
models and viewpoint weights remain unchanged. The legacy lane package is
archived outside app resources; old UFLDLane and IntelRoad weights must not ship.

## Source and score

- Run: `run-20261020-014314-latest-audits`, frozen export revision 6010.
- Best checkpoint: epoch 17, SHA256
  `2700da1e5b3956a732e652b12632174ae4013360fd1dbf8e4694d599afaca594`.
- Frozen dataset manifest SHA256:
  `75edfe930998a080b2cc46409b6f01700b6e051a1adeae801262d308040a64ba`.
- Python/CUDA validation: 1,586 images; 1,435 matched lanes, 152 false positives,
  971 missed lanes. Precision **90.42%**, recall **59.64%**, lane F1 **71.88%**.
  The metric rasterizes lane traces at 30-pixel thickness on a 1640×590 canvas
  and matches at IoU 0.5. This is provisional local validation, not official
  benchmark accuracy, an independent test result, or an on-device score.
- Reserved test images were not used for conversion/parity checks.

The missed-lane audit found useful short predictions removed by the stock
length gate. A minimum-12 validation experiment reached **79.32% F1** but reduced
precision to **83.47%**. This validation-tuned decoder is **not promoted** in N62;
its gain does not establish held-out test performance.

## Runtime contract

The caller resizes the entire camera frame to **1600×533**, crops off the top
213 rows, and passes the remaining **1600×320 RGB byte image**. ImageNet
normalization is embedded in the graph. The model is an iOS 17 FP16 MLProgram
with float32 raw logits:

| Output | Shape |
| --- | --- |
| `loc_row` | `[1,200,72,4]` |
| `loc_col` | `[1,100,81,4]` |
| `exist_row` | `[1,2,72,4]` |
| `exist_col` | `[1,2,81,4]` |

The stock hybrid decoder uses row heads for slots 2/3 and column heads for
slots 1/4, retaining at least **37 row** or **21 column** positive anchors.
Local soft argmax uses radius 1 and a 0.5 bin-center offset. Full-frame anchor
coordinates are retained when mapping predictions back to the camera. Ego
boundary evidence supplies corridor fitting; a wider model input does not
supply missing map lane identity or justify drawing unobserved paint.

The model hash participates in the existing compiled-model cache. Existing
cadence, thermal admission, vehicle-inference priority and fallback policies
remain. Physical-device latency and thermal behavior require measurement.

## Validation status

Export and `verify_ufldv2.py` completed: exact checkpoint/manifest hashes,
strict trained-weight loading, eight validation-image CPU eager/TorchScript
and preprocessing comparisons, four named output contracts, three package
file hashes and 79 trained-weight references passed. The converted lane package
contains 432,871,555 bytes. Cross-framework CPU comparison on the same eight
validation images also passed: original Windows PyTorch 2.11.0 versus converter
Linux PyTorch 2.7.0 differed by at most `1.52587890625e-05` in raw logits.
`verify_road_models.py` passed for both UFLDv2 and unchanged SegFormer:
RGB input sizes, all named output shapes, checkpoint/epoch/source/license
metadata, stock decoder provenance and the exact seven-file lane/road manifest
inventory. `verify_model_assets.py` passed for all bundled detector, lane, road
and viewpoint weights/provenance SHA256 records. Retired UFLDLane, IntelRoad and
YOLOP assets are rejected in app resource roots; the explicitly excluded legacy
comparison directory remains outside this check and outside the app.
All **513 release Core tests passed**, including 20 UFLDv2 decoder tests.
The release iPhone build and all three finished-IPA model verifiers passed:
bundled bytes, notices, old-model exclusion, output contracts and ZIP CRC.
Vehicle, road and viewpoint model weights remain unchanged.

Local artifact: `BrakeCheck-N62.ipa`, 584,626,383 bytes. SHA256:
`467cb873784e81b786c5c495322b6ea661c14645118f93e3a4ea44f2a9d515e1`.

These checks do not execute the converted graph on Apple hardware. Core ML
numerical parity, first-launch compilation, iPhone FPS, sustained thermal cost
and physical-road behavior have not been validated. The larger ResNet34 model
may consume more time/memory than the prior ResNet18 resource.

Run from the repository root in an appropriate conversion environment:

```text
python tools/model_migration/verify_ufldv2.py --ipa <finished-IPA>
python tools/model_migration/verify_road_models.py --ipa <finished-IPA>
python tools/model_migration/verify_model_assets.py --ipa <finished-IPA>
```

For device verification, use the same mounted camera and route as N61. Check
portrait/landscape preprocessing alignment, visible lane endpoints, curves,
night/glare, vehicle occlusion, compile-cache reloads, and capture-to-output
latency with vehicle detection active. Record timings rather than inferring
FPS from successful packaging.

## Provenance and permissions

UFLDv2 architecture code is MIT. This checkpoint descends from ImageNet
ResNet34 initialization and reviewed local lane-data training, rather than the
old CULane-trained lane weights. Source and conversion details ship in
`LaneModelData/UFLDv2Lane-provenance.json` and `manifest.json`; the UFLD MIT
notice is retained. Dataset provenance and applicable permissions still need
assessment before commercial use. No commercial-clearance claim is made.
The unchanged SegFormer model keeps its NVIDIA non-commercial
research/evaluation license and its bundled license text.
