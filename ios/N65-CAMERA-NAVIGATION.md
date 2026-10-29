# N65 camera and navigation refinement

N65 follows the N64 lane stability work. It keeps version 0.4.0 and the same
bundled detector, lane, road and viewpoint weights.

## Camera and trip lifecycle

- Capture timestamps preserve actual frame age when translating the camera
  clock to uptime. Invalid, repeated and future timestamps cannot become fresh
  inference evidence.
- Interrupted or failed capture clears stale tracking evidence. The camera view
  provides a retry action or a Settings link when appropriate.
- Filtered GPS speed CSV creation, formatting, writes and final flush run on a
  private serial queue. Admission retains at most 64 samples. Slow storage drops
  a sample and leaves a real timestamp gap instead of delaying a GPS callback.
- Rapid trip restarts create distinct CSV files and keep each trip's accepted
  rows together. Trip finalization waits for both recording and GPS CSV closure
  before allowing the summary or subsequent presentation.

## Navigation

- The position marker and following camera share one smoothed location, removing
  extra layers of filtering that delayed the displayed position.
- Measured/estimated GPS handovers preserve display continuity, while expired
  estimates and stale fixes retain their safeguards. Measured GPS is not
  extrapolated beyond the accepted fix.
- Along-route search starts at measured route progress or a projected position
  within the current segment. It retains the remaining part of long segments
  and spaces search locations by road distance, including on looped routes.

## Validation

The complete release Core suite passed **621 tests, zero failures** before the
final error-formatting lock change. All **13 queued speed-log tests passed**
after that change. They cover actual temporary CSV files, same-second restart
isolation, invalid GPS values, bounded admission during blocked storage, errors,
completion order and off-main I/O.

The final N65 iPhone release compilation and IPA packaging passed. The finished
archive passed CRC, arm64, build/revision 65, version 0.4.0, and every bundled
model file/hash check. A separate lifecycle review found no actionable issue.

Artifact: **BrakeCheck-N65.ipa**, 584,840,154 bytes, SHA-256
`2a68e5b1358922b26961b3d82545cf38be5830c9da55a93e86799a9f57525b65`.

Commands, logs and receipts live under
`test-results/iphone-refinement-20261021/`, including `n65-ipa-receipt.json` and
`n65-published-artifact.json`. Existing IPAs and all model weights are preserved.

Linux Core tests and cross-compilation cannot establish physical iPhone FPS,
Core ML latency, preview/sensor alignment or sustained thermal performance.
Device checks should exercise a sustained trip, navigation follow/pan/recenter,
GPS loss/recovery, rotation, camera interruption/retry, and rapid trip stop/start.
