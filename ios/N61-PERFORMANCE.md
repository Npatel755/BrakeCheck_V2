# N61 performance audit

Based on merged N60 (`c3e1095`). Work is isolated on `perf/n61-smoothness`.
The user's reported symptom is choppiness on the navigation map or directions.

Implemented:
- The navigation map targets 60 Hz rather than a fixed 30 Hz. Low Power Mode and
  serious/critical thermal pressure retain a lower target; the actual rate is
  controlled by iOS. Map rendering still stops offscreen and respects gestures.
- Repeated representable updates no longer reset estimated-position smoothing.
  Successive service estimates interpolate toward their supplied positions
  without extrapolation; returning to measured/cached GPS resets estimate state.
- Routine route projections and offline road snapping use conservative spatial
  candidates. Exact geometry and existing ambiguity/freshness/recovery policies
  still decide matches; route progress falls back to the exhaustive path when
  no reachable nearby match exists.
- Immutable AR route geometry, cumulative distances, progress indexes and history
  keys prepare once, off the main actor. Start waits for preparation readiness.
  Route-library encoding/decoding and graph persistence run on an actor.
- Historical ETA patterns and last-location preferences avoid repeated full-route
  hashing and per-fix preferences writes.
- DriveModel and CameraARPresentation use Swift Observation. Live camera and risk
  UI are separate views so detections/replay frames do not invalidate the entire
  page shell and navigation map. Warning publication remains immediate.
- Mask contraction and row spans prepare with immutable mask identity on the
  model path. Canvas reuses spans; new warped masks retain distinct identities.
- Recorder encoding/storage work runs on its own serial queue. At most two frame
  operations retain capture buffers; lifecycle/incident operations preserve FIFO
  order. If storage cannot keep up, frame admission can reject recording samples
  instead of blocking live capture. Start preparation completes before capture.
- Vision admission preserves time-grid cadence despite jitter and busy workers.
  Thermal policy intervals, model assets, confidence and freshness guards remain.
- Patch tracking selects its needed stable top-k candidates without sorting
  complete windows. Exclusion-box expansion happens once per search.

Validation:
- 493 Core tests passed, including recording queue bounds/lifetime/ordering,
  cadence and thermal changes, stable top-k equivalence, exact mask contraction,
  indexed versus frozen N60 exhaustive progress matching, ambiguity, date-line/
  polar candidates, and estimated-position interpolation.
- A release-mode synthetic benchmark compares the exact N60 core sources and
  current sources; five repeats, matching checksums, with preparation costs
  shown separately. Run:
  `python tools/performance/run_core_benchmarks.py --baseline c3e1095`
- Local results (WSL x86_64, batches rather than individual frames):
  - 200 GPS updates on a 20,000-segment route: 150.48 ms -> 0.259 ms.
  - 100 AR projections on a 20,000-vertex route: 12.47 ms -> 0.602 ms.
  - 100 visibility-span scans of a 160x96 mask: 14.32 ms -> 0.0102 ms.
  - 100 offline snaps among 5,000 roads: 43.05 ms -> 0.0411 ms.
- These are algorithm timings, not iPhone FPS. New preparation increases one-time
  costs; route/index preparation moved off the main actor for this reason.

Device verification:
1. Install N61 and open Settings > Diagnostics > detailed measurements.
2. Under Performance, start recording while parked. Return to Camera/Navigate.
3. Reproduce the original symptom on the same route, then return and stop.
4. Prepare/share the performance report. It records bounded aggregate timings,
   display callback pacing and thermal-state counts, without images or positions.
5. Compare route following, turns, GPS gaps/recovery, pan/zoom/recenter, portrait/
   landscape, speech/alerts and a sustained trip under thermal load.

Display-link measurements describe callback scheduling; they cannot establish
compositor/rendered FPS. Physical-device tests and an Instruments rendering trace
remain necessary before claiming stutter is eliminated.

Remaining audit work:
- Measure the actual device's map camera/renderer cost at 60 Hz and sustained heat.
- SpeedLogWriter CSV writes and trip metrics/route finalization still perform
  filesystem operations on main. A bounded, ordered asynchronous persistence
  design is needed before changing completion/replay semantics.
- Optional Green Light sampling still uses Core Image CPU readback on the vehicle
  queue. It is default-off, but should get a separately bounded implementation
  with equivalent color/pixel validation before enabling it for profiling.
- Luma-copy/resampling and repeated visual tracking need device CPU traces before
  changing pixel kernels or sharing buffers. Accuracy and sample lifetime must
  remain verified.
