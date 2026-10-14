import XCTest
@testable import BrakeCheckCore

final class N35RegressionTests: XCTestCase {
    func testDepartureFromStopCannotRenewZeroSpeed() throws {
        var filter = GPSFilter()
        for index in 0..<3 {
            _ = filter.ingest(GPSFix(speed: 0, speedAccuracy: 0.5, horizontalAccuracy: 5, timestamp: Double(index)),
                wallNow: Double(index), uptimeNow: 100 + Double(index))
        }
        XCTAssertEqual(filter.sample(at: 102)?.metersPerSecond, 0)
        let uncertain = filter.ingest(GPSFix(speed: 3, speedAccuracy: 2, horizontalAccuracy: 5, timestamp: 3), wallNow: 3, uptimeNow: 103)
        XCTAssertNil(uncertain)
        XCTAssertNil(filter.sample(at: 103))
        let moving = try XCTUnwrap(filter.ingest(GPSFix(speed: 4, speedAccuracy: 0.3, horizontalAccuracy: 5, timestamp: 4), wallNow: 4, uptimeNow: 104))
        XCTAssertEqual(moving.metersPerSecond, 4, accuracy: 0.01)
        XCTAssertNil(moving.acceleration)
    }

    func testCalibrationCompletesAcrossCadenceGapsAndExpires() throws {
        let lane = LanePath(bands: (0..<24).map { i in
            let y = 0.05 + Double(i) * 0.02
            let width = 0.7 - y
            return LaneBand(y: y, left: 0.5 - width / 2, right: 0.5 + width / 2)
        }, confidence: 0.9, capturedAt: 10, source: .neural)
        let estimate = try XCTUnwrap(CameraCalibrationGeometry.estimate(lane: lane, fov: 60, aspect: 1.7))
        var assistant = CameraCalibrationAssistant()
        var ready: CameraCalibrationEstimate?
        for index in 0..<8 {
            let time = 10 + Double(index) * 0.4
            ready = assistant.update(estimate, observedAt: time)
            _ = assistant.update(nil, observedAt: time + 0.2)
        }
        XCTAssertNotNil(ready)
        XCTAssertNotNil(assistant.update(nil, observedAt: 13.4))
        XCTAssertNil(assistant.update(nil, observedAt: 14.1))
        XCTAssertNil(assistant.update(estimate, observedAt: 15))
    }

    func testCalibrationCannotReuseGeometryAcrossMountChange() throws {
        let lane = LanePath(bands: (0..<24).map { i in
            let y = 0.05 + Double(i) * 0.02
            return LaneBand(y: y, left: 0.5 - (0.7 - y) / 2, right: 0.5 + (0.7 - y) / 2)
        }, confidence: 0.9, capturedAt: 10, source: .neural)
        let estimate = try XCTUnwrap(CameraCalibrationGeometry.estimate(lane: lane, fov: 60, aspect: 1.7))
        var assistant = CameraCalibrationAssistant()
        for index in 0..<8 { _ = assistant.update(estimate, observedAt: 10 + Double(index) * 0.2) }
        let changed = CameraCalibrationEstimate(horizon: estimate.horizon + 0.08,
            yawDegrees: estimate.yawDegrees + 5, pitchDegrees: estimate.pitchDegrees, confidence: 0.9)
        XCTAssertNil(assistant.update(changed, observedAt: 11.6))
        for _ in 0..<20 { XCTAssertNil(assistant.update(changed, observedAt: 11.6)) }
    }
}
