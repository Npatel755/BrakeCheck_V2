# N60: Claude polish and navigation integration

Combines `claude/v3-polish` at `c1b0e43` with the local navigation and Premium
alert changes at `621beb5`, on `main_v3`.

The app uses Claude's BrakeSense display name, page tabs and landscape rail,
stacked Navigate layout, trip bar, typography, first-use notice, legal links,
and App Store pricing and review improvements. Its removal of committed backup
folders is included.

The Navigate screen retains the enhanced shared map and navigation service:
route alternatives, stops, route preferences and departure scheduling, saved
places, saved offline routes and imported road graphs, pause/resume, traffic
updates and history, nearby searches, Look Around, map options, and sharing.
Saved places and offline routes open from the planning toolbar; additional route
controls are under “Route choices, stops and options” and “Trip options”.

Traffic Start Alert, Lane Departure, Cut-In Prediction, and Green Light
Notification remain optional Premium previews, all off by default. Their runtime
entitlement and sensor checks remain in place. Existing collision warnings and
emergency check-ins remain free. See `N59-DRIVING-ALERTS.md` for preview limits.

The system review prompt uses `AppStore.requestReview(in:)`, which works with
both the cross-compiled iPhone SDK and Xcode. The incoming Simulator and Xcode
workflows also run when `main_v3` is pushed.

Validation: 471 BrakeCheckCore tests and 18 messaging tests passed; TypeScript
type checking and the arm64 iOS release compilation passed. Physical-device
driving and StoreKit transaction testing have not been performed for N60.
