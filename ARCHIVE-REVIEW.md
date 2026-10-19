# BrakeCheck archive review — 2026-10-10

The uploaded ZIP contained the correct camera/curve update files, but its iPhone project was incomplete. The corrected archive restores 37 missing original iPhone files, including the Swift package manifests, xtool configuration, Info.plist, app entry point, supporting services/views, original core sources, and bundled YOLO model data. Every file supplied in the uploaded ZIP has been preserved byte-for-byte.

## Validation completed

- 55 focused Swift core tests passed: lead selection, lane geometry, driving calculations, and retention.
- All 22 iPhone app source files passed Swift syntax parsing.
- All 12 Python source files, 8 JSON files, and Info.plist parsed successfully.
- The cloud launcher passed Node syntax checking.
- All 7 Photon logic tests passed in an offline Node 24 TypeScript test harness. No messaging agent or live services were started.
- Backend, Photon, and cloud source files match the preceding complete project after normalizing line endings.
- The ZIP passed its integrity check and contains the YOLO model and weights.

## Remaining validation

This is not a full iOS build or physical-device validation. Run `xtool dev build` from the extracted project's `ios` folder, then reinstall the new IPA with Sideloadly and test the camera and lead tracking on the phone.

The original GPSFilterTests are from an earlier GPS policy and were not part of the focused passing suite. Several expect immediate stationary output, unsmoothed speed, or acceptance of simulated fixes, which conflict with the current GPSFilter behavior. The complete core suite is therefore not claimed to pass.

The FastAPI integration tests were not run in this environment because their Python dependencies were unavailable. Python syntax checks do not validate backend runtime behavior.

The curve update uses lane-marking image processing and Apple Vision object tracking; it does not add a pretrained neural lane model. Detection reliability, camera layout, and performance still need device testing.

## Restored files

- `ios/.gitignore`
- `ios/.vscode/extensions.json`
- `ios/.vscode/tasks.json`
- `ios/Core/Package.swift`
- `ios/Core/Sources/BrakeCheckCore/DriveMath.swift`
- `ios/Core/Sources/BrakeCheckCore/GPSFilter.swift`
- `ios/Core/Sources/BrakeCheckCore/RetentionPolicy.swift`
- `ios/Core/Tests/BrakeCheckCoreTests/DriveMathTests.swift`
- `ios/Core/Tests/BrakeCheckCoreTests/GPSFilterTests.swift`
- `ios/Core/Tests/BrakeCheckCoreTests/RetentionTests.swift`
- `ios/Info.plist`
- `ios/MODEL_INFO.json`
- `ios/Package.swift`
- `ios/README.md`
- `ios/Sources/BrakeCheck/App/BrakeCheckApp.swift`
- `ios/Sources/BrakeCheck/Resources/YOLOModelData/Data/com.apple.CoreML/model.mlmodel`
- `ios/Sources/BrakeCheck/Resources/YOLOModelData/Data/com.apple.CoreML/weights/weight.bin`
- `ios/Sources/BrakeCheck/Resources/YOLOModelData/Manifest.json`
- `ios/Sources/BrakeCheck/Services/BackendClient.swift`
- `ios/Sources/BrakeCheck/Services/BonjourServiceDiscovery.swift`
- `ios/Sources/BrakeCheck/Services/CameraService.swift`
- `ios/Sources/BrakeCheck/Services/ImpactService.swift`
- `ios/Sources/BrakeCheck/Services/MotionSpeedFusion.swift`
- `ios/Sources/BrakeCheck/Services/SafetyService.swift`
- `ios/Sources/BrakeCheck/Services/SegmentRecorder.swift`
- `ios/Sources/BrakeCheck/Services/SpeedService.swift`
- `ios/Sources/BrakeCheck/Services/VoiceAudio.swift`
- `ios/Sources/BrakeCheck/Views/BrakeTheme.swift`
- `ios/Sources/BrakeCheck/Views/ContactsView.swift`
- `ios/Sources/BrakeCheck/Views/CrashCheckinView.swift`
- `ios/Sources/BrakeCheck/Views/IncidentListView.swift`
- `ios/Sources/BrakeCheck/Views/PairingScannerView.swift`
- `ios/Sources/BrakeCheck/Views/SettingsView.swift`
- `ios/THIRD_PARTY_NOTICES.md`
- `ios/VALIDATION.md`
- `ios/licenses/Ultralytics-AGPL-3.0.txt`
- `ios/xtool.yml`
