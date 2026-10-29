# N34 navigation upgrade

N34 adds route alternatives, route preferences, departure planning, ordered
intermediate stops, custom progress/ETA, voice, pause/resume, arrival and
off-route recovery. The planner adds nearby/along-route places, favorites,
recents, sharing, saved routes, road-graph import and coordinate destinations.
The driving map adds traffic/imagery/3D/appearance/heading controls, approach
zoom, route colors and Look Around. Camera guidance adds fixed maneuver cues
and a minimap, yielding to critical alerts and calibration controls.

Offline navigation can follow stored routes without MapKit route objects. New
offline routes and rerouting require an imported legal driving graph. The OSM
adapter handles explicit directed connectivity and supported restrictions,
conservatively omitting unsupported cases. Turn restrictions remain in force
across intermediate stops. Same-road shape points do not repeat voice prompts.

GPS snapping, smoothing and short display estimation remain separate from
collision/speed measurements. Stale/estimated positions do not announce turns,
advance guidance, complete stops or produce arrival. Deferred itinerary changes
must install their updated route before guidance can resume. Asynchronous route,
traffic and graph-import completions reject superseded requests. Stop completion
uses the routed endpoint and measured progress rather than a building's map pin.

## Verification

- Portable Core suite: **392 tests passed, zero failures** using Swift 6.4 on WSL.
- Added navigation tests exercise directed/partial-edge routing, prohibited/only
  turns, toll/highway avoidance, ambiguous carriageways, offset POIs, multistop
  restrictions, saved-route compatibility, OSM legality, merged instructions,
  GPS confidence/heading, bounded estimation, ETA and comparable trip history.
- Native release build and unsigned IPA packaging use
  `xtool dev build --configuration release --ipa` and passed (exit code 0).
- Packaged version is 0.4.0 (build 34), revision N34. ZIP integrity and all
  **18 bundled model/license resource files** passed verification. The 19
  recorded app/Core source files were unchanged during the final build.
- Root IPA: 152,069,921 bytes; SHA-256
  `43c42c8229d14e7863bb9c1b62b010dc12350d25647f4d01b50fbdb3c27e8335`.
- Source fingerprints, native build log, test log and artifact validation are in
  `test-results/navigation-20261019/` at the repository root. Archive verification
  checks ZIP integrity, revision/build metadata and every bundled model resource.
- Existing camera-orientation deprecation warnings remain.

The packaged build is `BrakeCheck-N34.ipa` at the repository root; signing and
installation use the existing xtool/sideload workflow. This validation does not
claim simulator or physical iPhone execution.

## Physical-device checks remaining

1. Plan a route, compare alternatives, toggle toll/highway preferences and choose
   a later departure. Add/reorder/remove stops and confirm the final destination.
2. Start guidance, pause/resume, take a wrong turn and rejoin; confirm remaining
   stops survive rerouting, and arrival occurs only at the true destination.
3. Test GPS loss and recovery: orange estimated position expires; progress and
   speech pause. End guidance during the gap and confirm the estimate clears.
4. Change stops while paused, then end guidance; confirm the preview replans and
   can start again. Test failed-network retry and rapid destination changes.
5. Import complete regional roads; plan and reroute offline using coordinates
   or saved places. Test boundary/disconnected/unsupported roads explicitly.
6. Inspect portrait/landscape, keyboard, larger text, camera HUD, critical-alert
   priority, zoom overrides/recenter, Look Around and map attribution.
7. Verify voice yields to safety audio, background/foreground pauses, traffic
   refresh and faster-route acceptance during a real drive with a passenger.

**World-aligned AR is outstanding.** The included camera arrows are fixed
onscreen. Precise camera/IMU pose, intrinsics, mount/road calibration and capture
ownership integration require a separate implementation and device validation.
Apple map tiles/imagery and live traffic are not bundled with saved road graphs.
See [NAVIGATION.md](NAVIGATION.md) for feature coverage and offline data setup.
