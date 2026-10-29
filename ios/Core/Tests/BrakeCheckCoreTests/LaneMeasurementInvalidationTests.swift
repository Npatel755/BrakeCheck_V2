import XCTest
@testable import BrakeCheckCore

final class LaneMeasurementInvalidationTests: XCTestCase {
    private func measurement(at time: Double) -> LaneModelMeasurement {
        let lane = LanePath(bands: (0..<8).map {
            LaneBand(y: 0.1 + Double($0) * 0.03, left: 0.2, right: 0.7)
        }, confidence: 0.9, capturedAt: time, source: .neural)
        let frame = LaneLumaFrame(width: 64, height: 64,
            pixels: [UInt8](repeating: 100, count: 64 * 64), capturedAt: time)!
        return LaneModelMeasurement(lane: lane, frame: frame)!
    }

    func testNegativeCaptureInvalidatesEarlierLaneWithoutErasingOlderVehicleFrames() {
        var buffer = LaneMeasurementBuffer()
        buffer.append(measurement(at: 10))
        buffer.invalidate(at: 10.2)
        XCTAssertEqual(buffer.latest(at: 10.1)?.lane.capturedAt, 10)
        XCTAssertNil(buffer.latest(at: 10.2))
        XCTAssertNil(buffer.latest(at: 10.4))
    }

    func testFutureNegativeNeverHidesAnOlderValidVehicleFrame() {
        var buffer = LaneMeasurementBuffer()
        buffer.append(measurement(at: 10))
        buffer.invalidate(at: 20)
        XCTAssertEqual(buffer.latest(at: 10)?.lane.capturedAt, 10)
        XCTAssertEqual(buffer.latest(at: 10.5)?.lane.capturedAt, 10)
        XCTAssertNil(buffer.latest(at: 20))
    }

    func testNegativeAtTheSameCaptureTimeWinsOverItsPositiveMeasurement() {
        var buffer = LaneMeasurementBuffer()
        buffer.append(measurement(at: 10))
        buffer.invalidate(at: 10)
        XCTAssertNil(buffer.latest(at: 10))
        XCTAssertNil(buffer.latest(at: 10.1))
        buffer.append(measurement(at: 10))
        XCTAssertNil(buffer.latest(at: 10.1))
    }

    func testNewerPositiveRecoversButDoesNotResurrectTheInvalidInterval() {
        var buffer = LaneMeasurementBuffer()
        buffer.append(measurement(at: 10))
        buffer.invalidate(at: 10.2)
        buffer.append(measurement(at: 10.4))
        XCTAssertEqual(buffer.latest(at: 10.1)?.lane.capturedAt, 10)
        XCTAssertNil(buffer.latest(at: 10.3))
        XCTAssertEqual(buffer.latest(at: 10.4)?.lane.capturedAt, 10.4)
        XCTAssertEqual(buffer.latest(at: 10.5)?.lane.capturedAt, 10.4)
    }

    func testStalePositiveBackfillCannotUndoANewerNegativeOrPositiveCapture() {
        var buffer = LaneMeasurementBuffer()
        buffer.append(measurement(at: 10))
        buffer.invalidate(at: 10.2)
        buffer.append(measurement(at: 10.1))
        XCTAssertNil(buffer.latest(at: 10.3))
        buffer.append(measurement(at: 10.4))
        buffer.append(measurement(at: 10.3))
        XCTAssertNil(buffer.latest(at: 10.3))
        XCTAssertEqual(buffer.latest(at: 10.4)?.lane.capturedAt, 10.4)
    }

    func testLateOlderNegativeDoesNotInvalidateANewerPositive() {
        var buffer = LaneMeasurementBuffer()
        buffer.append(measurement(at: 10))
        buffer.append(measurement(at: 10.4))
        buffer.invalidate(at: 10.2)
        XCTAssertEqual(buffer.latest(at: 10.1)?.lane.capturedAt, 10)
        XCTAssertNil(buffer.latest(at: 10.3))
        XCTAssertEqual(buffer.latest(at: 10.5)?.lane.capturedAt, 10.4)
    }

    func testMultipleNegativesPreserveTheEarliestLossForHistoricalQueries() {
        var buffer = LaneMeasurementBuffer()
        buffer.append(measurement(at: 10))
        buffer.invalidate(at: 10.4)
        buffer.invalidate(at: 10.2)
        buffer.invalidate(at: 10.8)
        XCTAssertEqual(buffer.latest(at: 10.1)?.lane.capturedAt, 10)
        XCTAssertNil(buffer.latest(at: 10.3))
        XCTAssertNil(buffer.latest(at: 10.6))
        buffer.append(measurement(at: 10.9))
        XCTAssertEqual(buffer.latest(at: 11)?.lane.capturedAt, 10.9)
    }

    func testInvalidNegativeTimestampsAreIgnoredAndResetClearsTheWatermark() {
        var buffer = LaneMeasurementBuffer()
        buffer.append(measurement(at: 10))
        for value in [Double.nan, .infinity, -.infinity] { buffer.invalidate(at: value) }
        XCTAssertEqual(buffer.latest(at: 10.1)?.lane.capturedAt, 10)
        buffer.invalidate(at: 20)
        buffer.reset()
        XCTAssertNil(buffer.latest(at: 10.1))
        buffer.append(measurement(at: 1))
        XCTAssertEqual(buffer.latest(at: 1.1)?.lane.capturedAt, 1)
    }

    func testPositiveHistoryRetainsItsExistingSixEntryLimitAndOneSecondLookupWindow() {
        var buffer = LaneMeasurementBuffer()
        for index in 0...6 { buffer.append(measurement(at: 10 + Double(index) / 10)) }
        XCTAssertNil(buffer.latest(at: 10.05))
        XCTAssertEqual(buffer.latest(at: 10.15)?.lane.capturedAt, 10.1)
        XCTAssertEqual(buffer.latest(at: 10.6)?.lane.capturedAt, 10.6)
        XCTAssertNil(buffer.latest(at: 11.61))
    }
}
