# Camera navigation

The Camera tab now displays a cyan route ribbon and forward chevrons using the
existing MapKit route. Start a live camera trip, choose a destination in Navigate,
start navigation, and swipe to Camera. Route selection and spoken guidance keep
using the existing navigation service. Live AR is excluded from demo, replay and
target-selection modes.

In Settings → Camera display, enable **Directions on the road** and set the
camera's height above the pavement. The default is 1.4 m. Mount yaw and horizon
adjustments are available for a fixed forward-facing phone mount. The calibration
assistant estimates horizon, pitch and yaw from lane convergence, requiring at
least eight distinct observations spanning one second with stable, nearly
straight lane geometry. Orientation estimation is automatic by default and runs
on the visible Camera tab before navigation starts. Brief cadence gaps preserve
progress without adding evidence. Height remains manual. Check the live preview
in Camera settings before verification. With fresh stationary GPS below
0.5 m/s, use **Verify mounted alignment while parked** to enable the full ribbon.
Verification binds to FOV, frame dimensions/orientation and calibration values.
A changed setting or conflicting orientation estimate removes alignment trust.
An unverified mount uses direction-only guidance. Orientation and fit/fill use the camera's existing shared geometry.
The device's actual camera horizontal FOV supplies the projection's focal scale.

## Perception

- N62 UFLDv2 ResNet34 uses the locally trained best epoch-17 checkpoint from
  the frozen reviewed BrakeCheck export. Resize the full camera frame to
  1600×533, retain the bottom 320 rows (remove the top 213), and supply RGB image
  bytes. ImageNet normalization is embedded in the model. Four raw outputs are
  `loc_row` `[1,200,72,4]`, `loc_col` `[1,100,81,4]`, `exist_row` `[1,2,72,4]`
  and `exist_col` `[1,2,81,4]`. The stock hybrid decoder uses row heads for
  slots 2/3 and column heads for slots 1/4, requiring 37/21 positive anchors
  respectively. Anchors map back into the full camera frame; they do not start
  at the cropped input's origin. Ego-boundary evidence supports corridor fitting.
  The validation-tuned minimum-12 experiment is not enabled in this build.
- NVIDIA SegFormer B0/Cityscapes: full-frame RGB stretch to 640×384,
  `road_scores` output `[1,2,384,640]` for non-road and road probabilities.
- Both trained FP16 `.mlpackage` resources are bundled and compiled/cache-hashed
  on the iPhone. No inference server or runtime model download is needed.
- Vehicle inference retains accelerator priority. Lane and road predictions
  release the shared gate between requests. No queue of old camera frames builds
  up. UFLD and SegFormer road segmentation have independent cadences. Segmentation defaults to
  0.30 s, increasing to 0.50 s in fair heat and 0.80 s in serious heat; measured
  latency can increase this further. Critical heat disables auxiliary neural
  work. Vehicle latency above 0.20 s stops segmentation; above 0.25 s also
  suspends UFLD. While moving, UFLD latency above 0.15 s or road-model latency
  above 0.12 s suspends that model. Stationary observations can probe again.
  Vehicle/viewpoint budgets are not relaxed for AR. These admission heuristics
  do not guarantee hard real-time deadlines: active Core ML calls cannot be
  preempted. Cadence skips preserve only bounded visual support; an actual
  failed/negative segmentation invalidates the old observation.
- Classical lane-marking fallback and the existing visual lane tracker remain
  available when neural inference cannot run. Segmentation is required for AR.

N62 replaces the initial UFLD ResNet18/CULane resource with the trained local
UFLDv2 model; the separate SegFormer road model remains unchanged. The larger
1600-pixel lane input and ResNet34 backbone need latency and thermal measurements
on the target phone. Existing admission and fallback policies remain in force.
No iPhone FPS or Apple Core ML execution-parity result is claimed.

The UFLDv2 checkpoint's Python validation lane F1 is 71.88% on the frozen
1,586-image validation split. This is not per-image accuracy or an on-device
Core ML score. The decoder experiment reached 79.32% validation F1 with lower
precision, but it has not been promoted. Reserved test images were not used.
See [N62 provenance and validation](N62-UFLDV2.md).

UFLDv2 code is MIT and the lane weights were trained locally from an ImageNet
ResNet34 initialization, rather than the old CULane-trained lane checkpoint.
Dataset permissions still require review; local training does not confer
commercial clearance. SegFormer retains its non-commercial research/evaluation
license, so this remains a test configuration for road segmentation.

## Road alignment

MapKit route progress selects the upcoming route section rather than choosing
the nearest segment at every frame. Geographic route coordinates become local
east/north offsets, rotate by measured travel course and mount yaw, then project
onto a ground-plane camera approximation. Lane width convergence estimates the
horizon. The ribbon's center follows the observed ego driving corridor. MapKit
geometry validates that corridor; it cannot select an adjacent lane or invent a
branch merely because pavement was segmented. Route/corridor disagreement
removes the full ribbon. Turns, merges, exits and roundabouts within 35 m use a
HUD direction arrow because the route provider does not establish an individual
recommended lane. GPS never supplies lane identity.

Map matching combines route reachability, course, speed and movement from the
last accepted on-route fix. Noisy fixes do not poison recovery heading. Competing
nearby route legs with compatible directions prevent full alignment. Course
can disambiguate opposing parallel legs; indistinguishable same-direction
overlapping legs remain uncertain.

