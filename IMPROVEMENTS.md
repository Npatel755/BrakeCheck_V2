# Ongoing improvement audit

## 4 November 2026 — commercial model clearance, lane alerts, lane-level guidance

### Completed

- Replaced the bundled NVIDIA SegFormer road model (non-commercial licence) with the Apache-2.0 Intel `road-segmentation-adas-0001` Core ML package that the repository had already converted and trialled. The tensor contract (`image` 640×384 RGB → `road_scores` [1,2,384,640]) is unchanged, so `LaneTracker`, the AR road mask, road-supported lead selection and camera-route alignment keep their existing decoding. Added `IntelRoad-MODEL-CARD.md` with source SHA-384/256 hashes, rewrote `NOTICE.md`, updated the lane manifest and the model verification scripts, and made SegFormer a retired model that CI rejects.
- Removed the AGPL Ultralytics YOLO resources (`YOLOModelData`, `ios/licenses`) from the repository; they were already excluded from the package but their presence complicated licence review.
- TestFlight builds now ship the lane and road models instead of stripping `LaneModelData`; the workflow verifies every bundled weight hash and licence and fails if a SegFormer or YOLO asset reappears. The build-check workflow runs the same verification.
- Offered Lane Departure and Cut-In Prediction as Premium "Lane alerts". Their engine, readiness messages and voice lines already existed behind an "offered" list; they remain gated on verified camera alignment, reliable GPS speed (≥ 18 mph for departure), a fresh non-paint lane pair with confidence ≥ 0.85, and pause during planned turns and route rejoins.
- Added lane-level navigation guidance (`LaneLevelGuidance` in Core). Using UFLDv2's four trained boundary slots it recognises the current lane among the visible painted lanes and, between 60 m and 25 s of travel before a turn or exit, shows "Move one lane right/left for the turn, when safe" or "Stay in this lane" in the turn banner and speaks the move once. A move is only suggested across a boundary that reads as broken paint (several pieces or incomplete coverage), never across a continuous line, so a two-way road's centre line never produces a left-move suggestion. Three consecutive fresh observations over 0.8 s are required; brake warnings, check-ins and active turn prompts take priority over the voice line.

### Verification

Swift Core: 795 tests passed (6 new lane-guidance tests). `tools/model_migration/verify_model_assets.py` passed with the Intel manifest. Native iOS compilation runs in the GitHub Mac workflows; physical-device behaviour of the Intel road mask, lane alerts and lane guidance remains to be checked on an actual drive.

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
