# BrakeCheck: App Store and navigation roadmap

Updated October 12, 2026. This report describes the current N14 prototype and the work needed for a public release. App Store approval has not been established.

## Current behavior

N14 provides Home and Camera pages with a shared driving pipeline. Moving between those pages keeps the trip running. Switching to another app or locking the phone pauses camera capture, recording, and vehicle tracking; it clears current collision estimates and stops impact sensing. Returning to BrakeCheck restarts the camera for an active trip. The brief background task only finishes the current video segment.

`SpeedService` sets `allowsBackgroundLocationUpdates` to `false`. N14 removes the unused location background mode, corrects the location permission description and camera pause message, and explains on Home that camera tracking requires BrakeCheck to remain on screen. Check-in audio stops in the background; an already-started backend check-in can retain its server-owned countdown.

The N14 IPA is an **unsigned sideload build**. Signing it for installation through Sideloadly does not create an App Store distribution archive or establish release readiness.

## How navigation can coexist with detection

The recommended next architecture is **navigation inside BrakeCheck**, keeping the camera pipeline in the foreground. A future MapKit integration would add destination search, route calculation, route progress, and spoken directions while retaining the Home/Camera structure. None of this navigation functionality is implemented yet. MapKit's `MKDirections` supplies route calculation; completing a navigation experience also requires progress tracking, rerouting, and device testing. [Apple: MKDirections](https://developer.apple.com/documentation/mapkit/mkdirections)

External Maps can run on a **second device** while the mounted BrakeCheck phone stays visible. This is the simplest arrangement to test with the current app. Navigation on the same phone currently interrupts BrakeCheck's visual pipeline when Maps becomes the foreground app.

An immediate single-phone option is to start a route in Apple Maps, then return to BrakeCheck and use Maps' spoken directions. Apple documents that turn-by-turn directions continue when another app opens. This keeps BrakeCheck visible but does not put a navigable map inside its UI. Navigation audio and BrakeCheck alerts still need testing together. [Apple: driving directions](https://support.apple.com/en-euro/guide/iphone/ipha84a94043/ios)

If the requirement is camera-derived collision warnings while arbitrary other apps occupy the phone, evaluate a **separately processed external camera** that detects hazards outside the iPhone app and delivers events to it. Hardware capabilities, connection behavior, event latency, background delivery, audible alerts, and failure reporting must be validated before promising this behavior. An external camera merely streaming frames to a suspended app would not solve the processing problem.

Apple documents a camera interruption when the video device is unavailable in the background. Multitasking camera access has a runtime support check; it should not be assumed for every device or app configuration. Apple's camera Picture in Picture guidance concerns video calls and does not establish eligibility for a dashcam feature. Background modes must serve their intended purposes under App Review guideline 2.5.4. [Camera background interruption](https://developer.apple.com/documentation/avfoundation/avcapturesession/interruptionreason/videodevicenotavailableinbackground), [multitasking camera support](https://developer.apple.com/documentation/avfoundation/avcapturesession/ismultitaskingcameraaccesssupported), [video-call Picture in Picture](https://developer.apple.com/documentation/avkit/adopting-picture-in-picture-for-video-calls), [App Review guidelines](https://developer.apple.com/app-store/review/guidelines/)

Background location could be a separate future feature for trip logging, with permission, visible trip state, and a clear stop control. It would not provide the missing camera observations needed for collision estimates. [Apple: background location updates](https://developer.apple.com/documentation/corelocation/cllocationmanager/allowsbackgroundlocationupdates)

## Concrete release work

| Area | Current evidence | Required next step |
| --- | --- | --- |
| Distribution | `xtool.yml` uses `com.brakecheck.prototype`; the current artifact supports sideloading. | Choose the production identifier and Apple developer team; create a reproducible Xcode archive/export workflow on a Mac, then validate a signed TestFlight build. Include the existing Core package and model resources. |
| SDK baseline | The N14 Mach-O records SDK 17 and minimum iOS 17; this sideload build is not a supported store submission. | Build with Xcode 26 or later and the iOS 26 SDK or later, as required since April 28, 2026. Validate toolchain, signing, archive contents, and upload results. [Apple requirement](https://developer.apple.com/news/upcoming-requirements/?id=02032026a) |
| Privacy manifest | No `PrivacyInfo.xcprivacy` is present. Code uses user defaults, system uptime, and disk-capacity APIs. | Audit required-reason APIs, select reasons matching actual use, and bundle the manifest. Verify the resulting archive and privacy report. |
| Privacy and retention | Video folders use file protection and are excluded from backup. Incidents can be deleted and shared. Speed logs persist in Documents without a retention/deletion screen. | Publish privacy/support information; add speed-log retention and deletion controls, plus unpairing and backend device-token revocation. Document retention of backend data. |
| Consumer backend | Settings assumes QR pairing with a separately managed backend or manual configuration. | Provide secure consumer enrollment and a maintained HTTPS service, with a usable review/demo path. If accounts are introduced, include account and associated-data deletion. |
| Data disclosures | A configured backend receives trip summaries; check-ins can send location/speed and spoken replies. Contacts include names and phone numbers. Replies go through the backend to ElevenLabs. | Make permissions, processing, providers, retention, and App Store privacy answers match these flows. Preserve confirmed-contact enrollment and explicit emergency check-in controls. |
| Model distribution | Repository notices identify Ultralytics model terms; other bundled models also have provenance/notices. Top-level notices are outside the Swift package resource list. | Audit licenses and provenance for all shipped weights, including the night detector, resolve the distribution approach, and package the applicable notices. No licensing conclusion is made here. |
| Product validation | Core tests and sideload builds verify parts of the implementation. | Validate the signed build on target iPhones: visibility/background transitions, navigation integration, voice interruptions, camera mounting, heat, low storage, denied permissions, stale measurements, and check-in failure/recovery. Keep detection and risk estimates described accurately. |

The next release decision is the navigation architecture. Until that decision is implemented and verified, N14's supported driving mode remains foreground camera operation.
