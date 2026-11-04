# N74 practice-drive ownership and recording resume

The first-run practice offer, simulated drive and Premium transition now share
one cancellation-aware flow. Delayed actions carry request identities, use
monotonic deadlines and recheck the current UI and driving state before running.
The offer is marked as seen only when it is presented. Backgrounding, starting
a trip or replay, opening another screen, using the pre-drive checklist, or a
safety check-in cancels an incompatible pending action.

The practice flow owns one explicit demo session. An old completion cannot stop
a newer demo started from Settings. The demonstrated duration now matches its
20-second description; at least 12 foreground seconds must elapse before a
Premium offer is eligible. Interrupted or early-ended practice does not count
background time as viewing. Check-ins dismiss optional onboarding screens before
presenting their controls.

Home and Recordings settings expose Resume recording when a foreground live
trip's camera is active and its recorder is paused. This retries recording after
freeing storage, increasing the app limit or a recording failure without ending
the trip or restarting detection. Storage is measured again after any cleanup,
and the current app limit and free-space floor remain in force. The resume
request is invalidated by recorder start, pause or stop, so an old storage result
cannot restart recording after backgrounding or End trip.

Storage measurement counts real movie files, deduplicates hard links using their
filesystem identity and rejects incomplete inventories. A paused recorder cannot
resume on an unknown measurement. When an app storage limit is configured, a
failed measurement pauses recording while preserving original clips. The camera
and detection pipeline retain their existing behavior.

## Verification

- 789 Core tests passed in debug, including 27 new regressions: 15 practice-flow
  cases, four resume-ownership cases and eight disposable-file inventory cases.
- The practice tests cover early wakeups, stale completions, foreground loss,
  explicit Start/Skip, safety priority, replacement demos, subscription changes,
  early manual completion and leaving Home before a deferred paywall.
- Filesystem cases cover missing/invalid roots, nested movies, hard links,
  overlapping roots, nonmovie files and symbolic movie links.
- The initial native debug integration compiled successfully. Final release
  tests, native build, IPA verification and source fingerprints are saved under
  `test-results/functionality-n74-20261029/`.

The portable tests prove the transition and inventory policies. They do not
execute SwiftUI modal animations, AVFoundation encoding or iOS storage/lock
transitions. Validate the offer and checklist race, interruption during practice,
check-in presentation, and Resume recording on a physical phone. No provider
messages, purchases, model changes or deployment were performed. Earlier IPAs
remain preserved.
