# N21 — Smooth navigation presentation

Fixed repeated zoom caused by feeding MKMapCamera.altitude back into a fromDistance parameter at a 45-degree pitch. Following uses centerCoordinateDistance and a stable 700 m target; manual pinch sets a new stable distance. Zoom, pitch, heading and center approach their targets continuously on a 30 fps display link. Noisy course is ignored below 2 m/s or when course accuracy is invalid or exceeds 35 degrees. Heading interpolation takes the shortest turn across north.

Route overview fitting is coalesced until map bounds have settled for 250 ms, then animated. This avoids fitting once to old bounds and again during the search/trip-panel animation. The display link pauses outside the Navigation page and is invalidated when its UIView is dismantled; its proxy holds the coordinator weakly.

NavigationMapSmoother is presentation-only. It applies an accuracy-aware stationary deadband, smooths accepted moving fixes and interpolates the location dot without extrapolation. Implausible isolated jumps expand the uncertainty circle instead of moving the dot. Cached fixes remain labeled and gray. The blue/gray accuracy circle includes reported horizontal accuracy plus the rendering offset, so smoothing does not imply improved GPS precision. The map dot uses a circle with a white border rather than a location pin.

Actual Core Location measurements still feed route progress, speed and collision estimates through their existing freshness checks. Visual smoothing neither changes GPS measurements nor guarantees correction of persistent GPS errors.

Validation: all 338 Core tests passed, including nine new tests for deadband, movement, outliers, uncertainty, invalid/stale inputs, cached fixes, approximate fixes, ordering and dateline handling. Native release compilation and IPA integrity, metadata, executable identity and eleven model-resource checks. Device checks still required: route selection/preview zoom, start guidance, slow traffic, course crossing north, GPS loss/recovery, pinch/recenter, keyboard and panel resizing, rotation, and page switching.
