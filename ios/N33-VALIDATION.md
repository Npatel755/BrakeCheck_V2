# N33 map camera continuity during GPS gaps

All three active-navigation map layouts keep follow mode enabled while a last
known position is available. GPS freshness and paused measurement state no
longer interrupt camera zoom/interpolation or switch to route-overview behavior.
The existing mapLocation getter supplies the current or cached fix; cached
positions remain marked as last known. Manual map pan/pinch overrides remain.
Offscreen/background display links still stop to preserve battery. Route progress
and spoken turns retain their fresh-fix requirements; no stale position is
extrapolated into movement or route advancement.

Validation: native release compilation and IPA verification required. Physical
phone check: start navigation, interrupt GPS during the approach zoom, verify
zoom completes centered on the last known position, then restore GPS; repeat
in portrait/landscape, after manual panning/recentering, and after backgrounding.
