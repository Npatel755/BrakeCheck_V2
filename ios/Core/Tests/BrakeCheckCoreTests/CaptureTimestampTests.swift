import XCTest
@testable import BrakeCheckCore

final class CaptureTimestampTests: XCTestCase {
    func testDeliveryDelayIsPreservedAcrossDifferentClockEpochs() throws {
        var clock = CaptureTimestamp()
        clock.reset(notBefore: 90)
        let captured = try XCTUnwrap(clock.uptime(hostTimestamp: 8_000.25,
            hostNow: 8_001, receivedAt: 100))
        XCTAssertEqual(captured, 99.25, accuracy: 1e-9)
        XCTAssertFalse(TrackingFreshness.measurementIsFresh(capturedAt: captured, now: 100))
        XCTAssertTrue(TrackingFreshness.identityIsFresh(capturedAt: captured, now: 100))
    }

    func testNegativeHostEpochDoesNotDiscardAValidConvertedFrame() throws {
        var clock = CaptureTimestamp()
        clock.reset(notBefore: 0)
        XCTAssertEqual(try XCTUnwrap(clock.uptime(hostTimestamp: -500.25,
            hostNow: -500, receivedAt: 100)), 99.75, accuracy: 1e-9)
    }

    func testNoMappingBeforeResetOrWithUnavailableClocks() {
        var clock = CaptureTimestamp()
        XCTAssertNil(clock.uptime(hostTimestamp: 10, hostNow: 10, receivedAt: 10))
        clock.reset(notBefore: 1)
        XCTAssertNil(clock.uptime(hostTimestamp: nil, hostNow: 10, receivedAt: 10))
        XCTAssertNil(clock.uptime(hostTimestamp: 10, hostNow: nil, receivedAt: 10))
        for invalid in [Double.nan, .infinity, -.infinity] {
            XCTAssertNil(clock.uptime(hostTimestamp: invalid, hostNow: 10, receivedAt: 10))
            XCTAssertNil(clock.uptime(hostTimestamp: 10, hostNow: invalid, receivedAt: 10))
            XCTAssertNil(clock.uptime(hostTimestamp: 10, hostNow: 10, receivedAt: invalid))
        }
        XCTAssertNil(clock.uptime(hostTimestamp: 10, hostNow: 10, receivedAt: -1))
        XCTAssertNil(clock.uptime(hostTimestamp: -Double.greatestFiniteMagnitude,
            hostNow: Double.greatestFiniteMagnitude, receivedAt: 10))
        // Bad readings do not consume the next valid frame.
        XCTAssertEqual(clock.uptime(hostTimestamp: 10, hostNow: 10, receivedAt: 10), 10)
    }

    func testFutureClockMismatchIsRejectedButTinyRoundingLeadIsBounded() throws {
        var clock = CaptureTimestamp()
        clock.reset(notBefore: 0)
        XCTAssertNil(clock.uptime(hostTimestamp: 20.01, hostNow: 20, receivedAt: 100))
        let value = try XCTUnwrap(clock.uptime(hostTimestamp: 20.001,
            hostNow: 20, receivedAt: 100))
        XCTAssertEqual(value, 100) // Never emits an uptime in the future.
        XCTAssertTrue(TrackingFreshness.measurementIsFresh(capturedAt: value, now: 100))
        XCTAssertNil(clock.uptime(hostTimestamp: 21, hostNow: 20, receivedAt: 100.01))
        XCTAssertEqual(try XCTUnwrap(clock.uptime(hostTimestamp: 20.05,
            hostNow: 20.06, receivedAt: 100.06)), 100.05, accuracy: 1e-9)
    }

    func testDuplicateAndRegressingHostTimestampsCannotRenewMeasurements() throws {
        var clock = CaptureTimestamp()
        clock.reset(notBefore: 0)
        XCTAssertEqual(try XCTUnwrap(clock.uptime(hostTimestamp: 500,
            hostNow: 500.1, receivedAt: 10.1)), 10, accuracy: 1e-9)
        XCTAssertNil(clock.uptime(hostTimestamp: 500, hostNow: 501, receivedAt: 11))
        XCTAssertNil(clock.uptime(hostTimestamp: 499.9, hostNow: 501, receivedAt: 11))
        XCTAssertEqual(try XCTUnwrap(clock.uptime(hostTimestamp: 500.2,
            hostNow: 501, receivedAt: 11)), 10.2, accuracy: 1e-9)
    }

