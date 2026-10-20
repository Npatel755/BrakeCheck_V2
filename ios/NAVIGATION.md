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
guidance, a background check at most every three minutes recalculates all remaining
legs in order, refreshes matching route ETA, reports a significant delay, or
offers a potentially faster route.
Accepting the offer calculates from the latest GPS fix. The same route planner preserves intermediate stops and checks that the routed
leg endpoints connect. Local measured progress updates ETA between provider requests.
Stop-and-go detection uses measured movement and is an observation, not a traffic
feed. Comparable completed trips accumulate local delay history; route changes
and reroutes are excluded from comparable-trip estimation. Offline historical
estimates require at least three comparable trips; they are not live traffic.

Navigation runs in the foreground. Backgrounding pauses it; returning requires
fresh GPS before measured guidance and speech resume. Voice yields to BrakeCheck
safety alerts. Ending guidance clears any extrapolated map position.

The Camera page retains main_v3's maneuver banner, calibrated road ribbon,
chevrons and mount setup. N58 removes the camera mini-map and its settings toggle,
and removes the GPS accuracy/confidence caption beneath navigation directions.

The existing road-alignment confidence gates remain active: fresh measured GPS,
reliable course, unambiguous route matching, a verified fixed mount, and fresh
road/lane observations are required for aligned ribbon rendering. Uncertain
alignment falls back to direction-only guidance. Display-only snapped/predicted
map positions never enter camera projection or collision measurements. Offline
routes participate in the same confidence gates and invalidate cached projections
when their geometry changes. This preserves the branch's calibrated flat-road
projection; it does not claim ARKit geographic localization or physical-device
validation. See [AR-NAVIGATION.md](AR-NAVIGATION.md) for calibration and limitations.

## Requested feature coverage

| Requested features | Implementation |
| --- | --- |
| 1–15: rerouting, alternatives, route comparison/preferences, maneuvers, ETA, voice, departure planning, stops/via, arrival, pause/resume | Service orchestration, Apple Directions, persisted route model and custom progress/ETA |
| 16–25: planned-route snap, smoothing, GPS gaps, off/rejoin detection, heading, confidence, prediction, intersection safeguards, recenter | Core progress/presentation/smoothing; visible GPS/confidence state and map controls |
| 26–35: traffic, ETA refresh, better routes, delay notices, alternative times, advisories, departure routing, history, stop-and-go | Apple data where supplied; throttled remaining-trip checks and local history/movement logic |
| 36–45: autocomplete, fuel/EV/parking/food, route search, add stop, favorites/recents/share | Native search plus route corridor filtering, local storage and share sheet |
| Imagery, 3D, camera/zoom, night/color/heading, Look Around and maneuver banners | Native MapKit with custom presentation and camera guidance; camera mini-map removed in N58 |
| Offline route following/generation/rerouting and road matching | Saved instructions; new routing/matching requires imported complete road data |
| Road-aligned camera navigation | Existing main_v3 calibrated ribbon/chevrons and confidence gates retained; device verification remains required |

The navigation change does not alter bundled inference assets, dataset decisions,
or collision/speed calculations.
