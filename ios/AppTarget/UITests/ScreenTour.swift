import XCTest

/// Walks the main screens in the iOS Simulator and attaches a screenshot of each, so CI can show
/// the UI to people without a Mac (.github/workflows/ios-screens.yml exports them as PNGs).
/// The Simulator has no camera, so the camera page shows its placeholder; simctl supplies the GPS fix.
/// Every step tolerates the one before it failing, so one missing button never costs the other shots.
final class ScreenTour: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = true
    }

    @MainActor
    func testTour() throws {
        XCUIDevice.shared.orientation = .portrait
        let app = XCUIApplication()
        app.launch()
        pause(3)
        snap("01-home")

        openPage(app, "Navigate")
        allowSystemAlerts()
        pause(4)
        dismissAppAlert(app, shot: "02a-navigate-alert")
        snap("02-navigate")

        let search = element(app.textFields, ["nav.search", "Destination search"])
        if search.waitForExistence(timeout: 5) {
            search.tap()
            search.typeText("Piedmont Park")
            pause(4)
            snap("03-navigate-typing")
            search.typeText("\n")
            pause(5)
            snap("04-navigate-results")
        }
        let start = element(app.buttons, ["nav.start", "Start navigation"])
        for _ in 0..<2 where !start.exists {
            let place = app.buttons.matching(NSPredicate(format: "label CONTAINS[c] %@", "Piedmont Park")).firstMatch
            if place.waitForExistence(timeout: 8) { place.tap() }
            _ = start.waitForExistence(timeout: 20)
        }
        if start.exists {
            pause(2)
            snap("05-route-preview")
            start.tap()
            allowSystemAlerts()
            pause(6)
            dismissAppAlert(app, shot: "06a-navigating-alert")
            snap("06-navigating")
            let details = element(app.buttons, ["nav.details", "Expand navigation panel", "Collapse navigation panel"])
            if details.exists && details.isHittable {
                details.tap()
                pause(2)
                snap("06b-navigating-details")
                details.tap()
                pause(1)
            }
        }

        openPage(app, "Camera")
        pause(3)
        snap("07-camera")

        XCUIDevice.shared.orientation = .landscapeLeft
        pause(4)
        snap("08-camera-landscape")
        openPage(app, "Navigate") { app.swipeLeft() }
        pause(4)
        snap("09-navigate-landscape")
        openPage(app, "Home") { app.swipeRight(); app.swipeRight() }
        pause(3)
        snap("10-home-landscape")

        XCUIDevice.shared.orientation = .portrait
        pause(3)
        openPage(app, "Home")
        pause(2)
        snap("11-home-trip")
        let settings = element(app.buttons, ["home.settings", "Settings"])
        if settings.exists && settings.isHittable {
            settings.tap()
            pause(2)
            snap("12-settings")
        }
    }

    // MARK: - Helpers

    /// Matches any key as an accessibility identifier or label. The query runs again on every
    /// use, so it finds elements that appear later and works before and after identifiers exist.
    @MainActor
    private func element(_ query: XCUIElementQuery, _ keys: [String]) -> XCUIElement {
        query.matching(NSPredicate(format: "identifier IN %@ OR label IN %@", keys, keys)).firstMatch
    }

    /// Taps the page switcher; `fallback` runs when this layout has no visible switcher.
    @MainActor
    private func openPage(_ app: XCUIApplication, _ title: String, fallback: (() -> Void)? = nil) {
        let button = element(app.buttons, ["page.\(title.lowercased())", title])
        if button.exists && button.isHittable { button.tap() } else { fallback?() }
        pause(1)
    }

    @MainActor
    private func allowSystemAlerts() {
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        for _ in 0..<3 {
            let alert = springboard.alerts.firstMatch
            guard alert.waitForExistence(timeout: 2) else { return }
            for label in ["Allow While Using App", "Allow", "OK"] where alert.buttons[label].exists {
                alert.buttons[label].tap()
                break
            }
        }
    }

    /// An in-app alert would hide the screen behind it: photograph it, then close it.
    @MainActor
    private func dismissAppAlert(_ app: XCUIApplication, shot: String) {
        let alert = app.alerts.firstMatch
        guard alert.exists else { return }
        snap(shot)
        alert.buttons.firstMatch.tap()
        pause(1)
    }

    @MainActor
    private func snap(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func pause(_ seconds: TimeInterval) {
        Thread.sleep(forTimeInterval: seconds)
    }
}
