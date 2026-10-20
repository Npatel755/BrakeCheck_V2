# N59: optional Premium driving alerts

Adds Traffic Start Alert, Lane Departure, Cut-In Prediction, and Green Light
Notification under **Settings → Safety → Premium driving alerts** and on the
existing Premium screen. Every switch defaults to false. Purchasing or
restoring Premium does not turn any switch on. An inactive, expired, upgraded,
revoked, unavailable, or unverified entitlement cannot authorize these alerts.
The current StoreKit product remains
`com.brakecheck.prototype.premium.monthly`. App Store Connect configuration and
signed sandbox/device purchase checks are still required before paid release.
No local subscription override was added.

These are camera-based feature previews, not validated ADAS models. No new
weights are included. They reuse the existing detector, lane estimator, rear
view classifier, measured GPS and camera range. All remain opt-in, foreground
trip features; replay, demo, camera geometry changes and stale evidence cannot
issue reminders. Basic collision warnings and emergency check-ins remain free.

- **Traffic Start:** same confirmed rear-facing lead, confident stationary GPS
  at capture and delivery, at least two seconds of stable measured range, then
  sustained opening speed and increasing distance. Target loss/switch resets it.
- **Lane Departure:** verified camera alignment, measured driving speed, fresh
  high-confidence neural/hybrid lane boundaries, initial centered observation,
  then sustained gradual lateral drift. Paint fallback, abrupt geometry jumps,
  upcoming planned maneuvers and route rejoining do not authorize it. It cannot
  distinguish an intentional lane change from drift; no turn-signal input exists.
- **Cut-In:** repeated fresh rear-facing observations of the same tracked
  vehicle, normalized relative to a fresh lane, sustained inward motion, bounded
  size/vertical changes and a projected lane-entry window of at most two seconds.
  This is a kinematic candidate, not a trained maneuver-intent model. It requires
  camera alignment and measured ego movement.
- **Green Light:** signal crops from the existing full-frame detector, one clear
  central vertical head, sufficient native resolution/confidence, color dominance,
  stable housing overlap, sustained red then sustained green while stopped.
  Unknown color, missing signal, movement, multiple candidate heads and stale
  captures reset the transition. This limited RGB heuristic cannot determine
  the relevant lane, recognize arrows, handle horizontal heads or guarantee
  right of way. Wording is “Signal changed — check the road.” Detection depends
  on the active detector exposing the `traffic light` class. No separate model
  job or color-sampling work is started when the feature is off/locked.

Reminders last three seconds and each feature has a twelve-second cooldown.
They use a camera banner, haptic feedback and optional local speech. Existing
brake/check-in audio takes priority; reminders do not interrupt directions.
Disabling a feature or losing Premium access clears its visible reminder and
resets evidence. Background/trip/geometry resets also clear temporal state.

Validation: all 471 BrakeCheckCore tests pass, including 11 new tests for
default-off preferences, entitlement gating, distinct fresh observations,
stable-track movement, lane drift, red-to-green continuity, signal loss and
RGB rejection of white/yellow/mixed colors. Release build and packaged-resource
checks are recorded in `test-results/driving-alerts-n59/` at the repository root.
No physical iPhone road trial or StoreKit sandbox transaction was performed in
this environment. Simulator/synthetic tests do not establish field accuracy.

Before enabling paid distribution, check each reminder on a mounted phone with
recorded and supervised passenger-operated road trials, including no-alert
controls: intentional turns, curves, camera bumps, GPS loss, an adjacent car
moving in parallel, glare, adjacent-lane signals, arrows, yellow phases, and
background/replay transitions. Validate subscription restore, expiration and
revocation using the real signed App Store configuration.
