# BrakeCheck merge validation

## Current improvement pass — 26 October 2026

- Backend: **122 offline tests passed** in a fresh Python 3.13 virtual environment installed from `backend/requirements-dev.txt`.
- Photon: **18 tests passed** and TypeScript checking passed after `npm ci --ignore-scripts`.
- Shared Railway process supervisor: **4 offline tests passed**, including failed spawn, unexpected clean exit, intentional shutdown and forced termination.
- Swift Core: **446 tests passed**, including backend-address validation, replay log/import regressions, recording-library recovery, navigation voice ownership/distances, camera delivery timing, GPS observation ordering, camera-session recovery and recording-segment timelines.
- Native iOS debug compilation and unsigned `.app` assembly passed with `taskset -c 0,1 xtool dev build --configuration debug`. An unrestricted first attempt crashed inside Swift 6.4's dependency scanner; limiting concurrency avoided it.
- Dataset pipeline baseline: **1,579 passed, 2 failed**. The failures were an outdated fake-Torch fixture and LibreYOLO being absent from the review environment. After fixes, the affected review/remote/trial/detector subset reports **51 passed, 1 explicitly skipped** in that environment. All **6 detector tests**, including actual CPU forward/backward gradient checks, passed in the existing `.gpu-venv` where LibreYOLO is installed. No production training was launched.
- Remaining test warnings come from the installed Starlette/httpx compatibility layer. Existing native map/camera warnings remain.

This pass fixes stale check-in timers, malformed speech responses and authentication headers, pairing-store preservation, atomic/coalesced voice caching, private-address validation, Bonjour reconnection and audio ownership. It also excludes local data and Photon state from Railway/Docker upload contexts and hardens the dataset review server's malformed-input handling. Existing navigation work and actual human review data were preserved. No external messages or deployment were performed; physical iPhone execution remains unverified.

Replay follow-up: speed-log parsing/writing now lives in the portable Core module. Tests cover duplicate/invalid timestamps, exact readings around long gaps, quoted CSV fields, semicolon delimiters, namespace-aware GPX, segment boundaries, invalid coordinate/speed gaps, separate same-second trip files and non-destructive imports with identical names. Replay file copying/parsing runs off the UI thread, canceled completions cannot replace newer selections, log matching honors the configured time offset, and invalid/backward movie timestamps are skipped. Native debug compilation and unsigned app assembly passed after these changes. Durable logs are under `test-results/improvements-20261026/` (`core-tests.log` and `ios-debug-build.log`).

Functional driving follow-up: recording recovery uses a non-destructive library scan, validates paths/time windows/manifest identity, exposes unindexed clips for export, protects incomplete archives, and retries missing incident copies. Navigation now accepts offline-route startup, retries interrupted instructions from the latest fix, provides earlier distance reminders at speed, and accounts for silent provider steps when displaying/announcing turn distances. Camera inference uses converted synchronization-clock timestamps; stale delivery is rejected instead of becoming fresh measurement time. GPS batches retain ordered readings and future-invalid fixes cannot poison observation order. Downloaded brake-audio playback errors fall back to device speech, with canceled task ownership checked before playback/fallback.

The new Core tests verify these policies and disposable-file recovery. Native compilation verifies integration with the iOS SDK; it does not prove physical camera/GPS clock behavior, audible directions, downloaded-audio failure behavior, offline-button execution, or road accuracy. Those require device validation. No model weights or collision thresholds were changed by this follow-up.

Camera recovery follow-up: eight policy tests exercise retry ownership/deadlines, cancellation after stop/background, interruption handling, bounded repeated failures, healthy-capture recovery, stalled video, sparse frames and repeated/pre-restart timestamps. The native camera service uses that policy for media-services resets and missing-frame watchdog recovery, clears tracking/recording across interruptions, and exposes manual retry on Home and Camera. Main-thread display reset no longer invalidates a newly queued field-of-view configuration. Actual AVFoundation interruption/reset behavior and UI interaction still require a physical phone.

Recording timing follow-up: nine new Core tests cover accepted-frame anchoring, single-frame duration, 24/30 fps endpoints, gap/clock-reset splitting, ten-second rollover, invalid sample timing, delivery-delay correction, millisecond metadata round trips, legacy date compatibility and recovery of the new format from disposable files. Recording may preserve a delayed frame up to three seconds old using its real capture time; inference retains its existing 600 ms freshness gate. Clip/incident timestamps now retain milliseconds, and saved footage has native playback controls plus chronological multi-clip playback. Encoder behavior and playback controls are compiled but still require device execution; the timing tests alone do not prove movie encoding or audible/interactive playback.

The sections below are historical validation records.

Validated in the Linux workspace on 2026-10-10.

Earlier sections record the 2026-10-10 workspace checks. The current run is
listed first; older "not performed" notes describe that earlier environment.

## Current checks (2026-10-15)

