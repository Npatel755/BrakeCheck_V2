# BrakeCheck navigation

Open **Navigation**, choose an address/place, select a route, then start guidance.
Route Options changes toll/highway preferences and departure time. Stops adds,
removes, and reorders places without replacing the final destination. Nearby
categories include fuel, charging, parking, food, and coffee; Search along my
route filters results to a corridor along the remaining trip.

The driving panel shows the next maneuver, upcoming instruction, distance to
turn, remaining distance/time, estimated arrival, progress, GPS accuracy and
route-position confidence. Guidance supports voice, pause/resume, automatic
arrival, rejoining and wrong-turn rerouting. The map supports traffic, satellite,
3D buildings, heading up, manual panning/recentering, route colors, appearance,
and approach zoom. Native Look Around is available where Apple supplies imagery.
Favorites, recent destinations, route instructions and completed trip history are
stored on this phone. Sharing opens the iOS share sheet with destination and ETA.

## Following routes offline

Planned routes are saved automatically; **Save offline** also confirms the save.
Open the Offline tab and select a saved route. Its polyline, maneuver instructions,
progress and local ETA work without a network. Apple map tiles, search, imagery,
traffic and Look Around require their own network/cache availability. Downloaded
maps in Apple's Maps app are not treated as a downloadable MapKit road graph.

New offline routes, road matching away from a planned route, and offline
rerouting need a road graph covering the journey. Import one using **Import
offline roads (JSON)** and enable **Use offline road graph**. Saved destinations
and **Route to coordinates** work without online place search. When online
planning fails, a loaded graph is also tried automatically. The router respects
directed roads, turn restrictions and driving preferences; unsupported or
ambiguous connectivity fails instead of drawing a straight connector.

The importer accepts either the validated `OfflineRoadGraph` schema in
BrakeCheckCore or a complete regional Overpass JSON export containing road ways,
nodes and their restriction relations. Geometry-only JSON, XML and PBF are not
supported. Incomplete exports cannot establish that a missing restriction is
absent in reality. The importer conservatively omits private/ambiguous access,
conditional directions/access and unsupported restrictions. Import notices and
OpenStreetMap attribution appear in the Offline tab. Imports are limited to
64 MB and bounded graph sizes. An imported graph is road data, not map imagery.

For a small regional export, run this query against your own/licensed Overpass
provider, replacing the four bounding-box values:

```overpass
[out:json][timeout:90];
way["highway"]["area"!="yes"](SOUTH,WEST,NORTH,EAST)->.roads;
rel(bw.roads)["type"="restriction"]->.rules;
(.roads;.rules;way(r.rules);)->.all;
(.all;>;);
out body;
```

The provider must include connected ways and all applicable restrictions.
The public Overpass service has a shared-use policy; a distributed commercial
app should use its own or a paid provider. See the [Overpass service guidance](https://wiki.openstreetmap.org/wiki/Overpass_API)
and [OpenStreetMap attribution/license](https://www.openstreetmap.org/copyright).
BrakeCheck makes no automatic public Overpass requests.

## Position, traffic, and camera behavior

Route tracking consumes fresh measured GPS. Heading, plausible progress and
confidence reduce incorrect matches at intersections. Map smoothing is display
only. A short GPS gap can show an orange estimated marker for at most four
seconds/80 meters with increasing uncertainty; it never advances maneuvers,
announces a turn, marks a stop/arrival or feeds the collision/speed pipeline.
GPS loss pauses measured guidance. A manual pause remains paused until Resume.

Apple supplies initial traffic-aware route estimates and alternatives. During
the final leg, a background check at most every three minutes refreshes matching
route ETA, reports a significant delay, or offers a potentially faster route.
Accepting the offer calculates from the latest GPS fix. Intermediate-leg times
remain the original provider estimate, updated locally from measured progress.
Stop-and-go detection uses measured movement and is an observation, not a traffic
feed. Comparable completed trips accumulate local delay history; route changes
and reroutes are excluded from comparable-trip estimation. Offline historical
estimates require at least three comparable trips; they are not live traffic.

Navigation runs in the foreground. Backgrounding pauses it; returning requires
fresh GPS before measured guidance and speech resume. Voice yields to BrakeCheck
safety alerts. Ending guidance clears any extrapolated map position.

The Camera page has a maneuver arrow/banner and compact route map. These cues
are fixed on screen. **World-aligned AR arrows are not implemented.** Current
capture geometry and vehicle-width calibration do not provide camera pose,
ground-plane depth, synchronized intrinsics/attitude or precise geographic
localization. Starting a competing ARKit camera session would interrupt the
current collision-detection capture pipeline. That feature requires a separate
capture/calibration change and physical-device validation.

## Requested feature coverage

| Requested features | Implementation |
| --- | --- |
| 1–15: rerouting, alternatives, route comparison/preferences, maneuvers, ETA, voice, departure planning, stops/via, arrival, pause/resume | Service orchestration, Apple Directions, persisted route model and custom progress/ETA |
| 16–25: planned-route snap, smoothing, GPS gaps, off/rejoin detection, heading, confidence, prediction, intersection safeguards, recenter | Core progress/presentation/smoothing; visible GPS/confidence state and map controls |
| 26–35: traffic, ETA refresh, better routes, delay notices, alternative times, advisories, departure routing, history, stop-and-go | Apple data where supplied; throttled final-leg checks and local history/movement logic |
| 36–45: autocomplete, fuel/EV/parking/food, route search, add stop, favorites/recents/share | Native search plus route corridor filtering, local storage and share sheet |
| 46–55: imagery, 3D, camera/zoom, night/color/heading, Look Around, maneuver banners, camera minimap | Native MapKit with custom presentation and camera guidance |
| Offline route following/generation/rerouting and road matching | Saved instructions; new routing/matching requires imported complete road data |
| Road-aligned AR navigation | Remaining: calibrated camera/pose integration and device testing |

No training models, dataset decisions or collision metrics are changed by these
navigation features.
