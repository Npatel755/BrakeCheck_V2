# N25 — Numeric speed and blue appearance

Restored the large numeric speed readout and removed SpeedometerView. Estimated GPS and assumed replay readings remain labeled. Trip history and automatic completed-trip saving remain available.

Light appearance uses a white background, pale blue panels and blue accents; dark appearance uses black, dark blue panels and brighter blue accents. The logo wordmark, decorative icons, page controls, lane/road overlays, vehicle boxes, navigation routes and destination marker share the blue palette. Warning/critical colors keep their risk meanings, with darker amber/orange for light-mode contrast.

Removed Camera's forced dark color scheme. Its readouts, status capsule, calibration controls, placeholder and letterboxing now follow the shared appearance preference. White top/bottom scrims provide contrast for dark light-mode text over the live road; dark appearance retains black scrims and white text. The navigation danger pill also follows appearance, and existing route renderers/markers recolor when the mode changes.

Validation: native release compilation and IPA integrity, revision, executable identity and eleven model-resource checks. No Core algorithms changed. Device checks remain: toggle appearance during live Camera, replay and an active route; inspect bright/dark footage in both orientations; check critical warnings, unavailable/estimated speeds and saved trip history.
