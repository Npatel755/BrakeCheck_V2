# N31 permissive vehicle-detector migration

N31 includes N30's restored original voice defaults and GPS display continuity.
The day detector is now Apache-licensed RTMDet-tiny; the low-light detector is
Apache-licensed RTMDet-small. Low-light is a selection profile, not a claim that
the COCO checkpoint was trained specifically for night driving. The original
traffic-trained YOLOv8s and YOLO11n are retained only in development sources and
old prototype IPAs; neither is part of the N31 package resource list.

## Selection evidence

OpenMMLab reports COCO box AP 41.1 / 4.8M parameters for tiny and 44.6 / 8.89M for
small at 640 pixels. RF-DETR Nano is an Apache-designated model with stronger
general COCO AP, but 30.5M parameters and an experimental Core ML exporter. We
screened all three candidates and the original YOLO11n on the same 16 sampled
frames from video2, including dark-looking screen-recording segments around
211–225 seconds. The user clarified that neither supplied clip contains night
driving; these segments are not a representative nighttime validation set. See
`.validation/model-migration/screening.json` and the four contact sheets.

Both RTMDet profiles detected the central vehicle in those dark-looking
segments. Small removed tiny's full-image false vehicle box in one such sample.
These observations do not establish real nighttime detection performance.
RF-DETR detected additional roadside vehicles; these unannotated samples cannot
establish which model has the best precision/recall. The recording contains old
boxes, captions, control-center content and cuts, so detection counts are not
accuracy scores. This does not establish equivalence to the specialized old
night checkpoint, which was not available in PyTorch form for this screening.

On this Windows/WSL CPU, four-thread PyTorch median latency was approximately
186 ms tiny and 281 ms small. Reference/challenger public Python APIs measured
61 ms YOLO11n and 248 ms RF-DETR Nano, including their preprocessing and
postprocessing. These paths differ and none measures iPhone/ANE latency or
battery performance. The original detector was faster on this CPU; do not claim
that the migration improves speed. Tiny is the smaller practical mobile
candidate; small supplies more capacity in low light. Existing measured-latency
and thermal inference budgets still apply.

Sources:
- https://github.com/open-mmlab/mmdetection/blob/main/configs/rtmdet/README.md
- https://github.com/roboflow/rf-detr
- https://github.com/roboflow/rf-detr/blob/develop/docs/exports/coreml.md
- https://www.libreyolo.com/docs/models/rtmdet

## Integration and provenance

`tools/model_migration/export_rtmdet.py` converts verified LibreYOLO checkpoints
derived from the official OpenMMLab EMA weights. It embeds RGB-to-BGR mean/std
normalization, FP16 weights, normalized center/size coordinates, the original
80 COCO label ordering and Apple's per-class NMS. The contract remains a
640×640 image input and Vision recognized-object output. Existing box filtering,
tracking, lane inference, lead eligibility and danger calculations are retained.
Night-only three-class aliases are disabled because both new models use COCO.

Model resources live in `DetectorModelData/Day` and `DetectorModelData/Night`.
The loader checks family, export revision, profile, class count and Apache
metadata, hashes every file (including pipeline weight files) and reuses a
compiled cache only when the package is identical. Model cards, source hashes,
converted hashes and Apache/MIT notices ship in `ModelLicenses`.

## Validation limits and release requirements

Conversion and traced/PyTorch output parity passed. Static package checks verify
image and NMS contracts, labels, checkpoint hashes, graph weight-file references,
resource identity and exclusion of the old Ultralytics resources. Native release
compilation and existing Core tests check app integration and retained logic.

Completed: native release build, all 362 Core tests, static contracts/source and
converted hashes, varied-input traced/PyTorch parity, and final IPA CRC,
executable identity, revision, all 18 resources and exclusion of old detectors.
Final IPA SHA-256: `94850a085dc9fe12645a5263f962b263cc2edf3d215f6bea35f61a8737b1caef`.

Core ML execution is unavailable on Windows/WSL. Required phone checks: first
model compile, day/night/automatic switching, correct boxes and lead tracking,
Core ML output parity, latency p50/p95, temperature and power use. Required before
claiming equal-or-better performance: a labeled independent day/night set,
vehicle recall/precision, lead-track dropout, box jitter and target-iPhone power
measurements. Fine-tune on commercially permitted night data if the COCO night
profile falls short. No equal-or-better or App Store-ready claim is made.
