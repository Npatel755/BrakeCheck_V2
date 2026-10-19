# N32 navigation recovery and trip history

Navigation adds bounded retries for failed route requests: 5, 10, 20, 40 and
60 seconds, with the delay reset after recovery or a manual retry. Automatic
retries require a fresh GPS fix and foreground/live operation. Returning to the
original route cancels pending recovery. Existing off-route confirmation and
request identity checks remain. GPS interruptions retain the last position,
directions and metrics without stale spoken turns or a Navigation paused banner.
The maneuver panel shows turn arrows, a larger current instruction and the next
instruction, with explicit rerouting progress. Existing selected-destination
loading feedback remains immediate.

Trip history now has overview cards and a dedicated summary page with measured
GPS maximum speed, camera-estimated events and links to saved incident footage
whose timestamps fall within the trip. JSON export remains; a spreadsheet CSV
export adds explicit mph units, estimated-event headers and empty cells for
unavailable speed. No distance, average speed or driving score is fabricated.

Settings → Recordings & storage shows video usage and limits of 1/2/5/10 GB,
with no app limit by default. Reaching the limit pauses recording while keeping
saved footage and allowing camera detection to continue. The existing 500 MB
free-space reserve remains. Usage scans run on a utility queue and deduplicate
hard-linked clips. Full scans are limited to once every 30 seconds during
recording, with explicit refreshes after settings changes and idle cleanup.
Checks are periodic; the cap is a soft recording budget and
can be temporarily exceeded by in-flight writes or a pending scan. Removing
footage/increasing the limit frees space; a new trip resumes recording.

Saved-incident automatic deletion defaults to off. Selecting 7/30/90 days shows
a destructive confirmation before saving the setting. Cleanup waits for idle
recording, no in-flight writers, and no collecting incident windows. Active,
recent and collecting incidents are protected. Trip metrics and exported copies
are retained. Cleanup runs locally on refresh, a new trip, or trip finalization.
No videos were deleted by this development task.

Core coverage checks retry deadlines/backoff/reset, invalid timestamps,
opt-in retention, active/collecting/recent/future protection, supported limits,
CSV unknown-versus-zero speeds and maneuver phrases versus road names.
Validation passed: all 367 Core tests, native release compilation and IPA CRC,
revision, executable identity, required feature strings and all 18 resources.
Detector resource bytes match N31. IPA SHA-256:
`410ef669830e2b37d609eb4b83368f6506fb9fae165a63e16a52853f18f6f94d`.
Physical device behavior remains unverified. Phone checks: route loss
and recovery, GPS loss with no advancement, offline reroute retry, voice timing,
trip details/incident links, JSON/CSV sharing, cleanup cancel/confirm, active
footage protection, storage-limit pause and recording restart after freeing space.

Detector models remain identical to N31. No model replacement is part of N32.