    func testMappedUptimeMustAlsoIncreaseAndRejectedReadsDoNotPoisonHistory() throws {
        var clock = CaptureTimestamp()
        clock.reset(notBefore: 0)
        XCTAssertEqual(clock.uptime(hostTimestamp: 500, hostNow: 500, receivedAt: 10), 10)
        // Both clocks advanced, but an inconsistent sample pair maps backward.
        XCTAssertNil(clock.uptime(hostTimestamp: 500.1, hostNow: 501, receivedAt: 10.5))
        XCTAssertEqual(try XCTUnwrap(clock.uptime(hostTimestamp: 500.1,
            hostNow: 501, receivedAt: 11)), 10.1, accuracy: 1e-9)
    }

    func testStartAndRotationBoundariesRejectQueuedOldFrames() throws {
        var clock = CaptureTimestamp()
        clock.reset(notBefore: 100)
        XCTAssertNil(clock.uptime(hostTimestamp: 1_000, hostNow: 1_001, receivedAt: 100.5))
        XCTAssertEqual(clock.uptime(hostTimestamp: 1_000.5, hostNow: 1_001, receivedAt: 100.5), 100)
        clock.reset(notBefore: 102.15)
        XCTAssertNil(clock.uptime(hostTimestamp: 1_002.1, hostNow: 1_003, receivedAt: 103))
        XCTAssertEqual(try XCTUnwrap(clock.uptime(hostTimestamp: 1_002.2,
            hostNow: 1_003, receivedAt: 103)), 102.2, accuracy: 1e-9)
    }

    func testResetClearsTimestampHistoryForTheNextSession() throws {
        var clock = CaptureTimestamp()
        clock.reset(notBefore: 0)
        XCTAssertEqual(clock.uptime(hostTimestamp: 900, hostNow: 900, receivedAt: 5), 5)
        clock.reset(notBefore: 10)
        XCTAssertEqual(try XCTUnwrap(clock.uptime(hostTimestamp: -100,
            hostNow: -99.9, receivedAt: 10.2)), 10.1, accuracy: 1e-9)
        clock.reset(notBefore: .nan)
        XCTAssertNil(clock.uptime(hostTimestamp: -99.8, hostNow: -99.8, receivedAt: 10.3))
    }

    func testTrackingThresholdUsesCaptureAgeRatherThanDeliveryAge() throws {
        for age in [0.0, 0.3, 0.599, 0.601, 2.99, 3.01] {
            var clock = CaptureTimestamp()
            clock.reset(notBefore: 0)
            let captured = try XCTUnwrap(clock.uptime(hostTimestamp: 10 - age,
                hostNow: 10, receivedAt: 10))
            XCTAssertEqual(captured, 10 - age, accuracy: 1e-9)
            XCTAssertEqual(TrackingFreshness.measurementIsFresh(capturedAt: captured, now: 10), age < 0.6)
            XCTAssertEqual(TrackingFreshness.identityIsFresh(capturedAt: captured, now: 10), age < 3)
        }
    }

    func testExactFreshnessAndFutureToleranceBoundaries() throws {
        var clock = CaptureTimestamp()
        clock.reset(notBefore: 0)
        let captured = try XCTUnwrap(clock.uptime(hostTimestamp: 0,
            hostNow: TrackingFreshness.measurementSeconds, receivedAt: TrackingFreshness.measurementSeconds))
        XCTAssertEqual(captured, 0)
        XCTAssertTrue(TrackingFreshness.measurementIsFresh(capturedAt: captured,
            now: TrackingFreshness.measurementSeconds))
        clock.reset(notBefore: 0)
        XCTAssertEqual(clock.uptime(hostTimestamp: CaptureTimestamp.futureTolerance,
            hostNow: 0, receivedAt: 1), 1)
        clock.reset(notBefore: 0)
        XCTAssertNil(clock.uptime(hostTimestamp: CaptureTimestamp.futureTolerance + 1e-6,
            hostNow: 0, receivedAt: 1))
    }
}
