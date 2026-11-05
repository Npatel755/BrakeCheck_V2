# Ongoing improvement audit

## 4 November 2026 — commercial model clearance, lane alerts, lane-level guidance

### Completed

- Replaced the bundled NVIDIA SegFormer road model (non-commercial licence) with the Apache-2.0 Intel `road-segmentation-adas-0001` Core ML package that the repository had already converted and trialled. The tensor contract (`image` 640×384 RGB → `road_scores` [1,2,384,640]) is unchanged, so `LaneTracker`, the AR road mask, road-supported lead selection and camera-route alignment keep their existing decoding. Added `IntelRoad-MODEL-CARD.md` with source SHA-384/256 hashes, rewrote `NOTICE.md`, updated the lane manifest and the model verification scripts, and made SegFormer a retired model that CI rejects.
- Removed the AGPL Ultralytics YOLO resources (`YOLOModelData`, `ios/licenses`) from the repository; they were already excluded from the package but their presence complicated licence review.
- TestFlight builds now ship the lane and road models instead of stripping `LaneModelData`; the workflow verifies every bundled weight hash and licence and fails if a SegFormer or YOLO asset reappears. The build-check workflow runs the same verification.
- Offered Lane Departure and Cut-In Prediction as Premium "Lane alerts". Their engine, readiness messages and voice lines already existed behind an "offered" list; they remain gated on verified camera alignment, reliable GPS speed (≥ 18 mph for departure), a fresh non-paint lane pair with confidence ≥ 0.85, and pause during planned turns and route rejoins.
- Added lane-level navigation guidance (`LaneLevelGuidance` in Core). Using UFLDv2's four trained boundary slots it recognises the current lane among the visible painted lanes and, between 60 m and 25 s of travel before a turn or exit, shows "Move one lane right/left for the turn, when safe" or "Stay in this lane" in the turn banner and speaks the move once. A move is only suggested across a boundary that reads as broken paint (several pieces or incomplete coverage), never across a continuous line, so a two-way road's centre line never produces a left-move suggestion. Three consecutive fresh observations over 0.8 s are required; brake warnings, check-ins and active turn prompts take priority over the voice line.

- Added a curve-speed warning (`CurveSpeedAdvisor` in Core). The active route is resampled every 10 m once; each GPS fix looks 15 m to ~8 s of travel ahead, finds the tightest bend, summarises it as one curve (length / total heading change) and warns when the current speed would exceed 3.5 m/s² of lateral acceleration ("Curve ahead · ease off"; ≥ 5 m/s² "Sharp curve ahead · slow down"), with the comfortable speed for 2.5 m/s². Intersection corners (effective radius < 25 m) are left to the turn prompt. Shown in the turn banner ahead of lane advice and spoken once per bend after two consistent fixes.
- Lane Departure now says which way the vehicle is drifting ("Drifting left/right — check your lane", with Spanish equivalents) instead of the generic reminder.

- Lane tracking stability: a confirmed UFLDv2 ego pair now survives one rejected or incomplete capture (≤ 0.6 s) instead of being discarded and re-confirmed over two more frames, so a single dropped head no longer blanks the corridor for ~0.5 s; a slot that decodes into several pieces keeps its best-supported piece. The display Kalman smoother likewise keeps its tracks across one missing capture (≤ 0.75 s) — nothing is drawn meanwhile — and weights far anchors as noisier than near ones (σ grows with image height), so distant lane ends stop twitching while the near corridor remains responsive. Freshness windows (0.5 s) are unchanged; no prediction is ever drawn without a current observation.

- UI polish from the simulator screenshot review: the camera route overview no longer overlaps the speed / car-ahead readouts while a trip runs; camera error text no longer doubles its full stop ("Simulator.. Use"); the Safety page shows the Premium lock once instead of under every alert toggle. Lane alerts and the Intel notice appear in Settings as intended.

- Trimmed the camera route mini-map (duplicated the Navigate page and crowded the readouts) and the Raw/Averaging/Kalman lane-tracking comparison picker (development tool; Kalman stays). Camera capture and preview now set `videoRotationAngle` (iOS 17 API) instead of the deprecated `videoOrientation`, removing the remaining deprecation warnings in `CameraService`/`CameraPreview`.

