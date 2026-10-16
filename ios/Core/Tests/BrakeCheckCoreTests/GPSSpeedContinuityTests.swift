import Foundation
import XCTest
@testable import BrakeCheckCore

final class GPSSpeedContinuityTests: XCTestCase {
    private func sample(_ time: Double, _ speed: Double, accuracy: Double = 0.5, simulated: Bool = false) -> GPSSpeedSample {
        GPSSpeedSample(metersPerSecond: speed, accuracy: accuracy, acceleration: nil,
            measurementUptime: time, simulated: simulated)
    }

    func testShortGapKeepsSeparateEstimateWithOriginalMeasurementAge() throws {
        var history = FrameAlignedSpeedHistory()
        history.append(sample(100, 12))
        XCTAssertNil(history.reading(at: 101.8))
        let estimate = try XCTUnwrap(history.estimate(at: 101.8))
        XCTAssertEqual(estimate.metersPerSecond, 12)
        XCTAssertEqual(estimate.lastMeasurementUptime, 100)
        XCTAssertEqual(estimate.source, .held)
        XCTAssertTrue(estimate.isEstimated)
        XCTAssertGreaterThan(estimate.accuracy, 3)
        XCTAssertNil(history.estimate(at: 102.6))
    }

    func testSteadyTrendProjectsSpeedButCapsTotalChange() throws {
        var history = FrameAlignedSpeedHistory()
        for time in 0...3 { history.append(sample(Double(time), 10 + 2 * Double(time))) }
        let estimate = try XCTUnwrap(history.estimate(at: 4.8))
        XCTAssertEqual(estimate.source, .projected)
        XCTAssertEqual(estimate.metersPerSecond, 19, accuracy: 0.0001)
        XCTAssertEqual(estimate.lastMeasurementUptime, 3)
        XCTAssertNil(history.estimate(at: 5.6))
    }

    func testNoisyOrRapidlyChangingTrendFallsBackToBoundedHold() throws {
        for speeds in [[10.0, 12, 10, 12], [10, 15, 20, 25]] {
            var history = FrameAlignedSpeedHistory()
            for (index, speed) in speeds.enumerated() { history.append(sample(Double(index), speed)) }
            let estimate = try XCTUnwrap(history.estimate(at: 4.8))
            XCTAssertEqual(estimate.source, .held)
            XCTAssertEqual(estimate.metersPerSecond, speeds.last)
        }
    }

    func testUncertaintyGrowsAndPoorFixCannotBridgeDropout() throws {
        var history = FrameAlignedSpeedHistory()
        history.append(sample(10, 15))
        let earlier = try XCTUnwrap(history.estimate(at: 11.6))
        let later = try XCTUnwrap(history.estimate(at: 12.4))
        XCTAssertGreaterThan(later.accuracy, earlier.accuracy)
        history.reset(); history.append(sample(10, 15, accuracy: 4))
        XCTAssertNotNil(history.estimate(at: 11))
        XCTAssertNil(history.estimate(at: 11.6))
    }

    func testMeasuredReadingsRemainCausalWhileLaterFixInterpolatesDisplay() throws {
        var history = FrameAlignedSpeedHistory()
        history.append(sample(100, 8)); history.append(sample(101, 12))
        let actual = try XCTUnwrap(history.reading(at: 100.8))
        XCTAssertEqual(actual.metersPerSecond, 8)
        XCTAssertEqual(actual.measurementUptime, 100)
        let display = try XCTUnwrap(history.estimate(at: 100.8))
        XCTAssertEqual(display.metersPerSecond, 11.2, accuracy: 0.0001)
        XCTAssertEqual(display.source, .interpolated)
        XCTAssertEqual(history.estimate(at: 101)?.source, .measured)
        XCTAssertNil(history.estimate(at: 99.9))
    }

    func testLaterFixCannotInterpolateLongGapOrImplausibleChange() throws {
        var history = FrameAlignedSpeedHistory()
        history.append(sample(10, 10)); history.append(sample(13, 13))
        XCTAssertEqual(history.estimate(at: 11)?.source, .measured)
        history.reset(); history.append(sample(10, 10)); history.append(sample(11, 30))
        let estimate = try XCTUnwrap(history.estimate(at: 10.5))
        XCTAssertEqual(estimate.source, .measured)
        XCTAssertEqual(estimate.metersPerSecond, 10)
    }

    func testMissingInitialFixResetAndInvalidQueriesStayUnavailable() {
        var history = FrameAlignedSpeedHistory()
        XCTAssertNil(history.estimate(at: 10))
        history.append(sample(10, 10))
        XCTAssertNil(history.estimate(at: .nan))
        XCTAssertNil(history.estimate(at: .infinity))
        history.reset()
        XCTAssertNil(history.estimate(at: 10.5))
    }

