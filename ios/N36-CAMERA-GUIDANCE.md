# N36 camera guidance repair

Reviewed video16 (99.25 seconds). Vehicle boxes and lead/gap readings now appear,
confirming that N35's missing-detector-weight repair reached the device. The
recording also shows changing provisional outlines, frequent missing lane/target
observations, AR status changes and the maneuver icon disappearing/reappearing.
The calibration screen is not opened in this recording.

## Fixes

- Starting navigation previously hid all lane/road outlines even when no AR
  ribbon could render. A small isolated perception overlay now retains the
  diagnostic outlines until a supported ribbon actually replaces them.
- The maneuver icon previously existed only at `directional` confidence. It is
  ordinary MapKit route guidance and now stays visible at all AR confidence
  levels. Rendering freshness rules still downgrade/hide the ribbon immediately.
- The explanatory AR text requires a persistent change for 600 ms. This filter
  never delays collision alerts or controls measurement/ribbon eligibility.
- Lane presentation uses a longer low-pass filter and requires three distinct,
  agreeing observations before accepting a large geometric jump. A brief switch
  to paint-only fallback cannot replace a recent neural outline. Genuine camera
  geometry changes reset the display filter.
- Road outline presentation uses fixed vertical samples, smoothing and bounded
  800 ms continuity. Road/lane diagnostics draw the two boundaries separately,
  removing jumping cross-road polygon caps. Smoothed road data never supplies a
  semantic mask, collision eligibility or the AR projection.
- One slow auxiliary prediction previously could permanently disable further
  measurements. Stale latency histories now permit a bounded retry after five
  seconds, subject to unchanged thermal/vehicle budgets and the idle accelerator
  gate. A successful recovered prediction replaces the stale cost estimate.
- UFLD readiness and predictions no longer require successful SegFormer loading
  or inference. UFLD measurements are published before segmentation. SegFormer
  errors clear semantic support and retry independently; they do not clear
  working neural lane observations. Core ML may choose GPU/ANE/CPU execution.
- Calibration has a three-step guided screen: park and secure the phone, enter
  lens height above pavement (feet/metres), and allow automatic orientation to
  estimate from stable geometry. Progress counts distinct observations. Advanced
  pitch/yaw/horizon controls are collapsed. Stable high-quality paint geometry
  can assist orientation estimation, with explicit parked verification; full AR
  still requires neural lane support and fresh road/obstacle observations.
- Verification uses a measured stationary GPS sample up to 1.5 s old with
  speed below 0.5 m/s and uncertainty at most 1.5 m/s. Dashboard interpolation/hold
  labels no longer briefly disable it between valid fixes. Invalid, simulated,
  moving or expired fixes still cannot verify a mount.
- Camera setup opens directly from Camera before navigation, or from the route
  HUD during navigation. The shortcut and turn icon do not blink with confidence.

## Checks and limits

- 405 optimized Core tests passed, including nine new jitter/outlier,
  display-only freshness, bounded road hold, status debouncing, inference
  recovery/budget, distinct-calibration-progress and stationary-verification
  freshness regressions.
- Source UFLD inference on seven camera crops finds at least ten paired rows in
  three crops, a single paired row in one, and no paired rows in the remaining
  three. This confirms that some
  missing lanes are actual model observations under occlusion/shadow, not solely
  UI visibility. The app does not invent trained boundaries to conceal misses.
- The supplied camera footage repeatedly contains rapidly changing camera pose,
  GPS availability, target loss and positive closing-gap measurements. The video
  alone cannot establish whether each warning is a false alarm; alert thresholds
  have not been weakened to conceal them.
- Native Release compilation succeeded. The final IPA passed every model
  SHA256 check, road-model contracts/licensing, ZIP CRCs and build-number
  verification (0.4.0/build 36). The displayed revision is N36.

This Windows/Linux workspace cannot execute Apple Core ML or measure physical
phone smoothness/alignment. Source-model crops, unit tests and compilation are
not a substitute for a mounted-camera device test.

The UFLD decoder's row/coordinate contract was checked against the
[official demo](https://github.com/cfzd/Ultra-Fast-Lane-Detection/blob/master/demo.py).