- Following-distance coaching (`DrivingAssistFeature.followingDistance`, Premium, under Lane alerts): after four seconds of headway under about one second behind the same confirmed lead at ≥ 22 mph, says "Leave more room ahead" once, then rests for a minute. Collision/brake warnings are unchanged and separate.
- First-launch app tour: after the driver-aid notice and practice-drive flow settle, a small "Want a quick tour?" popup offers six tip cards (Start trip, road view, Navigate, Clips/Trip history, Safety settings, done) that switch to the page they describe. Replayable from Settings → Try BrakeSense → "Show the app tour". `ScreenTour` captures the offer and three cards.
- Device-condition notice during a trip: when iOS reports a serious/critical thermal state or Low Power Mode, the HUD shows "Phone running hot / overheating / Low Power Mode on" (spoken once per trip, English/Spanish). Warnings are never suppressed; the driver just learns why they may lag.
- Low-battery notice during a trip: when the phone is at or below 15% and unplugged, the HUD shows "Battery low · plug in to keep the camera running" (spoken once per trip, English/Spanish). Battery monitoring is only enabled while a trip runs.
- The curve banner appends the comfortable speed in the driver's units ("Curve ahead · ease off to ~35 mph") when it is at least 5 below the current speed.
- Parked auto-end (`ParkedTripMonitor` in Core): a trip left running while the car sits still for 8 minutes shows "Parked · trip ends in N s" in the HUD and on Home (spoken once) with a Keep going button, then ends and saves itself after 90 s so the camera, GPS and recorder stop draining the battery. Only confirmed stationary fixes advance the timer, confident movement resets it, a GPS gap holds it, and guidance to an unreached destination or an open check-in defers the prompt.
- Own hard braking (`OwnBrakeDetector` in Core): two consecutive real, accurate GPS fixes decelerating at ≤ −3.5 m/s² while moving ≥ 5 m/s log a "You braked hard" moment on the trip map and timeline (6 s cooldown, simulated/stale fixes never count). Until now only the lead car's braking was recorded.
- Own hard brakes count: `TripMetrics.ownHardBrakes` (optional, older trips decode without it) joins lead hard brakes in the Home/trip-page "hard brakes" total, the trip score (`TripSafetyScore(trip)` weights both alike) and a new `own_hard_brakes` CSV column; the history row says "N hard brakes by you".
- Camera view blocked (`CameraViewMonitor` in Core): when the scene sampler sees near-uniform frames (10th–90th percentile luma spread ≤ 0.06) for 15 s while real GPS shows ≥ 5 m/s, the trip notice reads "Camera view blocked · check the mount and windshield" (spoken once, EN/ES) and clears on the first varied frame. Stopped, simulated or inaccurate GPS never confirms it, so a night road's headlit asphalt against the sky does not trigger it.
- Trip timeline → clip: a "What happened" moment with a saved clip within 20 s of it now opens that clip (play icon on the row) instead of needing the Saved clips list below.
- Break reminder (`BreakReminder` in Core): two hours into a trip, then hourly, "2 hours on the road · take a break when you can" shows on the HUD/Home for 20 s and is spoken once (EN/ES) — only while the car is measurably moving, and never over a brake warning, directions or a check-in.
- Spoken trip summary (`TripSummarySpeech` in Core): once a trip is saved, the voice says "Trip saved. 24 minutes. Smooth, 94." (EN/ES; trips under two minutes get no score) so the outcome is heard without looking at the phone. Respects the spoken-alerts switch.
- Today on Home (`TripDayDigest` in Core): with two or more trips today, the last-trip card adds "Today · 3 trips · 1 h 21 min · score 94".
- Clips on your own hard brakes: the automatic-clip path that already covered brake warnings now also fires on a measured hard brake of yours ("Clip saved · hard brake"), same single 10-minute cooldown and the same Settings → Recordings switch.
- Lane moments on the trip page: Lane Departure and Cut-In alerts are logged as "Drifted toward the lane line" / "Vehicle cut in ahead" on the trip map (teal pins) and "What happened" list, and travel in the GPX export. They are coaching moments, not collision evidence, and do not touch the score.
- Share ETA (`ETAShareMessage` in Core): while navigating, a Share ETA button in the trip bar sends "On my way to Piedmont Park — arriving around 5:42 PM (18 min). Sent from BrakeSense." to Messages or any share target.
- Lane corridor width continuity (`UFLDv2EgoCorridorEstimator`): a confirmed corridor whose near width changes by more than 1.35× in one capture — the classic UFLD slot hop onto the neighbouring painted line — is held like a dropped frame instead of replacing the lane; a width that persists (a real lane change) re-confirms over the next two captures. Small width changes still measure every capture.
- Road-mask veto for the ego corridor (`RoadRegion.encloses`): the Intel road mask from the previous cycle can reject a corridor whose edges sit more than 6% of the frame outside the observed road on over 30% of shared rows (kerb, barrier, oncoming side). It only ever removes a corridor — never adds or widens one — and stays out of the way when the mask is stale, low quality or shares under five rows. The lane status line counts "road vetoes".
- Weekly score trend: the Premium weekly trends panel on Trip history shows each week's average trip score next to the lead hard-brake rate (`TripTrendWeek.averageScore`).
- Route export: the trip page's Share button hands the trail to Maps/Files/Messages as GPX (`TripRoute.gpxDocument` in Core) — one track segment per continuous GPS run so signal gaps stay gaps, plus a waypoint for each flagged moment with its speed and closing time.
- Trip score (`TripSafetyScore` in Core): a 0–100 Smooth/Steady/Busy/Rough rating from estimated hard brakes and close calls per ten minutes of driving, shown on the Home last-trip card, trip detail stats and the trip history rows. Trips under two minutes are not scored.
- Brake warning timing (`BrakeWarningTiming` in Core): Settings → Safety offers Standard or Earlier. Earlier promotes a high-risk closing lead with under 2.6 s estimated time to impact to the brake warning instead of waiting for the critical gate; it can only bring warnings forward.
- Automatic clips on brake warnings: the first brake warning in any ten-minute window preserves the surrounding footage as an incident (same path as the Save clip chip), so near misses are kept without a tap. Settings → Recordings → "Save a clip on brake warnings" turns it off.
- Siri / Shortcuts / Action button: "Start a trip in BrakeSense" and "End the trip in BrakeSense" App Intents (`TripIntents.swift`) open the app and run the same Start/End path as the buttons, so a trip can begin before the phone goes in the mount. The intents refuse to start until the driver-aid notice has been accepted.
- Home "Navigate again" row: routes to the most recent destination in one tap whenever no route is set.
- Platform: `AVCaptureVideoOrientation` replaced by an app-owned `CaptureOrientation` (same raw values, so recorded metadata still reads); `ReplayService` loads asset metadata asynchronously; microphone permission read through `AVAudioApplication`. The Xcode build is warning-free. The five macOS-only Core test failures (`/private/var` tmp aliasing, an ulp-level `CGRect.intersection` perturbation) are fixed so the Mac CI Core step passes.

