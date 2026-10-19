# BrakeCheck Premium setup

The app uses StoreKit 2 for a monthly auto-renewable subscription with product ID
`com.brakecheck.prototype.premium.monthly`. Configure that exact ID in the same
App Store Connect app and a subscription group, choose one-month duration, and
set the US price to $9.99/month. Apple sets localized prices in other regions.
The app displays StoreKit's actual localized price when the product loads; it
does not charge using its fallback price label. The current unsigned xtool IPA
cannot be used to verify a live App Store purchase.

The Premium entitlement unlocks approximate route distance and average trip
speed in the trip detail screen and a compact camera directions setting. Basic
trip summaries, route maps, current AR guidance, alerts, and emergency check-ins
remain available without a subscription. Enhanced AR styles and cloud-assisted
models are roadmap ideas and are not part of the current subscription. Purchases are
verified through StoreKit current entitlements; Restore Purchases calls
`AppStore.sync()`. Validate purchases, renewals, cancellation, refund, grace
period, and restore in Apple's Sandbox/TestFlight before distribution. Supply
the production app identifier, subscription metadata, privacy policy, and
terms in App Store Connect before release.
