import XCTest
@testable import BrakeCheckCore

final class GPSObservationOrderTests: XCTestCase {
    func testFutureAndInvalidFixesCannotPoisonRecovery() {
        var order = GPSObservationOrder()
        XCTAssertTrue(order.accept(timestamp: 100, now: 100))
        for value in [Double.nan, .infinity, -.infinity, 10000] {
            XCTAssertFalse(order.accept(timestamp: value, now: 101))
        }
        XCTAssertTrue(order.accept(timestamp: 101, now: 101))
        XCTAssertTrue(order.accept(timestamp: 102, now: 102))
    }

    func testDuplicateAndUnorderedFixesAreNotNewMeasurements() {
        var order = GPSObservationOrder()
        XCTAssertTrue(order.accept(timestamp: 99, now: 100))
        XCTAssertFalse(order.accept(timestamp: 99, now: 100))
        XCTAssertFalse(order.accept(timestamp: 98, now: 100))
        XCTAssertTrue(order.accept(timestamp: 100, now: 100))
        order.reset()
        XCTAssertTrue(order.accept(timestamp: 99, now: 100))
    }

    func testOrderedBatchRetainsBothCameraAlignedSpeedMeasurements() throws {
        var order = GPSObservationOrder()
        var filter = GPSFilter()
        var history = FrameAlignedSpeedHistory()
        for (time, speed) in [(99.0, 10.0), (100.0, 12.0), (10000.0, 70.0)] {
            guard order.accept(timestamp: time, now: 100) else { continue }
            if let reading = filter.ingest(GPSFix(speed: speed, speedAccuracy: 0.5,
                horizontalAccuracy: 5, timestamp: time), wallNow: 100, uptimeNow: 200) {
                history.append(reading)
            }
        }
        XCTAssertEqual(history.reading(at: 199.4)?.metersPerSecond, 10)
        XCTAssertGreaterThan(try XCTUnwrap(history.reading(at: 200)?.metersPerSecond), 10)
        XCTAssertTrue(order.accept(timestamp: 101, now: 101))
    }
}
