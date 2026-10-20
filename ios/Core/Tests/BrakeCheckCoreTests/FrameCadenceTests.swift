import XCTest
@testable import BrakeCheckCore

final class FrameCadenceTests: XCTestCase {
    func testCameraJitterKeepsTenHertzOverThirtySeconds() {
        var cadence = FrameCadence()
        let jitter = [0.0, 0.0004, -0.0003, -0.0004, 0.0002]
        let accepted = (0..<900).compactMap { index -> Double? in
            let time = 100 + Double(index) / 30 + jitter[index % jitter.count]
            return cadence.shouldRun(at: time, interval: 0.1) ? time : nil
        }
        XCTAssertEqual(accepted.count, 300)
        for (previous, current) in zip(accepted, accepted.dropFirst()) {
            XCTAssertGreaterThan(current - previous, 0.09)
        }
    }

    func testSeriousThermalDoubleGateMaintainsIntendedAverageWithoutOvershoot() {
        var camera = FrameCadence(), detector = FrameCadence()
        let policy = VisionWorkPolicy(night: false, heat: .serious)
        var accepted: [Double] = []
        for index in 0...1800 {
            let time = Double(index) / 30
            if camera.shouldRun(at: time, interval: 0.2),
               detector.shouldRun(at: time, interval: policy.vehicleInterval) {
                accepted.append(time)
            }
        }
        XCTAssertEqual(accepted.count, 181) // Three predictions/second plus the initial capture.
        for (previous, current) in zip(accepted, accepted.dropFirst()) {
            XCTAssertGreaterThanOrEqual(current - previous, policy.vehicleInterval * 0.5 - 1e-6)
        }
        for start in stride(from: 0.0, through: 50.0, by: 0.37) {
            let count = accepted.filter { $0 >= start && $0 < start + 10 }.count
            XCTAssertLessThanOrEqual(count, 31)
        }
    }

    func testIrregularOffersSkipMissedSlotsWithoutCatchUpBacklog() {
        var cadence = FrameCadence()
        let spacing = [0.07, 0.11, 0.29, 0.04, 0.16, 0.23, 0.031]
        var time = 0.0, accepted: [Double] = []
        while time < 120 {
            if cadence.shouldRun(at: time, interval: 1.0 / 3) { accepted.append(time) }
            time += spacing[accepted.count % spacing.count]
        }
        XCTAssertLessThanOrEqual(accepted.count, 361)
        for (previous, current) in zip(accepted, accepted.dropFirst()) {
            XCTAssertGreaterThanOrEqual(current - previous, 1.0 / 6 - 1e-6)
        }
        XCTAssertTrue(cadence.shouldRun(at: 500, interval: 1.0 / 3))
        XCTAssertFalse(cadence.shouldRun(at: 500.001, interval: 1.0 / 3))
        XCTAssertFalse(cadence.shouldRun(at: 500, interval: 1.0 / 3))
    }

    func testBusyWorkerDoesNotConsumeTheNextFreshCaptureOpportunity() {
        var committed = FrameCadence()
        XCTAssertTrue(committed.shouldRun(at: 1, interval: 0.1))
        var rejected = committed
        XCTAssertTrue(rejected.shouldRun(at: 1.1, interval: 0.1))
        // The worker rejected 1.1; only commit the next accepted capture.
        var retry = committed
        XCTAssertTrue(retry.shouldRun(at: 1.133, interval: 0.1))
        committed = retry
        XCTAssertFalse(committed.shouldRun(at: 1.134, interval: 0.1))
        XCTAssertTrue(committed.shouldRun(at: 1.2, interval: 0.1))
    }

    func testThermalIntervalChangesResetAndInvalidTimes() {
        var cadence = FrameCadence()
        XCTAssertTrue(cadence.shouldRun(at: 1, interval: 0.1))
        XCTAssertFalse(cadence.shouldRun(at: 1.2, interval: 0.5))
        XCTAssertTrue(cadence.shouldRun(at: 1.5, interval: 0.5))
        XCTAssertFalse(cadence.shouldRun(at: .nan, interval: 0.1))
        XCTAssertFalse(cadence.shouldRun(at: 2, interval: .infinity))
        XCTAssertFalse(cadence.shouldRun(at: 2, interval: 0))
        XCTAssertFalse(cadence.shouldRun(at: 1, interval: 0.1))
        cadence.reset()
        XCTAssertTrue(cadence.shouldRun(at: 0, interval: 0.5))
    }
}
