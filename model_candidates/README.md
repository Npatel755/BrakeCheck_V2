# Commercial model candidates (evaluation branch)

This branch stores the two published checkpoints selected for evaluation and
packages a test iOS app using them. The converted models have **not passed live
lane or AR validation**. The attached dataset plan is intentionally not
implemented here.

## comma.ai comma10k-segnet

- Source: https://huggingface.co/commaai/comma10k-segnet
- Pinned revision: `b642c614eb0737fe7befa229f47fbd0245033038`
- `model.safetensors` SHA-256:
  `8208672861ad1b111dc98f3a7c54196d29875b709c7353e2dd1b7614343fb3a8`
- Published model-card license: MIT. The source comma10k repository explicitly
  describes its driving images and masks as MIT licensed:
  https://github.com/commaai/comma10k
- Architecture: U-Net with EfficientNet-B2 encoder; five semantic output
  classes. The checkpoint produces pixel classes, not lane instances or the
  correct driving corridor. Class mapping and preprocessing must be checked
  against official examples before integration.
- Files here: original safetensors, model card, model config, evaluation
  preprocessing config and an FP16 Core ML conversion. No fine-tuning has been
  done. Three original comma10k masks identify output class 1 as lane markings.

## Intel road-segmentation-adas-0001

- Source: https://github.com/openvinotoolkit/open_model_zoo/tree/master/models/intel/road-segmentation-adas-0001
- Published model classes: background, road, curb, marking.
- The included FP32 `.xml` and `.bin` are the official four-class weights;
  their SHA-384 hashes match Intel's `model.yml`. The included
  `IntelRoad.mlpackage` is the previously converted two-channel test candidate;
  it combines road and marking and therefore cannot serve as a marking detector
  without a new conversion from the four-class source.
- Intel Open Model Zoo license: Apache-2.0. Preserve upstream notices and
  confirm rights for the exact checkpoint before release.

The main working branch retains UFLD/SegFormer for testing. This branch's iOS
target bundles only comma.ai and Intel camera weights. Neither model's license
is a substitute for measured lane geometry, camera alignment or on-device
testing.