- Latest UI changes move Diagnostics into Settings, remove the Premium bottom tab, and show the Premium page after the first-trip rating prompt before its saved summary. The camera direction card retains the planned instruction without a live fix but hides stale turn distance. A cached starting position can start route guidance; live progress, reroutes, and turn announcements still require reliable current GPS. The iOS Release compile passed; these flows still need an on-device acceptance test.
- The navigation appearance control moved into Settings; only Blue and Polar night remain. A first saved trip asks for a rating before its route summary; later trips open that summary directly. Premium uses StoreKit 2 with the configured monthly product ID described in `ios/PREMIUM-SETUP.md`.
- App inactive/background transitions now stop the foreground driving pipeline promptly. Stale GPS can position a route preview but never drives turn instructions or a camera-aligned route.
- Backend offline suite: **64 passed**. This includes check-in persistence, restart recovery, stale-alert suppression, and agent retry using the same event ID. No provider messages were sent by these tests.
- Photon agent: **18 offline tests passed** and strict TypeScript type checking passed. These checks do not verify delivery by the live provider.
- Bundled day/night detectors, UFLD, SegFormer, and viewpoint weights passed SHA-256 asset verification. This does not verify Core ML execution or frame rate on an iPhone.
- iOS Core suite: **425 passed** in WSL. The iOS Release compile covers the imported `claude/v1` home, navigation banner, trip, and theme UI alongside the existing safety service. No IPA was packaged.
- The app and backend still need on-device and live-service acceptance tests. A successful build does not establish warning accuracy, camera frame rate, AR alignment, or actual contact delivery.

## Completed

- **32 Python offline integration cases passed before this QR-pairing update.** Coverage includes authenticated routes, missing configuration, voice failure, optional/invalid GPS, start/arm idempotency, spoken and manual responses, backend timeout, simultaneous resolution, no duplicate sends, dry-run isolation, zero-recipient failure, network failure, transcription fallback, upload size limits, contact invitation/consent/opt-out/removal, and trip forwarding.
- **TypeScript strict typecheck passed** against the supplied pinned Spectrum SDK 12.10.1.
- **6 offline Photon logic tests passed**, covering input validation, missing coordinates, possible-impact wording, location/status/trip replies, and honest local-only footage handling.
- **28 original Swift files syntax parsed without errors** using the tree-sitter Swift grammar. This is a syntax check, not Swift type checking or an iOS compilation, and did not include the new pairing files.
- Renamed package/target/import paths checked for consistency; plist parsed and required privacy descriptions checked.
- Bundled Core ML model data compared with the supplied model. Model bytes were not changed.
- Packaged archive checked for excluded secrets, vendor credential values, dependencies, contact data, and build caches.

## Not performed here

- No Swift compiler, Apple SDK, xtool device build, or iPhone was available. Core XCTest execution, SwiftUI layout rendering, native type checking, code signing, and install remain unverified.
- No real ElevenLabs call was made with the supplied credentials.
- The Photon agent was not started and no iMessage/invitation was sent. SDK type checking does not validate the live account, Photon user limits, permissions, routing, or delivery.

## QR pairing update

- Backend source passed Python `compileall` after adding pairing and Bonjour code.
- Pairing integration coverage was added for one-time code use, token-hash storage, device authentication, and revocation. It could not be executed in this workspace because `pytest`, FastAPI, Requests, and the project's other backend dependencies are unavailable here.
- The new Swift QR scanner, Bonjour discovery, Keychain token flow, and startup reconnect were not compiled or run. The current workspace has no Swift compiler or Apple SDK.

## Koyeb and test-message update

- Python source compilation, `bash -n` on the Koyeb deploy script, and Node syntax checks on the cloud process launcher passed.
- The Photon logic test file passed using Node's built-in TypeScript stripping; the TypeScript compiler is unavailable, so this does not replace `npm run typecheck`.
- A fake Koyeb CLI verified the deploy script stages source without a dummy `.env`, contacts file, paired-device store, virtual environment, or `node_modules` directory.
- Backend integration tests could not run in this workspace because `pytest`, FastAPI, and Requests are not installed.
- The Photon package typecheck could not run because project `node_modules` and the TypeScript compiler are not installed. No external provider call or text message was sent.
- Docker image build, Koyeb deployment, iPhone compilation, and live test-message delivery remain unverified here. Follow `cloud/README.md` and confirm receipt of a test message on the intended contact's phone before relying on the integration.

## Device acceptance checks

1. Run `xtool dev build`, then `xtool dev`. Confirm the app is named BrakeCheck and opens in landscape.
2. Check camera, precise GPS, detector startup, recording, calibration, incident playback, and sharing using the existing sensor workflow.
3. Pair by scanning the QR from `python pair.py`. For Koyeb, pass the HTTPS URL with `--backend-url`. Confirm the QR expires after five minutes, cannot be reused, and the phone reconnects after relaunch. Also test Advanced manual connection for a LAN backend and provider status.
4. Enable the microphone and run a dry check-in. Verify the prompt finishes before recording begins; try an okay reply, help reply, silence, and a manual button.
5. Try microphone denied, ElevenLabs unavailable, missing GPS, a locked phone, and a disconnected backend. Confirm no fake location or successful-delivery claim appears.
6. With the contact's consent, register and invite them. Verify YES, STOP, re-opt-in, and deletion. Tap **Test message**, confirm the warning, and have them verify the location-free message arrives. Check cooldown behavior before a live check-in.
7. Run a deliberately authorized live check-in and verify one message arrives; check where/status/clip/drive replies. Do not treat `sent` as a read receipt.
8. With a persistent `/data` volume, restart the backend during an active check-in. Verify the same countdown and event ID resume, and an unfinished alert request does not create a second message. After an outage longer than five minutes, verify the app reports the alert as expired or unconfirmed rather than sending a late emergency alert. Keep one Koyeb instance and one Python worker.

The app remains an experimental prototype; these software checks do not validate collision detection or emergency response performance.
