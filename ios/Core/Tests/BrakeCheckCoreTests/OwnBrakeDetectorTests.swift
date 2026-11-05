import XCTest
@testable import BrakeCheckCore

final class OwnBrakeDetectorTests: XCTestCase {
    private func sample(speed: Double, accel: Double?, at t: TimeInterval, accuracy: Double = 5,
                        simulated: Bool = false) -> GPSSpeedSample {
        GPSSpeedSample(metersPerSecond: speed, accuracy: accuracy, acceleration: accel, measurementUptime: t,
                       simulated: simulated)
    }

    func testTwoConsecutiveHardFixesFireOnceThenCoolDown() {
        var detector = OwnBrakeDetector()
        XCTAssertNil(detector.update(sample(speed: 20, accel: -1, at: 0)))
        XCTAssertNil(detector.update(sample(speed: 18, accel: -4, at: 1)))
        let event = detector.update(sample(speed: 14, accel: -4.5, at: 2))
        XCTAssertEqual(event, OwnBrakeDetector.Event(peakDeceleration: -4.5, speedAtOnset: 18))
        XCTAssertNil(detector.update(sample(speed: 10, accel: -4.5, at: 3)))
        XCTAssertNil(detector.update(sample(speed: 6, accel: -4.5, at: 4)), "still braking in the same stop")
        XCTAssertNil(detector.update(sample(speed: 20, accel: -4, at: 9)))
        XCTAssertNotNil(detector.update(sample(speed: 16, accel: -4, at: 10)), "a new stop after the cooldown counts")
    }

    func testSingleSpikeDoesNotFire() {
        var detector = OwnBrakeDetector()
        XCTAssertNil(detector.update(sample(speed: 20, accel: -6, at: 0)))
        XCTAssertNil(detector.update(sample(speed: 20, accel: -0.5, at: 1)))
        XCTAssertNil(detector.update(sample(speed: 19, accel: -6, at: 2)), "the pair was broken by a gentle fix")
    }

    func testSlowSimulatedInaccurateOrMissingFixesNeverCount() {
        var detector = OwnBrakeDetector()
        XCTAssertNil(detector.update(sample(speed: 3, accel: -5, at: 0)))
        XCTAssertNil(detector.update(sample(speed: 2, accel: -5, at: 1)), "parking-speed jolts are ignored")
        XCTAssertNil(detector.update(sample(speed: 20, accel: -5, at: 2, simulated: true)))
        XCTAssertNil(detector.update(sample(speed: 18, accel: -5, at: 3, simulated: true)))
        XCTAssertNil(detector.update(sample(speed: 20, accel: -5, at: 4, accuracy: 40)))
        XCTAssertNil(detector.update(sample(speed: 18, accel: -5, at: 5, accuracy: 40)))
        XCTAssertNil(detector.update(sample(speed: 20, accel: nil, at: 6)))
        XCTAssertNil(detector.update(nil))
    }

    func testStaleFirstFixIsReplaced() {
        var detector = OwnBrakeDetector()
        XCTAssertNil(detector.update(sample(speed: 20, accel: -4, at: 0)))
        XCTAssertNil(detector.update(sample(speed: 20, accel: -4, at: 5)), "5 s apart is not one braking event")
        XCTAssertNotNil(detector.update(sample(speed: 16, accel: -4, at: 6)))
    }
}
