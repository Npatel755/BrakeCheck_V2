import XCTest
@testable import BrakeCheckCore

final class NavigationLocationSelectionTests: XCTestCase {
    private func sample(_ time: Double, _ accuracy: Double, valid: Bool = true) -> NavigationLocationSample {
        NavigationLocationSample(timestamp: time, horizontalAccuracy: accuracy, coordinateValid: valid)
    }

    func testBadNewestFixDoesNotHideFreshAccurateFixInSameBatch() {
        let fixes = [sample(9.4, 5), sample(9.9, 80), sample(9.7, 4, valid: false)]
        XCTAssertEqual(NavigationLocationSelection.newestFreshIndex(fixes, now: 10,
            eligibleAfter: 9, lastAccepted: nil), 0)
    }

    func testRestartAndOutOfOrderUpdatesCannotReusePriorGPS() {
        let fixes = [sample(9.4, 4), sample(9.6, 4), sample(9.8, 5)]
        XCTAssertEqual(NavigationLocationSelection.newestFreshIndex(fixes, now: 10,
            eligibleAfter: 9.5, lastAccepted: 9.6), 2)
        XCTAssertNil(NavigationLocationSelection.newestFreshIndex(fixes, now: 10,
            eligibleAfter: 9.9, lastAccepted: 9.6))
    }

    func testRejectsFutureStaleAndNonFiniteAccuracy() {
        let fixes = [sample(4.9, 5), sample(10.1, 5), sample(9.8, .nan), sample(9.9, 65.1)]
        XCTAssertNil(NavigationLocationSelection.newestFreshIndex(fixes, now: 10,
            eligibleAfter: 0, lastAccepted: nil))
        XCTAssertNil(NavigationLocationSelection.newestFreshIndex([sample(9.9, 5)], now: .nan,
            eligibleAfter: 0, lastAccepted: nil))
    }

    func testSameTimestampPrefersBetterAccuracy() {
        XCTAssertEqual(NavigationLocationSelection.newestFreshIndex([
            sample(9.9, 30), sample(9.9, 5)], now: 10, eligibleAfter: 9,
            lastAccepted: nil), 1)
    }

    func testDelayedModerateAccuracyFixCanKeepMapPosition() {
        XCTAssertEqual(NavigationLocationSelection.newestFreshIndex([
            sample(6.5, 50), sample(4.8, 5)], now: 10,
            eligibleAfter: 0, lastAccepted: nil), 0)
    }
}
