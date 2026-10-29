# N64 lane display and lead geometry

N64 follows inspection of V5.mp4, with its monitor playback, scene cuts,
performance pauses and visibly jagged lane output. It keeps the N63 model
packages and the completed baseline training separate. No annotations change.

## Display

Individual trained-slot predictions pass a display-only shape filter before
temporal smoothing. Gross local zigzags and jumps are rejected; small bin
jitter receives a bounded local median correction. Only compatible same-slot
fragments blend across actual captures. Current endpoints, anchor coordinates,
confidence and capture times remain unchanged. No opposite side is generated,
unsupported gaps are not joined, and current vehicle masks apply after smoothing.
A negative result, long gap, scene jump, rotation or session reset clears the
relevant smoothing history. New intersections introduced by smoothing revert
to the current unsmoothed observation. Freshness remains 0.5 seconds.

The actual V5 CPU candidate at 124.75 seconds has mean existence 0.789 but
severe local zigzagging. The new Swift regression rejects it and preserves both
normal 75-second candidates. These approximate compressed screen crops contain
the original HUD/lines and monitor artifacts. They cannot establish Core ML
parity, device speed or road accuracy. Smooth incorrect lines can still pass.

The decoder now reads each trained head once, with separate existing stock and
short-span selection gates, rather than decoding the ego heads again for the
display. The auxiliary latency budget includes decoding and display processing.
Thermal limits, vehicle priority and frame admission rules are unchanged.

## Corridor and lead selection

The UFLDv2 corridor fitter preserves the identity of trained ego-left and
ego-right heads. It requires both independent stock-gated row heads, ordered
boundaries, overlapping observed support, valid geometry and two distinct
captures. An outer head or a single erratic head cannot supply both sides.
Vehicle masks and unsupported anchor gaps split support. A new negative capture
invalidates earlier model measurements from that time onward while retaining
their validity for older vehicle frames. Derived visual caches cannot resurrect
that invalidated pair.

For lead selection, an already tracked rear-facing vehicle must still lie in
the current forward footprint before road-only fallback can authorize path
metrics. Previously, road membership and forward motion alone could continue
authorizing the same ID after it moved adjacent. The regression fails before
this fix and passes afterward. Fresh measured curved-lane support still takes
precedence. Vehicle detector weights and collision-approach rules are unchanged.

## Missing paint and road edges

The present training targets observed paint; road edges are separate labels.
The original CULane policy also annotates lane positions inferred from context
when paint is occluded or worn ([dataset paper](https://cdn.aaai.org/ojs/12301/12301-13-15829-1-2-20201228.pdf)).
That is a different training target, which explains why a CULane-trained model
can continue a boundary without visible paint. It does not prove any particular
prediction used a real road edge correctly.

N64 keeps unmarked-road lead/hazard support through its separate fresh road,
motion and fixed-camera geometry evidence. A wide road region alone does not
identify an ego lane on a multilane or two-way road. No new inferred-lane model
or road-edge-derived ego corridor is trained or claimed here. Reproducing that
learned contextual behavior needs a separately defined target and validation;
existing painted-line annotations should not be relabeled as guessed paint.

## Verification

All **585 release Core tests pass**, along with the release iPhone build and
all three finished-IPA model verifiers. Checks and source hashes are recorded
in `BrakeCheck_Dataset/reports/phone_video_V5/REPORT.md` and its package receipt.
Artifact: `BrakeCheck-N64.ipa`, 584,737,743 bytes, SHA-256
`70d3c30b81934e579975820412c3b5b8ee3e4c8f0494bd8bbacbbb19737b186e`.
Physical Core ML execution, alignment and sustained device latency remain
unverified. The active
baseline finished 20 epochs and is awaiting review. Its best stock validation
F1 is 72.08%; its exploratory short-span result is 79.78%. These are local
validation scores, not official benchmark, held-out test or iPhone accuracy.
N64 does not automatically substitute either new checkpoint.
