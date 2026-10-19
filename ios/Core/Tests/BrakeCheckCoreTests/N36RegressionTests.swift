import XCTest
@testable import BrakeCheckCore

final class N36RegressionTests: XCTestCase {
    private func lane(_ time: Double, shift: Double = 0) -> LanePath {
        LanePath(bands: (0..<16).map { index in
            let y = 0.05 + Double(index) * 0.025
            return LaneBand(y: y, left: 0.5 - (0.7 - y) / 2 + shift, right: 0.5 + (0.7 - y) / 2 + shift)
        }, confidence: 0.9, capturedAt: time, source: .neural)
    }

    func testDisplayAttenuatesAlternatingJitterWithoutOvershoot() throws {
        var display = LaneDisplayContinuity(rejectOutliers: true, smoothingSeconds: 0.4)
        let initial = lane(10)
        _ = display.update(initial, at: 10)
        var outputError = 0.0
        for index in 1...40 {
            let time = 10 + Double(index) * 0.05
            let shift = index.isMultiple(of: 2) ? 0.025 : -0.025
            let result = try XCTUnwrap(display.update(lane(time, shift: shift), at: time))
            let error = result.bands[0].left - initial.bands[0].left
            XCTAssertLessThanOrEqual(abs(error), 0.025)
            XCTAssertEqual(result.capturedAt, time)
            outputError += error * error
        }
        XCTAssertLessThan(sqrt(outputError / 40), 0.0125)
    }

    func testOneOutlierAndRepeatedFramesCannotMoveDisplayedLane() {
        var display = LaneDisplayContinuity(rejectOutliers: true)
        _ = display.update(lane(10), at: 10)
        for _ in 0..<20 {
            XCTAssertEqual(display.update(lane(10.1, shift: 0.15), at: 10.1)?.capturedAt, 10)
        }
        XCTAssertEqual(display.update(lane(10.2), at: 10.2)?.bands[0].left, lane(10).bands[0].left)
    }

    func testPersistentNewGeometryReplacesOutlierHold() {
        var display = LaneDisplayContinuity(rejectOutliers: true)
        _ = display.update(lane(10), at: 10)
        XCTAssertEqual(display.update(lane(10.1, shift: 0.15), at: 10.1)?.capturedAt, 10)
        XCTAssertEqual(display.update(lane(10.2, shift: 0.15), at: 10.2)?.capturedAt, 10)
        XCTAssertEqual(display.update(lane(10.3, shift: 0.15), at: 10.3)?.capturedAt, 10.3)
        XCTAssertEqual(display.current(at: 10.3)?.bands[0].left, lane(10.3, shift: 0.15).bands[0].left)
        display.reset()
        XCTAssertNil(display.current(at: 10.3))
    }

    func testDisplayHoldCannotRenewSafetyFreshness() throws {
        var display = LaneDisplayContinuity(rejectOutliers: true)
        _ = display.update(lane(10), at: 10)
        let held = try XCTUnwrap(display.update(nil, at: 10.8))
        XCTAssertEqual(held.capturedAt, 10)
        XCTAssertFalse(held.isUsable(at: 10.8))
        XCTAssertNil(display.update(nil, at: 13.01))
    }

    func testRoadOutlineHoldExpiresAndNeverSuppliesSemanticMask() throws {
        let road = RoadRegion(bands: lane(10).bands, confidence: 0.9, capturedAt: 10,
            visibility: RoadVisibilityMask(width: 2, height: 2, pixels: [1, 1, 1, 1]))
        var display = RoadDisplayContinuity()
        let result = try XCTUnwrap(display.update(road, at: 10))
        XCTAssertNil(result.visibility)
        XCTAssertTrue(result.bands.allSatisfy { $0.y >= road.bands.first!.y && $0.y <= road.bands.last!.y })
        XCTAssertEqual(display.update(nil, at: 10.7)?.capturedAt, 10)
        XCTAssertFalse(display.current(at: 10.7)!.isUsable(at: 10.7))
        XCTAssertNil(display.update(nil, at: 10.81))
    }

