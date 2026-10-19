# BrakeCheck merge validation

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
