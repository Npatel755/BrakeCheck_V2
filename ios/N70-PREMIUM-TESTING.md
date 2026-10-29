# N70 local Premium testing

Home > Premium now exposes **Enable Premium for testing** in this sideload test
configuration. It starts off, persists the explicit opt-in on the device, and
unlocks the same feature gates as Premium. Optional alert switches remain
individually controlled and start off; the user still enables the ones to test.

The flag `BRAKECHECK_PREMIUM_TESTING` is injected only into the staged N70 app
target in `test-results/n70-premium-testing-20261023/app/Package.swift`. The normal
`ios/Package.swift` contains no such define. Normal builds ignore both the
persisted preference and the test setter, and expose no local-access control.
Changing only an Info.plist entry cannot enable access.

Test builds skip product loading and reject purchase/restore entry points, so
the local switch never creates a transaction. Real verified entitlements remain
separate from local access. StoreKit refresh cannot erase an enabled local
override; switching it off does not remove an existing verified entitlement.
The Premium page labels active local access as **Premium test access active**.

`PremiumStore` behavior was exercised in normal/testing compilations of the
actual service source, using portable StoreKit API stubs. Checks cover opt-in,
persistence on a new instance, entitlement refresh, disable, verified-access
preservation/removal, production ignoring a persisted test preference, and zero
commerce calls in the local-test build. This is not Apple StoreKit runtime or
physical-phone UI validation. Logs and exact tested source are in the N70 test
folder. The unchanged Core source retains its 676-test release validation.

N70 keeps N69's lane/lead behavior and model weights. The sustained AR-tracking
prototype remains a PC test path and is not integrated into the live camera.
Premium assist event detection still requires its ordinary live-camera/GPS
conditions; unlocking access does not fabricate events during replay.