    func testHUDTextDoesNotFlickerButPersistentChangeAppears() {
        var status = ARStatusPresentation(initial: "Set up camera")
        for index in 0..<20 {
            XCTAssertEqual(status.update(index.isMultiple(of: 2) ? "Lost" : "Directional", at: Double(index) * 0.1), "Set up camera")
        }
        _ = status.update("Waiting for GPS", at: 3)
        XCTAssertEqual(status.update("Waiting for GPS", at: 3.7), "Waiting for GPS")
        XCTAssertEqual(status.update("Old frame", at: 2), "Waiting for GPS")
    }

    func testSlowAuxiliaryModelCanRecoverWithoutBypassingVehicleBudget() {
        var history = AuxiliaryLatencyHistory()
        for time in [0.0, 0.2, 0.4] { history.observe(0.25, at: time) }
        XCTAssertEqual(history.seconds(at: 1), 0.25)
        let throttled = ARInferencePolicy(minimumLaneInterval: 0.2, heat: .nominal, vehicleLatency: 0.05,
            laneLatency: history.seconds(at: 1), roadLatency: 0.02)
        XCTAssertTrue(throttled.laneAllowed)
        XCTAssertEqual(throttled.laneInterval, 0.5)
        XCTAssertEqual(history.seconds(at: 5.5), 0)
        history.observe(0.04, at: 5.5)
        XCTAssertEqual(history.seconds(at: 5.6), 0.04)
        XCTAssertTrue(ARInferencePolicy(minimumLaneInterval: 0.2, heat: .nominal, vehicleLatency: 0.05,
            laneLatency: history.seconds(at: 5.6), roadLatency: 0.02).laneAllowed)
        XCTAssertFalse(ARInferencePolicy(minimumLaneInterval: 0.2, heat: .nominal, vehicleLatency: 0.3,
            laneLatency: 0, roadLatency: 0).laneAllowed)
        XCTAssertNil(ARInferencePolicy(minimumLaneInterval: 0.2, heat: .critical, vehicleLatency: 0.01,
            laneLatency: 0, roadLatency: 0).roadInterval)
    }

    func testCalibrationProgressCountsOnlyDistinctMeasurements() throws {
        let estimate = try XCTUnwrap(CameraCalibrationGeometry.estimate(lane: lane(10), fov: 60, aspect: 1.7))
        var assistant = CameraCalibrationAssistant()
        for _ in 0..<20 { _ = assistant.update(estimate, observedAt: 10) }
        XCTAssertEqual(assistant.observationCount, 1)
        for index in 1..<8 { _ = assistant.update(estimate, observedAt: 10 + Double(index) * 0.2) }
        XCTAssertEqual(assistant.observationCount, 8)
        XCTAssertNotNil(assistant.update(estimate, observedAt: 11.4))
    }

    func testParkedVerificationRemainsAvailableBetweenValidGPSUpdatesAndExpires() {
        let measured = GPSSpeedSample(metersPerSecond: 0, accuracy: 0.5, acceleration: nil, measurementUptime: 10, simulated: false)
        for time in [10.0, 10.2, 10.8, 11.5] { XCTAssertTrue(measured.isFreshStationary(at: time)) }
        XCTAssertFalse(measured.isFreshStationary(at: 11.51))
        XCTAssertFalse(measured.isFreshStationary(at: 9.9))
        XCTAssertFalse(GPSSpeedSample(metersPerSecond: 0, accuracy: 2, acceleration: nil, measurementUptime: 10, simulated: false).isFreshStationary(at: 10))
        XCTAssertFalse(GPSSpeedSample(metersPerSecond: 0, accuracy: 0.5, acceleration: nil, measurementUptime: 10, simulated: true).isFreshStationary(at: 10))
        XCTAssertFalse(GPSSpeedSample(metersPerSecond: 2, accuracy: 0.5, acceleration: nil, measurementUptime: 10, simulated: false).isFreshStationary(at: 10))
    }
}
