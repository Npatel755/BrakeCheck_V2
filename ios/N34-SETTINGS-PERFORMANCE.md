# Build 34: Settings responsiveness and camera workload

Reviewed `video14.mp4`, including the camera/navigation transition and road
frames. The clip shows no measured vehicle/GPS readings and overlapping camera
navigation/collision-status banners. It does not provide inference diagnostics
or establish the cause of missing GPS speed or target measurements. The user's
clarification identified Settings as the main location of the choppiness.

The previous distributed IPA was a debug build. This build uses
`xtool dev build -c release --ipa`, enabling Swift optimization for the pixel
processing and view code. The app bundle version is 34; the existing N33
measurement-revision label describes the measurement rules, not build mode.

Changes:

- Settings no longer observes the full DriveModel. An Equatable destination
  prevents live dashboard updates from rebuilding the settings screen. Specific
  deduplicated publishers update trip/demo/impact state and storage usage.
- Only the small calibration panel observes camera-calibration scenes. Its
  actual lifecycle activates AR calibration in portrait and landscape, instead
  of relying on the landscape sidebar's selection state. Verification rechecks
  current stationary/camera/geometry conditions at the moment of the tap.
- Device voice choices are cached per visible voice page/language rather than
  querying and sorting installed voices during every body evaluation.
- AR presentation has a separate observable object, avoiding two dashboard/map
  invalidations per camera frame. AR work runs only for the visible camera
  navigation screen or calibration panel. Hidden navigation graphics stop their
  timeline while Settings is pushed.
- Visual matching/AR publication are bounded to about 5 Hz. No luma frame is
  copied when there is no lane measurement to track. Lane and road tracking
  reuse the same patch match when their source frames agree. Mask paths and
  route projections are cached between unchanged input observations.
- Patch searches rank offsets using nine samples, then verify candidates and
  ambiguous alternatives against full 7×7 patches. Road-mask warps compute
  lane-bound interpolation per row rather than per pixel. UFLD reads tensor
  strides/pointers directly, avoiding approximately 72,000 temporary NSNumber
  objects per prediction.
- Day vehicle-model loading no longer blocks the vehicle-processing queue while
  the optional night model is compiled/warmed. The standard detector is
  published first; night loading is requested when low light is actually
  encountered. Auxiliary models start after the standard detector is ready.
- Initial Core ML predictions are excluded from steady-state auxiliary timing
  estimates. Cold-start setup cannot permanently suspend an otherwise fast
  warmed model based on its first prediction.
- Camera navigation status now sits in the HUD layout below collision status
  rather than using a fixed camera offset that overlaps it. The risk pill
  distinguishes detector preparation/unavailability from a missing reading.

Validation:

- Full Core suite in release configuration: 393 tests passed, zero failures.
  Added tests for verified patch-motion coordinates, textureless-frame rejection,
  foreground-occlusion rejection and cold-start timing admission.
- Synthetic desktop release comparison with the old implementations: patch
  matching median 1.71 → 1.47 ms; road-mask warp 0.43 → 0.19 ms. Tracked geometry
  agreed, and every mask pixel—including the obstacle hole—was identical. These
  are desktop CPU microbenchmarks, not measured iPhone FPS.
- The same fixture compiled without optimization: old patch tracking 32.68 ms,
  old mask warp 7.34 ms. This confirms that build configuration materially
  changes the CPU workload, independently of the Settings subscription issue.
- iOS release build, packaged-model hashes/contracts, license inclusion and ZIP
  integrity checked. Logs/benchmark fixtures are in ignored
  `tools/model_migration/artifacts`.

Physical Settings scrolling, slider responsiveness, first-launch model loading,
camera FPS, GPS availability and Apple Core ML execution still require checking
on the iPhone. Check Camera settings/calibration and Voice in both orientations,
including navigating away/back with an active trip. Source-model licensing and
the AR ground-plane approximation's device-validation limits remain as recorded
in `AR-NAVIGATION.md` and `THIRD_PARTY_NOTICES.md`.