### Verification

Swift Core: 814 tests passed on Linux and macOS (lane-guidance, curve-speed, following-distance and parked-trip tests new). `tools/model_migration/verify_model_assets.py` passed with the Intel manifest. Native iOS compilation (0 errors, 0 warnings), `ScreenTour`/`ExtraSettingsTour` and the unsigned road-test IPA were verified on a macOS machine with Xcode 26.6 because GitHub's hosted Mac runners were not acquiring jobs; physical-device behaviour of the Intel road mask, lane alerts and lane guidance remains to be checked on an actual drive.

### Still to verify on a device

- Intel road mask coverage at intersections, in rain, at night and around parked cars (see `tools/model_migration/ROAD-CANDIDATE-COMPARISON.md`); AR ribbon alignment with the new mask.
- Lane Departure / Cut-In false-alert rate on worn or missing paint; the lane model's provisional F1 of 0.80 was measured on CPU, not on the phone.
- Lane-level guidance on multi-lane roads: that UFLDv2 slot 3 / slot 0 reliably correspond to a real adjacent lane, and that broken-line detection does not misread long dashes as solid.
- Legal sign-off that the UFLDv2 training pool (comma10k, MIT) and the Intel/Apache terms meet the distribution plan.

## 26 October 2026

The user requested an ongoing audit and subsequently clarified that Koyeb is no longer used. Current hosting work targets Railway. Existing navigation changes were present at the start and have been preserved.

The user subsequently asked to emphasize the app's actual driving functionality. Current work prioritizes navigation, capture/GPS alignment, warning playback and useful recording recovery.

### Completed

