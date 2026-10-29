# N38 replay and navigation status

The supplied v18.mp4 has the same SHA-256 as video17.mp4 and visibly records
build N36. It cannot verify the N37 camera preview or parked setup changes.
In that recording the camera preview turns black while detector overlays
continue; the setup preview was a separate view in N36. The recorded messages
say "Waiting for GPS" and "No reading"; a literal "Navigation paused" is not
visible in the sampled recording.

The camera navigation HUD now shows NavigationService's specific pause reason,
including stale GPS, location permission, route-position uncertainty or
backgrounding, rather than a generic "Navigation paused" label. Freshness
and route safety behavior are unchanged.

Imported movies now select "Video only - no speed" so an MP4 can start without
an unrelated BrakeCheck speed log or invented speed. The app explains how to
transfer the file into iPhone Files, which overlays replay can test, and why
camera route AR needs synchronized location, route and camera data. Choosing an
assumed speed or importing a speed log remains available for speed-related
replay testing. Replay errors identify unsupported/unreadable video files.

Release build 38 validation: native release build succeeded; both IPA model
verifiers passed, including package hashes and archive CRC. The 413 Core
regressions passed in N37; no Core sources changed in N38. An iPhone replay of the user's specific MP4 is still
needed to verify device decoding and preview behavior.
