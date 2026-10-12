# N13 lead and danger consistency

The vehicle display, lead identity and collision measurements now have separate continuity rules. Display smoothing cannot change the measured boxes used for range or closing speed.

## Changes

- Light, time-based smoothing follows each vehicle ID. Boxes snap to new geometry after a changed crop or longer detection gap. Replaced display IDs are retired so ghost boxes cannot return.
- Vehicle association predicts motion through detector gaps up to 600 ms. Ambiguous crossings and superseded identities still need new orientation evidence.
- Two independent rear checks remain attached to the same ID during lead acquisition. An eligible replacement can confirm while the former lead coasts or exits its lane.
- Strong measured lane history can guide candidate selection for at most 800 ms. It does not become a fresh lane measurement or authorize stale numerical path support. Adjacent, front-facing and repeatedly side-facing vehicles remain excluded.
- A selected lead reuses the range fit already collected for its ID, avoiding an additional speed-sampling delay. The risk estimator also handles valid rapid closure at a slower detector cadence.
- The danger display increases immediately, requires 350 ms of new lower observations before dropping a band, and can hold its last measured indication for at most 600 ms through a brief gap. Held indications say “checking.” Distance, speed and TTC retain their own freshness checks; unknown observations cannot become a green indication.

## Video1.mp4 review

The supplied clip is a screen recording of N12: 848 × 384 pixels, approximately 299.74 seconds. It includes the app's HUD and existing detection annotations, rather than the original camera frames and capture/GPS log.

Sampled frames show repeated box loss and rear-view reconfirmation while vehicles remain visible. This occurs with the highway truck around 0–25 seconds and with traffic near the lights around 253–280 seconds. At 260 seconds a rear-confirmed vehicle is visible while lead metrics remain unavailable. The truck around 20 seconds also sits slightly to the right of the fixed camera-axis guide, supporting use of recent measured lane geometry during brief lane gaps.

The clip was inspected locally. The new Core ML detectors were not executed on this video, and no before/after detector accuracy or lead-availability percentage is claimed. The longer automated driving fixtures are synthetic scenarios inspired by these failure patterns.

## Validation

Final N13 checks completed with Swift 6.4 in WSL:

- **311 core XCTest cases passed**, including 33 new continuity and pipeline regressions.
- **iPhone release compilation and IPA packaging passed** with `xtool dev build --configuration release --ipa`. Existing camera-orientation deprecation warnings remain.
- `ios/xtool/BrakeCheck.ipa` contains the executable from the final release build, revision N13, and all 11 unchanged model resource files. Resource hashes and ZIP integrity checks passed.
- The IPA is unsigned. Use the existing `xtool dev` signing/install workflow from `ios/` before device testing.
- `git diff --check` passed. No detector model weights or production GPS behavior changed.

Run from the project root with Swift 6:

```sh
swift test --package-path ios/Core --jobs 2 --disable-swift-testing
```

Build from `ios/`:

```sh
xtool dev build --configuration release --ipa
```

Core regression coverage includes vehicle jitter, temporary misses, crossing ambiguity, front/side vetoes, adjacent-lane exclusion, lead replacement, stale lane evidence, rapid closure at 2 Hz, isolated range spikes, danger hold expiry and GPS/camera timestamp alignment. Legacy assertions were updated to match the existing adaptive GPS filter and lead grace periods; production GPS behavior was not changed.

Actual lead availability and detector performance still require an iPhone replay or a new capture. A camera cannot guarantee detection of every eligible vehicle. Range uses assumed vehicle width, and the danger bands are a heuristic collision indication rather than a calibrated probability.

## Device checks

1. Build/install N13, then replay the original camera footage with its GPS log where available. Confirm the LIVE/REPLAY revision shows N13.
2. Follow a visible in-lane vehicle through straight and curved sections. Check that its ID survives brief misses, its displayed box follows the vehicle, and nearby passing vehicles do not replace it.
3. Check a closer cut-in and a lead leaving the lane. The new lead should confirm promptly, with no inherited speed or acceleration from the former lead.
4. Verify that a steady measured gap produces a current lower indication, a closing gap raises danger immediately, and a short miss shows “checking” before the bounded hold expires.
5. Verify that missing GPS pauses absolute speed/headway while fresh camera closure can still provide an estimated collision indication. Missing camera motion must remain unavailable.
