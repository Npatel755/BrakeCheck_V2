import XCTest
@testable import BrakeCheckCore

final class LaneDisplayContinuityTests: XCTestCase {
    private func lane(_ time: Double, shift: Double = 0) -> LanePath {
        LanePath(bands: (0..<6).map { LaneBand(y: 0.1 + Double($0) * 0.08,
                                            left: 0.3 + shift, right: 0.7 + shift) },
                 confidence: 0.9, capturedAt: time, source: .neural)
    }
    func testMissesHoldThreeSecondsWithoutRefreshingTimestampOrEligibility() {
        var display = LaneDisplayContinuity()
        _ = display.update(lane(10), at: 10)
        for time in [10.2, 11, 12.99, 13] {
            XCTAssertEqual(display.update(nil, at: time)?.capturedAt, 10)
        }
        XCTAssertFalse(display.current(at: 11)!.isUsable(at: 11))
        XCTAssertNil(display.update(nil, at: 13.01))
    }
    func testSmallChangesAreSmoothedWithoutOvershoot() {
        var display = LaneDisplayContinuity()
        _ = display.update(lane(10), at: 10)
        let result = display.update(lane(10.1, shift: 0.03), at: 10.1)!
        XCTAssertGreaterThan(result.bands[0].left, 0.3)
        XCTAssertLessThan(result.bands[0].left, 0.33)
        XCTAssertEqual(result.capturedAt, 10.1)
    }
    func testSceneChangeAndRecoveryUseCurrentMeasurement() {
        var display = LaneDisplayContinuity()
        _ = display.update(lane(10), at: 10)
        XCTAssertEqual(display.update(lane(10.1, shift: 0.15), at: 10.1)!.bands[0].left, 0.45, accuracy: 0.00001)
        XCTAssertEqual(display.update(lane(11, shift: 0.1), at: 11)!.bands[0].left, 0.4)
    }
    func testOldAndInvalidResultsDoNotRenewHold() {
        var display = LaneDisplayContinuity()
        _ = display.update(lane(10), at: 10)
        XCTAssertEqual(display.update(lane(9.9), at: 10.1)?.capturedAt, 10)
        XCTAssertEqual(display.update(lane(11), at: 10.1)?.capturedAt, 10)
        XCTAssertNil(display.update(lane(10), at: 13.01))
    }
    func testResetClearsHeldCorridor() {
        var display = LaneDisplayContinuity()
        _ = display.update(lane(10), at: 10)
        display.reset()
        XCTAssertNil(display.current(at: 10.1))
    }
}
