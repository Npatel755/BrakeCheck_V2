# N71 Premium alert fixes

Navigate now renders the same optional-alert banner as Camera. Both use shared
visibility rules: active live trip, foreground, Premium access, no collision
warning/check-in/selection UI, and an alert no more than three seconds old.
Replay/demo cannot display a live reminder through this path.

The Premium and Safety pages now show each optional alert's current status.
The text identifies disabled access, inactive capture, missing camera alignment,
GPS requirements, unavailable lane/lead measurements, and signal requirements.
These statuses only explain waiting conditions; they do not enable detection
or relax its evidence requirements. "Watching" is not proof that a temporal
event has armed or that an alert will trigger.

Traffic-start history now ignores duplicate observations from the same capture,
instead of resetting its stopped-lead history. Backward timestamps, stale data,
changed identity, missing evidence, and disabled access still require rearming.

Core release validation: 682 tests passed, including duplicate-callback and
rearming regressions, readiness explanations and banner gating/expiry. Three
portable PremiumStore behavior variants passed (normal, test, persisted opt-in).
Normal Package.swift still has no local Premium testing compiler flag; only
the staged N71 sideload target enables it. No model weights changed.

Use Home > Premium > Enable Premium for testing, then enable individual alerts.
Their prerequisites remain conservative. Green-light reminders require a single
clear vertical signal; horizontal/multiple signals are unsupported. Lane
departure and cut-in require verified camera alignment and fresh reliable lanes.
Traffic-start needs real stationary GPS, a confirmed rear lead and gap/range-rate
measurements. Replay and demo do not exercise these live events.

Compilation and automated checks do not establish physical iPhone behavior.
The package still requires sideloading and controlled device validation; it
has not been installed or driven on the user's phone by this agent.
