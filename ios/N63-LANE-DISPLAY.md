# N63 lane prediction display

N63 addresses hidden lane predictions found while inspecting the user's V4.mp4
screen recording. It retains the exact N62 UFLDv2 epoch-17 model package and
all other model weights. The active baseline training continues separately.

## Evidence

A CPU probe of approximate landscape camera crops from the compressed recording
found a plausible ego-left boundary at 10 seconds, discarded by the stock
37-anchor minimum despite 32 positive anchors and 0.918 mean existence. At
25 seconds a stock ego-right boundary survived with 41 anchors and 0.847 mean
existence, but a lone boundary cannot form the existing two-sided corridor.
At 20 seconds all assigned heads predicted absence. Display changes cannot
recover evidence the model does not predict.

These samples include screen overlays and do not reproduce the original
camera feed or prove physical Core ML numerical parity, latency or accuracy.
The full report and reproducible probe are in
`BrakeCheck_Dataset/reports/phone_video_V4/` in the main workspace.

## Changes

- Render individual model boundaries independently of the paired safety
  corridor, including a single side and the trained outer slots.
- The separate display decoder uses at least four positive and valid anchors
  with mean existence at least 0.75. Its validation-tuned confidence is not a
  calibrated probability of correctness or a new benchmark score.
- Keep original capture timestamps and expire raw guides after 0.5 seconds.
  Split unsupported anchor gaps and omit vehicle-overlapping segments. Do not
  synthesize an opposite boundary or extend endpoints.
- Clear display observations after rotation, trip/tracking resets, camera
  stop, demo and detector changes. Recheck freshness while drawing.
- Show concise camera status for loading, predictions, delayed inference,
  performance pauses, model errors, or searching for painted lines.
- Load/compile the independent SegFormer road model on its road queue so it
  does not block the lane inference queue after lane-model readiness.

The relaxed observations never enter LanePath, corridor fitting, calibration,
collision warnings or AR alignment. Stock corridor gates, thermal admission,
vehicle inference priority and timestamp rules remain in force. Existing
short-term lane continuity is no longer drawn after corridor freshness expires.

## Validation

All 525 release Core tests pass, including 12 display tests for confidence,
partial boundaries, unsupported gaps, vehicle masks, invalid inputs and
freshness. The release iPhone build and all three finished-IPA model verifiers
passed, covering model hashes, output contracts, provenance, licenses and CRC.
The artifact is `BrakeCheck-N63.ipa`, 584,701,143 bytes, SHA-256
`34c4645ef29cfb23675ea245e9e8da7c6a123964e090879dbda5d87d35edab42`.
The V4 report contains the packaging receipt and verifier logs.
Device Core ML execution, alignment and sustained
latency still require an iPhone run; the screen recording cannot establish them.
