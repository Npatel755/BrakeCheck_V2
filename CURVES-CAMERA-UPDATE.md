# BrakeCheck: curves, persistent lead tracking, full-screen camera

This update builds on the latest BrakeCheck-Railway project. Use the update
archive to keep your current folder and local settings.

## Apply to your existing project

1. Extract BrakeCheck-Curves-Camera-Update.zip.
2. Copy its ios folder into your current BrakeCheck folder. Merge folders and
   replace the matching files; do not delete your current ios folder.
3. The eight app source files below must be applied together. Swift Package
   Manager automatically discovers the two new source files.
4. From your current project's ios directory, run:

       xtool dev build

5. Reinstall the rebuilt app using your existing Sideloadly workflow. Choose
   Auto select lead vehicle from the scope menu if you previously calibrated
   a target manually. These are iPhone changes; no backend deployment is needed.

| Path relative to the project root | Action |
| --- | --- |
| ios/Core/Sources/BrakeCheckCore/LaneGeometry.swift | Add |
| ios/Core/Sources/BrakeCheckCore/LeadSelector.swift | Replace (add if absent) |
| ios/Sources/BrakeCheck/Services/LaneTracker.swift | Add |
| ios/Sources/BrakeCheck/Services/VehicleTracker.swift | Replace |
| ios/Sources/BrakeCheck/App/DriveModel.swift | Replace |
| ios/Sources/BrakeCheck/Views/CameraPreview.swift | Replace |
| ios/Sources/BrakeCheck/Views/DetectionOverlay.swift | Replace |
| ios/Sources/BrakeCheck/Views/ContentView.swift | Replace |

The archive also includes the selector and lane regression tests. They are
optional for installing the app.

## What changed

The camera now fills the screen. GPS speed, lead speed, gap, closing speed,
TTC, the risk indication, trip controls, and incident saving overlay the
camera. The road's center remains open. Camera, lane lines, target boxes, and
manual selection use one shared aspect-fill rectangle, so enlarging the camera
does not move boxes away from their vehicles.

Automatic selection uses a soft lane-position score rather than the previous
narrow, fixed corridor and box-width constraint. It confirms a new candidate
after three observations over at least 0.20 seconds. Acquisition accepts
detector confidence of 45% or higher; matching an existing track accepts 28%
or higher. The published identity can survive a detection gap of up to
0.55 seconds. A nearer cut-in gets separate confirmation and range history.

Apple Vision follows the confirmed vehicle between detections. A fresh YOLO
box reseeds the visual tracker to reduce drift. A dashed yellow box means
the track is being held or a closer cut-in is being confirmed. During that
interval, derived speed/TTC/risk estimates pause. Predicted or visually
tracked widths are not new distance measurements. Fresh detections below the
existing risk estimator's 65% confidence threshold can keep the target visible
and show an approximate gap, but do not produce a risk score or lead speed.

## Lane estimate and curves

This version uses lightweight image processing on the iPhone's camera frames:
white/yellow paint contrast, vehicle masking, separate robust quadratic fits
for left/right boundaries, and temporal smoothing. It scans about five times
per second. It is not a bundled pretrained neural lane model.

When two boundaries can be fitted and confirmed, vehicle selection follows
their curved center at the vehicle's road-contact point. Each boundary can be
dashed or partly occluded. The estimate requires at least six points on each
side spanning at least 22% of image height and two consistent scans.
Lane coordinates do not extrapolate beyond the observed road region.

When markings are missing or the fit fails, the UI explicitly says
LANE UNAVAILABLE · FALLBACK. The faint dashed guide is a mounting assumption;
the solid green outline is a lane estimate. Short gaps can hold a prior lane
estimate for at most 0.50 seconds. The scope menu can hide lane lines or show
all raw vehicle boxes.

This improves the tested selection logic but does not establish reliable road
perception. Worn paint, shadows, bright reflections, intersections, tight
curves, hills, camera tilt, and an incorrectly fitted lane remain limitations.
The detector has no front/rear direction classifier: an oncoming or parked
vehicle within an incorrectly estimated path can still be chosen. A stopped
vehicle genuinely in your lane must remain eligible.

UFLDv2 with CurveLanes weights is a candidate for a future neural lane model.
Its PyTorch weights still need Core ML conversion, correct preprocessing/output
decoding, and performance/accuracy testing on your iPhone and footage. This
update does not claim that those weights were integrated.

## Verification

- Swift 6.0.3 compiled BrakeCheckCore in Swift 5 language mode.
- 55 focused tests passed: 20 selector, 14 lane/geometry, 15 driving math, and
  6 incident-retention tests.
- The lane scanner was exercised with synthetic white/yellow curved markings,
  dashed boundaries, occlusion, invalid input, and vehicle masking.
- All six changed iOS tracker/model/view files passed Swift syntax parsing.
- A full iOS typecheck/build, device install, camera performance measurement,
  visual layout check on a device, and real-road validation were not performed.
- The full core suite was not run: the prior update documented existing
  GPSFilter test failures, including a test that force-unwraps nil. This update
  does not modify GPSFilter or those tests.

To run the focused checks:

    cd ios/Core
    swift test --filter 'LeadSelectorTests|LaneGeometryTests|DriveMathTests|RetentionTests'

## Next device check

Use recorded footage or a passenger-operated test. Check straight traffic,
left/right curves, stopped traffic, parked vehicles, oncoming lanes, lane
changes, and a brief blocked camera view. Check that the selected box stays
on the vehicle with the camera enlarged and that a detection gap produces
a yellow box with paused estimates. Camera mounting changes still affect
the lane estimate and approximate width-based range.

## References

- UFLDv2: https://github.com/cfzd/Ultra-Fast-Lane-Detection-v2
- Apple Vision tracking:
  https://developer.apple.com/documentation/vision/tracking-multiple-objects-or-rectangles-in-video
- Core ML PyTorch conversion:
  https://apple.github.io/coremltools/docs-guides/source/convert-pytorch-workflow.html
