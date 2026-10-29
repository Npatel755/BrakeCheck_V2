# Branch integration — 29 October 2026

Reviewed every local branch and every fetched `origin` branch. Integrated the
compatible improvements into `navigation_included`, preserving the existing
local backend, recording, replay and navigation work. Other working checkouts
were read and snapshotted without modifying their files or indexes.

## Sources and decisions

| Branches | Incorporated improvements |
| --- | --- |
| `main`, `origin/main`, `origin/main_v2` | Shared history and AR capabilities, carried through the newer app branches. |
| `main_v3`, `origin/main_v3` | Combined navigation, app branding, trip routes, checklist, native Xcode project, screenshot/build CI, dataset utilities, driving alerts and Premium foundation. |
| `origin/claude/nav-banner`, `origin/claude/productize` | Camera direction banner, Red Hat typography and product UI, as retained and refined in the newer branches. |
| `origin/claude/v1` | TestFlight workflow and its checkout-before-signing fix. Its earlier Plus subscription and screen variants are superseded by the newer Premium/paywall and app UI. |
| `origin/claude/v3-polish` | Navigation layout, page switching, trip-bar fit and App Review polish through `main_v3`. |
| `origin/claude/xcode-fix` | Split lane-assist arithmetic to avoid Xcode type-checking failures. |
| `origin/claude/camera-light` | Clear camera image in light mode; readable plates for floating metrics. Applied to the extracted `CameraPage`. |
| `origin/claude/paywall` | Three StoreKit plans, pinned purchase button, trial messaging, practice drive with the full warning, seven-day free history and road-test build workflow. |
| `origin/claude/website` | Complete static website, real-drive assets, current route screenshot, press kit, support page and draft privacy/terms pages. No publishing performed. |
| `origin/commercial-comma-intel` | Pinned original and converted candidate models, provenance/license files, decoder/tests and evaluation/export tools. The older test app's model runtime is superseded by the newer trained UFLDv2 runtime. Candidates remain available separately for evaluation. |
| `perf/n61-smoothness` | Indexed route/AR geometry, isolated live observations, bounded recorder queue, vision cadence, map rendering and performance diagnostics. |
| Uncommitted N62–N71 work in the performance checkout | Trained UFLDv2 model/provenance, lane display and temporal smoothing, lead-lane evidence, remaining-route geometry, map smoothing, queued speed logging, alert readiness/visibility and guarded local Premium testing. |

## Integration fixes

- Kept the latest multi-plan paywall while adding the N71 alert-status UI and
  compile-time guarded local testing access. Ordinary builds retain verified
  StoreKit entitlements; ROADTEST retains its explicit test-build behavior.
- Combined camera recovery/watchdogs with monotonic converted capture times,
  geometry readiness, inference cadence and recorder preparation before capture.
- Kept capture-time recording metadata, incident/trip identity, recovery exports
  and the bounded recorder queue, including recovery callbacks across that queue.
- The app uses `AppSpeedLogWriter` with the queued Core writer. Portable log
  parsing and the existing Core writer API remain available for replay/tools.
- Kept cancellable asynchronous replay imports and speed-log matching, the
  navigation voice ownership/retry fixes, and the optional camera route overview.
- Preserved backend crash-state recovery while requiring both the current
  deadline and timer identity before a timeout can resolve a check-in. Added a
  regression for replacement timers with identical deadlines. Delivery tests
  wait for the asynchronous worker's final state.
- Added non-destructive candidate-model staging and byte verification. The
  default trained model bundle is not replaced by candidate staging.
- Combined deployment exclusions for durable crash state, pairing/contact data,
  Photon state and local artifacts. The staging test checks original files are
  preserved and these files never enter the upload copy.
- Enabled build/screenshot/road-test CI on `navigation_included`. Kept source
  line endings portable and included packaged model weights in source inventory.

## Validation

- Swift Core: **744 tests passed**, zero failures.
- Backend: **134 offline tests passed**.
- Candidate staging: **3 offline tests passed**.
- Dataset utilities: **16 offline tests passed** using disposable fixtures.
- Photon agent: **18 tests passed**, TypeScript type checking passed.
- Cloud supervisor: **4 tests passed**.
- Deployment staging: **1 POSIX test passed**, using a fake CLI.
- Native arm64 iOS debug compilation and unsigned app assembly passed with
  `taskset -c 0,1 xtool dev build --configuration debug` in WSL.
- All default model weight/provenance hashes passed, including the trained
  UFLDv2 checkpoint. Separate candidate package hashes and real staging passed.
- Website JavaScript syntax passed; **110 local links/assets** were checked
  with no missing targets. Info.plist and the web manifest parsed successfully.

Logs are under `test-results/branch-integration-20261029/`. No training,
deployment, provider messages, purchases, signing or phone installation occurred.
Native compilation does not validate physical camera/GPS behavior, lane accuracy,
StoreKit transactions, warning audio or sustained device performance. Existing
camera API deprecation and framework compatibility warnings remain.

## Recovery and Git state

The upstream branch union is retained in commit `c1a0e21`. The complete resolved
result, including both sets of pre-existing local work, is also retained on
`integration/all-improvements-20261029-185405`.

The active branch retains the integrated local work as uncommitted changes for
review. No remote branch was pushed. Safety snapshots:

- Original working-folder source state:
  `safety/branch-integration-20261029-185405` (`29da71f`).
- Original N62–N71 performance-checkout source state:
  `safety/performance-n71-20261029` (`0e4ae24`).

Existing training/review data, local tools, reports, credentials and unrelated
untracked files were preserved. The performance checkout remains untouched.
