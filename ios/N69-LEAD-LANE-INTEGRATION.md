# N69 lane-aware lead selection

N69 follows V8 inspection. It retains N68's filled corridor and N67's temporal
display choices, with identical detector/lane/road/viewpoint weights. It adds
independent measured-lane evidence for lead search, viewpoint scheduling and
selection. This works with lane guides hidden and cannot read the Kalman-smoothed
display as driving evidence.

Fresh strong single-sided ego observations can mark a vehicle as outside an
observed boundary. This redirects recovery/classifier work and pauses that
vehicle's path support; it cannot create the other side or bypass rear/direction
checks. Changed and expired observations release the constraint, allowing cut-ins.
Consistent two-sided observations can additionally confirm a lead-specific
geometry path over three independent captures. The stock corridor remains
preferred. Collision-hazard selection and AR use their unchanged original paths.

The pair route requires 0.82 per-side score, eight points per fragment, shared
span >=0.14, shape checks and three captures spanning >=0.30 s with bounded changes.
Single-sided exclusion requires 0.80, eight shape-filtered points, supported box
footpoint and an outside margin. Capture-ordered buffers prevent future or
negative observations from resurrecting old geometry. These are experimental
selection gates, not calibrated probabilities or a commercial driving validation.

Settings > Diagnostics > Camera and models now exposes lead-lane evidence state.
No new setup switch is required. Existing Raw/Averaging/Kalman and Filled
corridor/Lines controls affect rendering only.

Main-workspace evidence: `BrakeCheck_Dataset/reports/phone_video_V8/REPORT.md`.
The video crop screening marks the adjacent right SUV outside the lane at 144,
146 and 148 s while leaving the central candidate unexcluded. The new pair route
does not confirm on those 41 probes; single-sided relevance supplies the measured
improvement. Original-buffer/Core ML phone confirmation remains to be tested.

All 661 release Core tests pass. Build and IPA verification evidence is in
`test-results/n69-lead-lane-20261023/`. Final package receipt records source hashes,
version/revision, architecture, CRC and comparison with unchanged model resources.

Final main-workspace artifact: `BrakeCheck-N69.ipa`, 585,039,715 bytes, SHA-256
`887d849146264daee64c83c1a3dafc6bfe305c38d7a5fa20635908a74d4131fd`.
IPA checks passed, including 36 unchanged N66 resources and all model contract,
provenance/license and archive checks. Apple runtime parity and installation
remain physical-device work.
