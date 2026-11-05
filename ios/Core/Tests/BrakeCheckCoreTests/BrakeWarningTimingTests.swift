import XCTest
@testable import BrakeCheckCore

final class BrakeWarningTimingTests: XCTestCase {
    func testCriticalAlwaysWarns() {
        for timing in BrakeWarningTiming.allCases {
            XCTAssertTrue(timing.warrantsBrakeWarning(level: .critical, timeToCollision: nil))
            XCTAssertTrue(timing.warrantsBrakeWarning(level: .critical, timeToCollision: 1.2))
        }
    }

    func testStandardIgnoresHighRisk() {
        XCTAssertFalse(BrakeWarningTiming.standard.warrantsBrakeWarning(level: .high, timeToCollision: 2.0))
        XCTAssertFalse(BrakeWarningTiming.standard.warrantsBrakeWarning(level: .elevated, timeToCollision: 1.0))
    }

    func testEarlierPromotesCloseHighRiskOnly() {
        let earlier = BrakeWarningTiming.earlier
        XCTAssertTrue(earlier.warrantsBrakeWarning(level: .high, timeToCollision: 2.4))
        XCTAssertFalse(earlier.warrantsBrakeWarning(level: .high, timeToCollision: 2.6))
        XCTAssertFalse(earlier.warrantsBrakeWarning(level: .high, timeToCollision: nil))
        XCTAssertFalse(earlier.warrantsBrakeWarning(level: .high, timeToCollision: .infinity))
        XCTAssertFalse(earlier.warrantsBrakeWarning(level: .elevated, timeToCollision: 1.0))
        XCTAssertFalse(earlier.warrantsBrakeWarning(level: .low, timeToCollision: 0.5))
    }

    func testDefaultsRoundTrip() {
        XCTAssertEqual(BrakeWarningTiming(rawValue: "earlier"), .earlier)
        XCTAssertNil(BrakeWarningTiming(rawValue: "later"))
        XCTAssertEqual(BrakeWarningTiming.allCases.map(\.title), ["Standard", "Earlier"])
    }
}
