import XCTest
@testable import BrakeCheckCore

final class CaptureFrameTimingTests: XCTestCase {
    func testDeliveryDelayDoesNotBecomeFreshCaptureTime() throws {
        let captured = try XCTUnwrap(CaptureFrameTiming.captureUptime(frameHostTime: 1000,
            hostNow: 1000.4, uptimeNow: 200.4))
        XCTAssertEqual(captured, 200, accuracy: 0.000001)
        XCTAssertTrue(TrackingFreshness.measurementIsFresh(capturedAt: captured, now: 200.5))
        XCTAssertFalse(TrackingFreshness.measurementIsFresh(capturedAt: captured, now: 200.7))
    }

    func testDelayedFrameUsesTheGPSFixAtCaptureRatherThanAtReceipt() throws {
        var history = FrameAlignedSpeedHistory()
        history.append(GPSSpeedSample(metersPerSecond: 10, accuracy: 0.5, acceleration: nil,
            measurementUptime: 99.9, simulated: false))
        history.append(GPSSpeedSample(metersPerSecond: 15, accuracy: 0.5, acceleration: nil,
            measurementUptime: 100.2, simulated: false))
        let captured = try XCTUnwrap(CaptureFrameTiming.captureUptime(frameHostTime: 900,
            hostNow: 900.4, uptimeNow: 100.4))
        XCTAssertEqual(history.reading(at: captured)?.metersPerSecond, 10)
        XCTAssertEqual(history.reading(at: 100.4)?.metersPerSecond, 15)
    }

    func testJitterInDeliveryDoesNotInventUnevenCameraMotionIntervals() throws {
        var captures: [Double] = []
        for (frame, delay) in [0.0, 0.15, 0.03, 0.20, 0.07].enumerated() {
            let time = Double(frame) * 0.1
            captures.append(try XCTUnwrap(CaptureFrameTiming.captureUptime(frameHostTime: 1000 + time,
                hostNow: 1000 + time + delay, uptimeNow: 100 + time + delay)))
        }
        for pair in zip(captures, captures.dropFirst()) {
            XCTAssertEqual(pair.1 - pair.0, 0.1, accuracy: 0.000001)
        }
    }

    func testOldFutureAndInvalidClockValuesCannotAuthorizeInference() {
        XCTAssertNil(CaptureFrameTiming.captureUptime(frameHostTime: 10, hostNow: 10.61, uptimeNow: 100))
        XCTAssertNil(CaptureFrameTiming.captureUptime(frameHostTime: 10.1, hostNow: 10, uptimeNow: 100))
        XCTAssertNil(CaptureFrameTiming.captureUptime(frameHostTime: -1, hostNow: 0, uptimeNow: 100))
        XCTAssertNil(CaptureFrameTiming.captureUptime(frameHostTime: 10, hostNow: 10.5, uptimeNow: 0.1))
        for value in [Double.nan, .infinity, -.infinity] {
            XCTAssertNil(CaptureFrameTiming.captureUptime(frameHostTime: value, hostNow: 10, uptimeNow: 100))
            XCTAssertNil(CaptureFrameTiming.captureUptime(frameHostTime: 10, hostNow: value, uptimeNow: 100))
            XCTAssertNil(CaptureFrameTiming.captureUptime(frameHostTime: 10, hostNow: 10, uptimeNow: value))
        }
    }
}
