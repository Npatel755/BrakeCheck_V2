# N39 warning and camera HUD

The v19 screen recording shows build N38 in LIVE mode filming road footage
on a laptop. The iPhone has no reliable GPS speed. At 29-30 seconds the
estimated gap closes to roughly 18 ft with about 3 mph closing speed, but
estimated closing time remains above the 2-second camera critical threshold.
At 31-33 seconds the target becomes clipped or lost and fresh gap/TTC fields
become unavailable. The displayed High risk is a three-second visual hold,
not a current critical risk. At 34-35 seconds the target reappears around
6 ft, but the apparent closure is 0 mph as the source footage stops.
No critical risk was measured, so no BRAKE NOW warning was authorized.
A video crash on a separate display is not a physical phone impact.

The camera pill now says RISK UNAVAILABLE while a previous score is visually
held and the current estimate is absent. The gap and closing-time placeholders
name a clipped vehicle or detection gap where known, rather than merely
saying Needs target. This clarifies missing evidence without generating a
BRAKE NOW alarm from proximity alone. Current camera TTC warnings remain
independent of GPS when reliable visual range motion is available.

In landscape only, the direction card moves to the top-right and is capped
at 280 points wide. Portrait placement stays the same. The brake risk pill
remains centered below the top header.

Validation: optimized iOS IPA build 39 succeeded; both model/IPA verifiers passed package hashes and archive CRC. The 413 Core tests passed on unchanged Core sources in N37. A genuine
fixed-mount moving-vehicle drive, or original dashcam replay with synchronized
camera and speed data, is needed to evaluate the warning timing; screen-filming
introduces motion/geometry that is unrelated to the iPhone's vehicle motion.