- Prevent canceled check-in deadlines from resolving a rearmed session.
- Reject malformed speech-provider responses without disabling manual check-in responses.
- Reject non-ASCII invalid authentication headers cleanly.
- Preserve damaged pairing registries; report unavailable writes without replacing existing devices.
- Use elapsed time for pairing-code expiration, with exact-deadline rejection.
- Coalesce simultaneous voice-cache requests, publish complete files atomically and regenerate empty cache entries.
- Share a tested HTTP/HTTPS backend-address policy between connection settings and QR scanning. Restrict HTTP to canonical RFC 1918 IPv4 addresses and validate ports.
- Resolve Bonjour services to supported private addresses, retain active resolutions, run discovery on the main actor and clean up cancellation.
- Keep old recording cleanup from stopping new audio and migrate microphone permission APIs to iOS 17.
- Make the shared Railway launcher fail correctly when either service fails to spawn or exits unexpectedly.
- Exclude datasets, models, reports, worktrees and test outputs from deployment contexts. Exclude Photon opt-out/retry state from Git and Railway uploads.
- Reject malformed review PINs and Origin headers; preserve invalid remote-link configuration while keeping local review available.
- Repair outdated model-provenance test fixtures and make the optional LibreYOLO dependency explicit in the review test suite. Verify the real-gradient case separately in the model environment.
- Update the active README's hosted-backend instructions to Railway.
- Move speed-log logic into portable Core; validate and normalize replay measurements, preserve GPS/GPX discontinuities and parse quoted CSV fields without shifting columns.
- Prevent same-second trip logs and identical-name replay imports from overwriting prior files.
- Copy and parse imported replay files off the UI thread; reject canceled/stale completions and show import progress.
- Match automatic replay logs using the configured offset, reject invalid/backward video timestamps and report interrupted decoding.
- Preserve movies with damaged/missing recording metadata, expose unindexed clips for playback/export, and protect unresolved incidents from automatic cleanup. Validate recovered filenames, dates and manifest identity before using them.
- Repair missing incident copies from retained rolling footage instead of trusting the manifest entry alone; retain clips whose deletion fails for retry.
- Fix the drive-model gate that prevented starting an offline route despite an enabled Start button.
- Repeat interrupted navigation prompts using the latest maneuver; count a prompt as completed only after speech finishes. Add earlier speed-aware reminders and use the correct maneuver distance across silent provider steps.
- Timestamp live inference with the camera synchronization clock rather than callback arrival, preserving GPS alignment through delivery jitter and excluding stale queued frames.
- Retain ordered GPS batches for frame alignment; reject invalid future timestamps before they can block later fixes.
- Fall back to device brake speech when downloaded alert playback fails, and cancel superseded alert tasks before a check-in or pause.
- Recover transient camera resets and stalled frame delivery with at most three automatic retries. Pause estimates/recording across gaps, cancel retries on background/End trip, and require sustained healthy capture before resetting the retry budget.
- Show Retry camera and the actual camera status on Home and Camera while a trip remains active. Report capture as ready only after frames arrive.
- Avoid a second main-thread tracker clear canceling the capture queue's new field-of-view configuration during recovery.
- Anchor recorded segments to the first accepted frame's capture time, retain valid single-frame clips and end them using the available sample duration. Split at long frame gaps, source-clock resets or wall-clock discontinuities.
- Preserve millisecond timestamps in new clip/incident metadata while continuing to load existing whole-second metadata, maintaining replay/speed-log alignment after restart.
- Add native playback controls and Play all available clips to incident footage; play existing clips in time order while explicitly skipping unrecorded periods.

### Verification

See [VALIDATION.md](VALIDATION.md) for counts, environment details and limits. Current Core/native build logs, disposable Python environments and baseline dataset-test fixtures are under `test-results/improvements-20261026/`. The latest Core run passed 446 tests. Native debug app compilation/assembly is checked after functional edits; physical-device behavior remains unverified.

### Further audit areas

- Check navigation and recording behavior on an actual iPhone, including discovery after an address change, permission denial, interruption and foreground recovery.
- Continue reviewing local review tools, persistent-state recovery and asynchronous completion ownership.
- Verify camera-clock alignment and navigation prompt timing during a passenger-operated phone test. Test delayed frames, GPS batches, interrupted directions, offline route startup and invalid downloaded warning audio.
- Verify camera recovery after a media-services reset, interruption and stalled video on an iPhone; confirm no retry resumes capture after background/End trip and manual retry preserves the current trip.
- Verify short/single-frame movie finalization, slow encoder readiness, 24/30 fps clip boundaries and native queued playback on an iPhone. Recording metadata now preserves fractions; trip-history timestamps still use the older whole-second strategy and warrant an association check near trip boundaries.
- Add user-directed repair/discard controls for damaged incident metadata after exporting unindexed recordings; incomplete recovery currently preserves original footage conservatively.
- Review remaining compiler/dependency warnings without obscuring real failures or changing model behavior without evidence.

The ongoing goal remains active. No deployment, provider message, production label mutation or new training run was initiated by this audit.
