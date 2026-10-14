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

- UFLD original ResNet18/CULane: full-frame RGB stretch to 800×288, embedded
  ImageNet normalization, `lane_logits` output `[1,201,18,4]`. The decoder uses
  the official CULane row anchors and a stabilized softmax over 200 grid bins;
  the final bin suppresses absent lanes. Vehicle-overlapping peaks are excluded.
- SegFormer B0/Cityscapes: full-frame RGB stretch to 640×384, embedded ImageNet
  normalization, `road_scores` output `[1,2,384,640]`. Road is Cityscapes class 0;
  non-road aggregates all other softmax classes, including sidewalks and cars.
  Probabilities are bilinearly restored to input resolution before scanning.
- Both trained FP16 `.mlpackage` resources are bundled and compiled/cache-hashed
  on the iPhone. No inference server or runtime model download is needed.
- Vehicle inference retains accelerator priority. Lane and road predictions
  release the shared gate between requests. No queue of old camera frames builds
  up. UFLD and SegFormer have independent cadences. Segmentation defaults to
  0.30 s, increasing to 0.50 s in fair heat and 0.80 s in serious heat; measured
  latency can increase this further. Critical heat disables auxiliary neural
  work. Vehicle latency above 0.20 s stops segmentation; above 0.25 s also
  suspends UFLD. While moving, UFLD latency above 0.15 s or SegFormer latency
  above 0.12 s suspends that model. Stationary observations can probe again.
  Vehicle/viewpoint budgets are not relaxed for AR. These admission heuristics
  do not guarantee hard real-time deadlines: active Core ML calls cannot be
  preempted. Cadence skips preserve only bounded visual support; an actual
  failed/negative segmentation invalidates the old observation.
- Classical lane-marking fallback and the existing visual lane tracker remain
  available when neural inference cannot run. Segmentation is required for AR.

The original UFLD ResNet18 version was selected as the initial mobile model;
UFLD v2's default CULane configuration uses a much wider 1600-pixel input and a
larger classification head. SegFormer B0 is the small encoder variant. Actual
latency and model quality must be measured on the target phone before choosing
a larger model. This change does not claim benchmarked iPhone performance.

The NVIDIA pretrained SegFormer checkpoint declares a custom license (`other`
on its model card). Its upstream SegFormer license limits use to non-commercial
research/evaluation. The full license is bundled; Transformers' Apache license
does not change the checkpoint's license. This integration is an evaluation
build; commercial distribution requires suitable rights or replacement weights.

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

Install `tools/model_migration/requirements-ar.txt` in a Python virtual environment
(CPU PyTorch wheels work), then run `tools/model_migration/export_road_models.py`.
It fetches the official trained CULane checkpoint and NVIDIA Cityscapes checkpoint,
checks all lane inference weight keys, verifies eager/trace parity, exports iOS 17
Core ML packages and writes the source/hash manifest. PyTorch 2.7.1 is newer than
Core ML Tools 8.3's tested PyTorch range; conversion succeeded, but Apple execution
parity remains a required device check.

`tools/model_migration/verify_road_models.py --ipa ios/xtool/BrakeCheck.ipa`
checks resource and IPA hashes, model contracts, color layout, license inclusion,
ZIP integrity and exclusion of the removed YOLOP model.

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
