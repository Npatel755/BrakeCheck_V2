# N14 Home and Camera pages

Updated October 12, 2026. This is a native iPhone build for sideload testing; it is not an App Store submission.

## Behavior

- Home opens first, with trip start/end, saved clips, settings, emergency contacts, incidents, replay, diagnostics, display preferences, and calibration entry. The existing dark theme, amber logo, and mint controls are retained.
- Camera contains the road view, vehicle/lane overlays, danger indication, and driving measurements. Swipe horizontally or use the two page labels beneath the view. Starting a trip, replay, or simulation opens Camera.
- Both pages share the same DriveModel and capture session. Changing pages does not end a trip or restart tracking. Orientation observation, safety check-in presentation, and sheets remain outside the pager.
- Calibration temporarily removes the pager so dragging a vehicle box cannot turn the page. Canceling or completing selection restores paging.
- A critical warning still lasts at least five seconds. It reads **BRAKE NOW** while current risk is critical and **BRAKE ALERT** when only the retained warning remains. Danger steps reflect the current bounded danger display; the selected target is red only for current critical risk.
- Camera tracking and recording require BrakeCheck to remain on screen. The unused background-location declaration and messages claiming GPS continues in the background were removed; background GPS remains disabled. See [APP-STORE-ROADMAP.md](APP-STORE-ROADMAP.md) for the navigation/background design decision and release work.

## video2.mp4 review

The supplied `C:\Users\Nick\Downloads\video2.mp4` is a 263.695-second, 848 × 384 recording of **LIVE · N13**. It shows the phone camera watching driving footage on another screen, with scene cuts and stationary-phone GPS. It supports observations about visible UI and tracking behavior, not calibrated road distances, absolute vehicle speeds, or a collision-accuracy percentage.

| Clip time | Observation | Result |
| --- | --- | --- |
| 0:06–0:10 | Lane fit appears to span adjacent lanes; a left-lane truck becomes the lead. | Investigate fitted lane boundaries and candidate support using raw capture/diagnostic data. No eligibility threshold was widened. |
| 0:18–0:20 | Forward white van becomes a closing hazard and supplies gap/closure/TTC. | Camera-relative risk can be available without moving-phone GPS. |
| 2:12–2:14 and 3:04–3:06 | BRAKE NOW remains while current closure is zero and TTC is unavailable. | N14 separates the retained warning label/box colour from current critical risk. The five-second warning duration remains intact. |
| 2:54–3:01 | Close Nissan requires repeated view checks and shows conflicting held/fresh outlines. | Classifier history and track association need per-ID capture/score traces. Rear/side evidence gates were retained. |
| 3:08–3:10 | Hard compilation cut briefly retains an old dashed outline; numeric measurements clear. | Scene-boundary identity reset remains follow-up work. A brightness-only cut detector was not added. |
| 3:30–3:44 | Night lead stays selected, but motion sampling repeatedly remains unsettled. | Investigate sample cadence, range outliers, and fit residuals. Unavailable motion is not presented as low risk. |

Review frames and detailed notes are under the ignored `ios/.validation/video2-lead/` and `video2-risk/` folders. The Core ML pipeline was not rerun on raw original driving frames. No detection model weights, lane/lead thresholds, or risk calculations were changed for N14.

## Validation

The existing N13 core implementation passed 311 XCTest cases. N14 changes presentation, release metadata, and background-use descriptions, and leaves that tested core implementation unchanged; the core suite was not rerun for these UI changes.

- Native release build and IPA packaging passed with `xtool dev build --configuration release --ipa`. Existing iOS camera-orientation deprecation warnings remain.
- ZIP CRC checks passed. The packaged executable matches the final release binary and contains the N14 page/warning strings. All 11 model resource files match source hashes.
- Bundle version is `0.4.0 (14)`, with measurement revision `N14`. The obsolete N12 fingerprint was removed. Mach-O records minimum iOS 17 and SDK 17; this is a sideload artifact and needs an updated, signed Xcode pipeline for App Store submission.
- The root copy `BrakeCheck-N14.ipa` matches `ios/xtool/BrakeCheck.ipa`: **154,485,694 bytes**, SHA-256 `73678c51325bdfe74090b554ba4bc5e8c2b6772290dc75184cfbb544759dbb5a`. N13 is preserved separately.
- The IPA has no embedded distribution provisioning profile. Sign it with the existing Sideloadly setup for device testing. No TestFlight upload or phone installation was performed for N14.
- `git diff --check` passed. Package verification metadata is saved under ignored `ios/.validation/n14-package-verification.json`.

## Remaining device checks

1. Install N14 and confirm its Home launch page and LIVE/REPLAY revision label.
2. Start a trip, swipe to Home and back, and verify the same camera session, selected vehicle, and recording continue. Rotate in each page and check overlay alignment.
3. Open/dismiss each Home destination; start replay and simulation from their existing sheets; end a trip and confirm return to Home.
4. Enter calibration, drag a selection, and cancel/complete it with the decimal keyboard in portrait and landscape. Confirm paging resumes and the selection stays aligned.
5. Trigger a warning while Camera or Home is visible. Check BRAKE NOW → BRAKE ALERT, current target colour, danger steps, and independent numeric freshness. Also check presentation while a settings sheet is open.
6. Switch to another app and return: camera tracking/recording pause in the background and resume for an active trip. The app must not imply camera protection while unavailable.

These on-device layout, gesture, and lifecycle checks have not been performed for N14.
