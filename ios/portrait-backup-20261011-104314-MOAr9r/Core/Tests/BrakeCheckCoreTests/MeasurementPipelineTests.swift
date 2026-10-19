import Foundation
import XCTest
@testable import BrakeCheckCore

final class MeasurementPipelineTests: XCTestCase {
    private func gps(_ time: Double, speed: Double = 8, simulated: Bool = false) -> GPSSpeedSample {
        GPSSpeedSample(metersPerSecond: speed, accuracy: 0.5, acceleration: nil,
                       measurementUptime: time, simulated: simulated)
    }

    func testCameraBeforeNewestGPSUsesPreviousFix() throws {
        var history = FrameAlignedSpeedHistory()
        history.append(gps(100)); history.append(gps(101, speed: 12))
        let reading = try XCTUnwrap(history.reading(at: 100.8))
        XCTAssertEqual(reading.measurementUptime, 100)
        XCTAssertEqual(reading.metersPerSecond, 8)
        XCTAssertEqual(history.reading(at: 101.1)?.metersPerSecond, 12)
    }

    func testNoFutureStaleSimulatedOrCrossTripGPS() {
        var history = FrameAlignedSpeedHistory()
        history.append(gps(10))
        XCTAssertNil(history.reading(at: 9.9))
        XCTAssertNil(history.reading(at: 11.6))
        history.append(gps(11, simulated: true))
        XCTAssertEqual(history.reading(at: 11)?.measurementUptime, 10)
        history.reset()
        XCTAssertNil(history.reading(at: 11))
    }

    func testStationaryRearCandidateProducesCameraNumbersWithoutGPSOrLane() throws {
        var estimator = CameraRangeEstimator()
        let id = UUID()
        var result = CameraRangeSnapshot()
        for frame in 0...12 {
            result = estimator.update(targetID: id, time: 100 + Double(frame) * 0.1,
                                      distance: 12, confidence: 0.94, fresh: true)
        }
        XCTAssertEqual(result.distance, 12)
        XCTAssertEqual(try XCTUnwrap(result.closingSpeed), 0, accuracy: 0.0001)
        XCTAssertEqual(result.reason, "Gap steady")
    }

    func testApproachingAndRecedingGapHaveCorrectSign() throws {
        for rate in [-2.0, 2.0] {
            var estimator = CameraRangeEstimator()
            let id = UUID()
            var result = CameraRangeSnapshot()
            for frame in 0...12 {
                let t = Double(frame) * 0.1
                result = estimator.update(targetID: id, time: 100 + t,
                    distance: 20 + rate * t, confidence: 0.9, fresh: true)
            }
            XCTAssertEqual(try XCTUnwrap(result.closingSpeed), max(0, -rate), accuracy: 0.0001)
            XCTAssertEqual(result.reason, rate < 0 ? "Gap closing" : "Gap opening")
        }
    }

    func testLowConfidenceFrameDoesNotRestartContinuousReliableHistory() throws {
        var estimator = CameraRangeEstimator()
        let id = UUID()
        var result = CameraRangeSnapshot()
        for frame in 0...12 {
            result = estimator.update(targetID: id, time: 100 + Double(frame) * 0.1,
                distance: 12, confidence: frame == 6 ? 0.5 : 0.9, fresh: true)
            if frame == 6 { XCTAssertNil(result.closingSpeed) }
        }
        XCTAssertEqual(try XCTUnwrap(result.closingSpeed), 0, accuracy: 0.0001)
    }

    func testCoastingAndChangedTargetCannotPublishPreviousMotion() throws {
        var estimator = CameraRangeEstimator()
        let id = UUID()
        for frame in 0...12 {
            _ = estimator.update(targetID: id, time: 100 + Double(frame) * 0.1,
                                 distance: 12, confidence: 0.9, fresh: true)
        }
        let coast = estimator.update(targetID: id, time: 101.3, distance: 12,
                                     confidence: 0.9, fresh: false)
        XCTAssertNil(coast.distance); XCTAssertNil(coast.closingSpeed)
        let changed = estimator.update(targetID: UUID(), time: 101.4, distance: 35,
                                       confidence: 0.9, fresh: true)
        XCTAssertEqual(changed.distance, 35); XCTAssertNil(changed.closingSpeed)
    }

    func testLongGapAndAbruptRangeChangeRestartMotionWindow() {
        var estimator = CameraRangeEstimator()
        let id = UUID()
        for frame in 0...12 {
            _ = estimator.update(targetID: id, time: 100 + Double(frame) * 0.1,
                                 distance: 12, confidence: 0.9, fresh: true)
        }
        let jump = estimator.update(targetID: id, time: 101.3, distance: 40,
                                    confidence: 0.9, fresh: true)
        XCTAssertNil(jump.closingSpeed)
        let gap = estimator.update(targetID: id, time: 103, distance: 40,
                                   confidence: 0.9, fresh: true)
        XCTAssertNil(gap.closingSpeed)
    }

    func testBoxWobbleCannotMasqueradeAsStableSpeed() {
        var estimator = CameraRangeEstimator()
        let id = UUID()
        var result = CameraRangeSnapshot()
        for frame in 0...12 {
            result = estimator.update(targetID: id, time: 100 + Double(frame) * 0.1,
                distance: frame % 2 == 0 ? 19.4 : 20.6, confidence: 0.9, fresh: true)
        }
        XCTAssertEqual(result.distance, 19.4)
        XCTAssertNil(result.closingSpeed)
        XCTAssertEqual(result.reason, "Gap available · range change unsettled")
    }
}
