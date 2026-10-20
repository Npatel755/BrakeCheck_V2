# N57 navigation integration on main_v3

This change is based on `main_v3` commit
`75383ad9dae9c0ac2097646b14a448df944b27d9`. N57 follows the user's current N56
installation. The checked-in plist previously held build 52 and a stale static
measurement revision; both bundle values are now 57/N57. DriveModel retains its
newer dynamic revision lookup from the bundle.

## Result

- Route alternatives with time/distance comparison, toll/highway preferences,
  scheduled departure, ordered stops, arrival, voice and manual pause/resume.
- Measured route progress and GPS confidence, reliable snapping, short bounded
  map-only estimates, off/rejoin detection and automatic rerouting.
- Fuel/charging/parking/food/coffee and route corridor searches, saved places,
  recent destinations, share sheet, stored routes and offline coordinate entry.
- Legal directed offline routing and rerouting using an imported prepared graph
  or complete supported regional OSM JSON; source attribution remains visible.
- Traffic refresh across the remaining trip, delay/better-route notices,
  local completed-trip history and measured stop-and-go detection.
- Traffic/imagery/3D/heading/night/color map settings, native Look Around,
  automatic approach zoom with manual overrides and camera mini-map.

The newer main_v3 theme, fonts, camera capture, AR ribbon/calibration, trip
history, purchases and safety pipeline are preserved. Camera projection still
requires measured GPS/course, an unambiguous match and the existing mount/scene
confidence gates. Offline/full-trip routes feed the existing projection through
the same gate and a stable cached route polyline. Estimated map positions never
feed camera projection or collision/speed calculations.

## Correctness checks

The Core suite passes **460 tests with zero failures**. Added checks cover saved
route compatibility and bounds, directed routing/turn restrictions and parallel
roads, multistop legal continuity, actual routed stop endpoints/milestones,
disconnected Apple-style leg rejection, OSM access/restriction rules, meaningful
instruction merging, route snaps under the newer ambiguity gates, GPS outage
estimate expiry, observed ETA and comparable-trip history.

The service integration also handles superseded route/traffic/import requests,
offline-toggle cancellation, failed-plan retries, preserved completed stops,
paused guidance and expired display positions. Old alternatives cannot satisfy
a newer itinerary/preferences request. Faster-route acceptance recalculates from
the latest fix. Provider traffic refresh uses the same ordered-leg planner and
continuity checks as the preview. Saving errors are reported rather than claiming
that a route has been persisted.
Starting midway through a saved route is excluded from comparable full-trip
history, so it cannot teach the estimator a partial duration for the whole route.

Native release compilation and packaging use:

```sh
cd ios
xtool dev build --configuration release --ipa
```

Build logs, source/resource fingerprints, Core test results and artifact
verification are in `test-results/navigation-main-v3/`. The final artifact check
validates ZIP integrity, a native arm64 executable, build/revision metadata and
every packaged inference/license/font resource against the source snapshot.
Resources are not replaced by this navigation change.

Final result: native release build and IPA packaging passed (exit code 0).
All 117 source fingerprints and 35 bundled resource files passed verification.
The IPA is 240,571,369 bytes, with SHA-256
`11a43dc74d6b73ce298576aec32d340233a83de81bf1d272886645d1f581656b`.
A matching convenience copy is in the original `BrakeCheck_V2` workspace.

The root artifact is `BrakeCheck-N57.ipa`. It is unsigned and uses the existing
signing/sideload workflow. Native compilation and unit tests do not establish
physical-device execution. Existing camera/orientation deprecation and Swift 6
language-mode migration warnings from main_v3 are outside this navigation change.

## Device verification remaining

1. Plan routes and compare alternatives; change driving preferences/departure,
   add/reorder/remove stops and verify the final destination remains correct.
2. Start from a cached origin then obtain fresh GPS; check paused text/speech,
   route matching, manual pause/resume, GPS gaps and return from background.
3. Visit stops, take a wrong turn, stop/restart guidance and confirm completed
   stops do not reappear. Check stale searches and mode changes during requests.
4. Import a complete road region; plan/follow/reroute with connectivity disabled.
   Confirm one-way/turn restrictions, region boundaries and missing/ambiguous
   road errors. Map imagery is separate from route instructions and road data.
5. Check traffic refresh and faster-route acceptance for a multistop journey;
   estimated times must not replace arrival or a user-paused trip state.
6. Verify camera ribbon/calibration remains aligned when trusted and hides when
   uncertain; mini-map/banner yield to critical alerts and selection controls.
7. Inspect small phones, portrait/landscape, larger text, keyboard, Look Around,
   native attribution, map settings and zoom/recenter overrides.

See [NAVIGATION.md](NAVIGATION.md) and the existing
[AR-NAVIGATION.md](AR-NAVIGATION.md) for operation and calibration limitations.