Camera-frame patch correspondences track neural lanes and warp the last road
mask relative to its original source for at most 0.8 s. Two aligned scenes can
continue with bounded constant-velocity prediction for up to 180 ms during a
brief dropout. Predictions never become new predictor history; a new negative
road inference clears it immediately. Original model observation ages remain
explicit. AR-only predicted/warped roads never enter collision eligibility.
Ribbon smoothing stays inside the current supported corridor and visible pixels.

Raw semantic road pixels clip the ribbon and chevrons, preserving holes even
when the road-span scanner bridges an occlusion. Person, cyclist and vehicle
boxes further exclude foreground obstacles; they do not become collision/range
targets. Diagnostic road/corridor fills are suppressed during active camera AR
so they cannot paint over the navigation mask's obstacle holes.

Three confidence levels control rendering: **aligned** shows the full ribbon;
**directional** shows a HUD arrow without claiming a road position; **lost**
removes aligned graphics and the arrow, preserving ordinary maneuver text.
Full alignment requires verified calibration, both road and neural lane support,
visibility pixels, fresh obstacle detections, map confidence at least 0.75 and
no branch ambiguity. GPS must be at most two seconds old, with position accuracy
at most 20 m, course accuracy at most 15° and speed at least 2 m/s. The scene
must be at most 0.35 s old and source observations at most 0.8 s old. Partial
visual support uses direction-only guidance; loss of all support removes it.
Recovery requires three distinct aligned frames spanning 0.2 s; downgrades are
immediate. AR retains its separate display setting.

This is a visually constrained ground-plane projection, not a full ARKit camera
pose solution. Sloped roads, intersections without lane markings, phone movement,
GPS geometry errors and occlusions can make it unavailable or misaligned. A fixed
mount and physical-device calibration are required to assess it. No physical road
test, Apple Core ML execution parity, or thermal/latency validation was performed
from the Windows/Linux development environment.

## Export and verification

The N62 lane export uses `tools/model_migration/export_ufldv2.py` with the
trusted local frozen run. It checks the source checkpoint/training manifest,
loads the exact trained inference weights, embeds RGB normalization, compares
CPU eager/TorchScript outputs and native-loader preprocessing, and exports the
four-head iOS 17 FP16 MLProgram with float32 outputs. Its provenance records the
conversion environment and checks actually performed. It does not execute
Core ML on the Linux conversion host. The unchanged SegFormer resource keeps
its existing source revision, package hashes and license.

`export_road_models.py` is the historical original-UFLD/CULane exporter; do not
use it to recreate N62's lane package. `verify_ufldv2.py` checks the lane-specific
provenance and output contracts. `verify_road_models.py --ipa
<finished-IPA>` checks both lane/road resources, named output shapes, color
layout, checkpoint metadata, SHA256 inventories, license inclusion and ZIP
integrity. `verify_model_assets.py --ipa <finished-IPA>` checks all model weights
and provenance records. Both reject retired UFLDLane, IntelRoad and YOLOP resources.
These are packaging and structural checks; Apple execution parity remains a
required device check.

`swift test` in `ios/Core` verifies lane class suppression, coordinate conventions,
stable decoding, straight/curved route projection, missing/stale geometry,
calibration rejection, wrong course, vehicle occlusion and smoothing reset, in
addition to the existing driving/navigation regression suite. `xtool dev build
-c release --ipa` in `ios` builds the optimized iPhone app for device testing.

Build 34 isolates Settings from camera-frame publications, scopes live
calibration to its visible panel, caches render/voice data, reduces background
visual work and fixes cold-start timing admission. See
`N34-SETTINGS-PERFORMANCE.md` for the 393-test release validation and desktop
performance comparison.

Device checks: confirm both models report ready; verify road/lane geometry on the
same frame in portrait and landscape; measure capture-to-output latency with
vehicle detection active; verify first-launch model compilation and subsequent
cache loads; calibrate a fixed mount; check straight roads, bends, upcoming turns,
reroutes, stop/start, camera rotations, GPS loss, night lighting and thermal load.

N35 repairs missing detector weights, automatic calibration and departure GPS
state. See [N35 validation](N35-DETECTION-CALIBRATION.md). Verify all model assets
with `python tools/model_migration/verify_model_assets.py --ipa ios/xtool/BrakeCheck.ipa`
from the repository root.

N36 keeps lane outlines visible when the ribbon is unavailable, stabilizes the
route HUD and diagnostic boundaries, and adds **Camera setup** directly on the
Camera tab. Park, secure the phone, measure the lens height above pavement, wait
for automatic orientation, check the preview and finish setup. Expert orientation
controls are under Advanced adjustments. See [N36 validation](N36-CAMERA-GUIDANCE.md).

N37 replaces the modal preview with setup over the existing live camera. Park,
secure the mount, measure lens height and tap **I'm parked - save mount**.
Setup closes automatically without requiring visible lanes or a GPS stationary
fix. Fresh confidently moving GPS still blocks the parked acknowledgment.
The app shows **Waiting for road alignment**, then completes orientation from
stable, fresh lane observations when available later, without another tap.
The saved orientation survives lane gaps and app restarts. Changing camera
geometry, height or explicitly resetting the mount requires setup again.
Full ribbon rendering continues to require current lane/road/obstacle evidence,
reliable route matching and GPS; saved configuration is not live confidence.

N38 replay accepts an imported movie in **Video only - no speed** mode and
shows vehicle/lane/road perception. Route AR remains live-only because an
MP4 or speed-only log does not provide synchronized map position, travel
course, camera geometry and a route. Navigation pause text now shows the
specific reason supplied by the navigation service.
