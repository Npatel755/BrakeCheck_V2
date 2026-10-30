# N73 trip saving and route recovery

Ending a trip now queues its history and route persistence on `TripArchiveStore`.
The UI waits for that save, the video finalization and the speed-log flush before
opening the summary. A save journal is written before either archive is changed;
startup and Retry trip save can complete an interrupted save. Existing malformed
history, journals and route files are preserved and reported. A completed trip's
identity cannot replace a different route or different totals during recovery.

Home and Trip history expose save failures and a retry action. A failed save
retains its in-memory payload as well as any successfully written journal.
If writing the journal itself fails, automatic recovery after process termination
is impossible; the visible retry must succeed while the app still has that data.
No current-trip crash recovery is claimed before End trip is tapped.

Trip detail loads route files asynchronously, checks their contents before
creating map geometry and reports read errors with a retry action. New routes
retain fractional timestamps; the decoder continues to accept legacy timestamps.
The route collector rejects invalid measurements and does not place events at
future or stale GPS fixes. Long GPS gaps and implausible jumps split map paths
and do not count toward route-distance estimates. Distances use spherical
geometry, including short crossings of the date line.

Trip clips use the recorded trip identity, including across phone-clock changes.
Legacy incidents without a trip ID continue to use the recorded time window.
Trip duration uses monotonic elapsed time so changing the phone clock cannot
produce a negative duration. Starting navigation during a trip updates the saved
destination. Deleting routes is serialized with saves and cannot leave pending
journals that later recreate the deleted GPS data.

The standard app remains on the existing trained UFLDv2, vehicle, road and
viewpoint model assets. No confidence thresholds or collision policies changed.

## Validation scope

- 762 portable Core tests passed in debug, including 18 new route/archive tests.
- Fault tests use disposable real files to interrupt writes, corrupt history,
  rename a journal to a different ID, preserve conflicts, retry after repair and
  save concurrently without losing entries.
- Route tests cover invalid/backward observations, fresh event locations,
  duplicate spacing, GPS gaps, implausible jumps, date-line distances, legacy
  timestamps and clip identity/time-window association.
- Native debug app compilation passed after the initial integration.
- Final release Core tests, native arm64 compilation, IPA packaging and asset
  checks are recorded under `test-results/functionality-n73-20261029/`.

The native compile checks SDK integration; it does not demonstrate device disk
exhaustion, iOS termination/lock behavior, map rendering or spoken alerts on a
phone. Those remain device-validation tasks. The N72 IPA is preserved.