    func testRejectedFixesAndRepeatedDisplayQueriesNeverRenewEstimate() {
        var history = FrameAlignedSpeedHistory()
        history.append(sample(10, 10))
        history.append(sample(11, 90, simulated: true))
        history.append(sample(12, .nan)); history.append(sample(9, 15))
        history.append(sample(10, 15)); history.append(sample(12, 15, accuracy: 9))
        for time in [11.6, 11.8, 12.0, 12.2] {
            XCTAssertEqual(history.estimate(at: time)?.lastMeasurementUptime, 10)
        }
        XCTAssertNil(history.estimate(at: 12.6))
        XCTAssertNil(history.reading(at: 12.0))
    }

    func testRecoveryUsesRealNewFixAndDoesNotForecastAcrossLongOutage() throws {
        var history = FrameAlignedSpeedHistory()
        for time in 0...2 { history.append(sample(Double(time), 10 + Double(time))) }
        XCTAssertNil(history.estimate(at: 8))
        history.append(sample(9, 0))
        let real = try XCTUnwrap(history.estimate(at: 9))
        XCTAssertEqual(real.metersPerSecond, 0)
        XCTAssertFalse(real.isEstimated)
        XCTAssertEqual(history.estimate(at: 10.8)?.source, .held)
    }

    func testDecelerationProjectionCannotBecomeNegativeSpeed() throws {
        var history = FrameAlignedSpeedHistory()
        for time in 0...3 { history.append(sample(Double(time), 7 - 2 * Double(time))) }
        let estimate = try XCTUnwrap(history.estimate(at: 4.8))
        XCTAssertEqual(estimate.metersPerSecond, 0)
        XCTAssertEqual(estimate.source, .projected)
    }

    func testFreshCameraClosureCanSupplyDisplayLeadSpeedDuringGPSGap() throws {
        var history = FrameAlignedSpeedHistory()
        history.append(sample(100, 12))
        let ego = try XCTUnwrap(history.estimate(at: 101.7))
        let camera = CameraRangeSnapshot(distance: 20, closingSpeed: 2, rangeRate: -2)
        let speed = GPSSpeedDisplayRules.estimatedLeadSpeed(ego: ego, camera: camera,
            confidence: 0.9, rearConfirmed: true, capturedAt: 101.7, now: 101.8)
        XCTAssertEqual(speed, 10)
        // The estimate is not written into a GPS sample or RiskSnapshot.
        let risk = CollisionApproachRules.evaluateCamera(camera, confidence: 0.9)
        XCTAssertNil(risk.leadSpeed); XCTAssertNil(risk.leadAcceleration); XCTAssertNil(risk.headway)
        XCTAssertEqual(risk.timeToCollision, 10)
    }

    func testEstimatedLeadSpeedNeedsCurrentConfirmedCameraMotion() throws {
        var history = FrameAlignedSpeedHistory()
        history.append(sample(100, 12))
        let ego = try XCTUnwrap(history.estimate(at: 101.7))
        let good = CameraRangeSnapshot(distance: 20, closingSpeed: 2, rangeRate: -2)
        func display(_ camera: CameraRangeSnapshot? = nil, confidence: Double = 0.9,
                     rear: Bool = true, capture: Double = 101.7, now: Double = 101.8) -> Double? {
            GPSSpeedDisplayRules.estimatedLeadSpeed(ego: ego, camera: camera ?? good,
                confidence: confidence, rearConfirmed: rear, capturedAt: capture, now: now)
        }
        XCTAssertNil(display(rear: false))
        XCTAssertNil(display(confidence: 0.6))
        XCTAssertNil(display(capture: 101.1))
        XCTAssertNil(display(capture: 101.9))
        XCTAssertNil(display(now: 102.4))
        XCTAssertNil(display(capture: 101.8))
        XCTAssertNil(display(CameraRangeSnapshot(distance: 20)))
        XCTAssertNil(display(CameraRangeSnapshot(closingSpeed: 2, rangeRate: -2)))
        XCTAssertNil(display(CameraRangeSnapshot(distance: 20, closingSpeed: 2, rangeRate: 2)))
        XCTAssertNil(display(CameraRangeSnapshot(distance: 20, closingSpeed: 40, rangeRate: -40)))
    }

    func testLaterFixRefinesDisplayAtCameraTimeInsteadOfUsingNewestSpeed() throws {
        var history = FrameAlignedSpeedHistory()
        history.append(sample(100, 8)); history.append(sample(101, 12))
        let ego = try XCTUnwrap(history.estimate(at: 100.8))
        let camera = CameraRangeSnapshot(distance: 20, closingSpeed: 2, rangeRate: -2)
        let lead = try XCTUnwrap(GPSSpeedDisplayRules.estimatedLeadSpeed(ego: ego, camera: camera,
            confidence: 0.9, rearConfirmed: true, capturedAt: 100.8, now: 101.1))
        XCTAssertEqual(lead, 9.2, accuracy: 0.0001)
        XCTAssertEqual(history.reading(at: 100.8)?.metersPerSecond, 8)
        XCTAssertEqual(history.reading(at: 101.1)?.metersPerSecond, 12)
    }
}
