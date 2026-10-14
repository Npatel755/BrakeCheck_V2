# BrakeCheck iOS / xtool

The complete setup guide is **[../README.md](../README.md)**. Build from this directory:

```bash
xtool dev build -c release --ipa
xtool dev run -c release
```

Use the Release IPA for on-device performance checks. Settings responsiveness
and camera CPU work depend heavily on Swift optimization. Build 34's fixes and
validation are recorded in [N34-SETTINGS-PERFORMANCE.md](N34-SETTINGS-PERFORMANCE.md).

- iOS 17+, landscape-right camera pipeline, Swift 6 toolchain with Swift 5 language mode.
- App product: `BrakeCheck`; math/storage module: `BrakeCheckCore`.
- `Info.plist`: camera, precise location, motion, local network, Bonjour, and microphone descriptions.
- `Sources/BrakeCheck/Resources/YOLOModelData`: copied as ordinary package data so xtool does not need to run Apple's Core ML compiler. The tracker reconstructs an `.mlpackage` and compiles it on the iPhone.
- `Sources/BrakeCheck/App/DriveModel.swift`: trip lifecycle, sensor integration, speech-trigger transitions, impact check-ins, and trip summaries.
- `Services/BackendClient.swift`: authenticated requests, QR pairing, and per-phone Keychain tokens.
- `Services/BonjourServiceDiscovery.swift`: finds the backend after pairing and when its local IP changes.
- `Services/VoiceAudio.swift`: playback completion, short reply recording, and local spoken-warning fallback.
- `Services/SafetyService.swift`: connection, contacts, check-in flow, result polling, and demo isolation.
- `Views/ContentView.swift`: redesigned driving dashboard. Contacts, settings, check-in, and diagnostics have separate views.

First launch: run `python pair.py` on the computer and scan its QR from Settings. Allow Local Network and Camera access, then allow microphone access and run the message-free check-in demo. See the root validation report for remaining physical-device checks.
