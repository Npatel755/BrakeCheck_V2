# Lane-model plan viability review

The supplied dataset-system text was read as a proposal, not executed or
modified. Its core idea is viable: start with comma10k's licensed images and
labels, use the published comma10k-segnet as an annotation candidate, curate
additional authorized driving scenes, and train a separate lane-instance model.
It is not a short path to a production AR lane detector.

## Scope and rights

- comma10k explicitly describes its images and masks as MIT licensed:
  https://github.com/commaai/comma10k . The published comma10k-segnet card
  declares MIT and identifies comma10k as training data:
  https://huggingface.co/commaai/comma10k-segnet . Keep both source records.
- comma2k19's repository is MIT, but its roughly 33 hours cover a repeated
  California highway commute. At one frame per second that is approximately
  119,000 frames, not 119,000 independent scenes. Separate train/test by drive,
  not random frame. Its full download is about 100 GB:
  https://github.com/commaai/comma2k19 .
- ZOD requires approved access. Its dataset is CC BY-SA 4.0, while its SDK is
  MIT: https://github.com/zenseact/zod . Review share-alike obligations and
  the actual access terms before mixing it into a commercial training pool.
- SAM 2 checkpoints are Apache-2.0. SAM 3 and DINOv3 have their own Meta
  licenses; review each exact checkpoint and the resulting distribution plan
  before using them as commercial teachers:
  https://github.com/facebookresearch/sam2 ;
  https://github.com/facebookresearch/sam3 ;
  https://github.com/facebookresearch/dinov3 .
- CARLA code is MIT, assets are CC BY, and Unreal Engine has separate terms:
  https://github.com/carla-simulator/carla . Track synthetic provenance.

## Technical limits

comma10k-segnet predicts five semantic pixel classes, including lane-marking
pixels. It does not directly output individual lane instances, boundary order,
ego-lane identity, or UFLD V2 training coordinates. Verify its exact class
mapping and preprocessing empirically before treating any class as lane paint.
The Intel model's four classes include markings, but its previous BrakeCheck
Core ML conversion collapsed road and marking into one channel. Neither
checkpoint alone solves route-to-camera alignment or safe lane selection.

The proposed 100,000–200,000 target is a possible later scale, not a useful
first milestone. Two reviewers cannot exhaustively correct that many images
within a $100 cash budget. Start with a modest, diverse, human-reviewed set;
use model-assisted annotation only after a fixed holdout confirms it reduces
review effort without hiding systematic mistakes.

## Proposed release gates (engineering targets, not published benchmarks)

1. **Rights gate:** every training image, label, teacher checkpoint and final
   weight has recorded source, version, license and permitted commercial use.
2. **Pilot gate:** build 2,000–5,000 human-reviewed, drive-separated training
   images and at least 1,000 untouched evaluation images. Include enough
   distinct night, rain, curve, merge, intersection, occlusion and missing-paint
   scenes to report each category separately. Do not infer performance from
   comma2k19's highly correlated frames alone.
3. **Lane geometry gate:** on the held-out set, target at least 95% boundary
   precision and 90% recall for visible near/mid-field lane boundaries, plus
   95th-percentile boundary error below 3% of image width. Review all false
   ego-lane selections, not only the average metric. Tune these thresholds
   against a labeled baseline and the actual AR display geometry.
4. **AR display gate:** show the full camera-aligned ribbon only when a
   separately calibrated confidence gate identifies the correct driving
   corridor. Target at least 99.5% correct-corridor displays across independent
   road episodes; when evidence is ambiguous, fall back to a direction arrow.
   Hundreds of independent episodes across conditions are needed to support
   such a low error rate; adjacent video frames are not independent samples.
5. **Device gate:** on the target iPhone, sustain at least 5 fresh lane updates
   per second while vehicle detection and navigation run, with p95 lane-result
   age below 250 ms and no sustained thermal throttling or severe UI choppiness.
   Measure a continuous 30-minute drive and battery impact. Thresholds are
   provisional until tested on the actual device and camera mount.

Meeting segmentation IoU alone is insufficient. The final decision depends on
observed lane geometry, wrong-lane rate, stable tracking, on-device latency,
camera calibration, and fail-safe behavior at intersections and missing paint.
