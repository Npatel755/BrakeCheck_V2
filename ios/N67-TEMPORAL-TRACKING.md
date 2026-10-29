# N67 temporal lane-guide test

Artifact: `BrakeCheck-N67.ipa` at the main workspace root. Version 0.4.0,
build 67, measurement revision N67. Install with the existing Sideloadly flow.

Open Settings > Camera display, turn on Show lane guides, and use Lane tracking
test to compare Raw, Averaging, and Kalman. Kalman is the default. Selecting a
different mode turns lane guides on and resets filter history on the next decoded
camera result. The existing replay screen can also exercise these options.

Raw means no temporal smoothing; the existing per-frame shape and vehicle-mask
filters still apply. Averaging retains the N66 display smoother. Kalman uses a
new native Swift position/velocity filter, capture timestamps, geometric fragment
association, uncertainty gating and reset rules. It handles the phone's roughly
3 Hz observation cadence, preserves current support/fragments and confidence,
and keeps original capture timestamps. A missing or stale observation immediately
clears its display history. The filter does not generate lanes through detection
losses. Optical-flow refinement and blind extrapolation remain offline comparison
options and are not included in this IPA.

This test affects lane-guide rendering only. The stock-gated corridor, lead
selection, warnings, calibration, model weights and training are unchanged. All
36 bundled model/provenance/license/font resource files match N66 byte for byte.

Validation: the arm64 release IPA build succeeded; all 634 release Core tests
passed, including 11 new Kalman tests. Tests cover 3 Hz jitter, duplicates,
missing observations, backwards/future timestamps, resets, jumps, reordered and
relabeled boundaries, unsupported gaps, column heads and ambiguous associations.
Finished-IPA CRC, version/revision, binary architecture, source hashes and bundled
resource hashes passed. Package/model verifiers check structure and provenance;
Core ML execution and sustained performance still require the iPhone.

The interruption left WSL launching into its system distribution. Validation ran
the already compiled release test executable inside the recovered Ubuntu root
with process-private proc/dev/Windows/temp mounts. No production source changed
after the successful native build, as verified against the build's source hashes.

Size: 584,930,867 bytes. SHA-256:
`9927bdeb8baa18c5849426c128a6c03f7905efaf2f0b29c157b399cb3e8f44c2`.
Evidence: `test-results/n67-temporal-20261023/` in this worktree, including
`n67-ipa-receipt.json`, `native-build.log`, and `core-tests.log`.

For comparison, use the same replay with each mode and look for jitter, curve
response, lane-change lag and disappearance behavior. A smoother incorrect line
is still an error. No physical phone installation or runtime validation was
performed by this packaging step. Prior IPAs are preserved.
