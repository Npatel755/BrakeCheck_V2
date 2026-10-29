import XCTest
@testable import BrakeCheckCore

final class GPSFilterTests: XCTestCase {
    func testActualStationaryFixIsZeroAndNeverDefaultHighwaySpeed() {
        var filter = GPSFilter()
        XCTAssertNil(filter.sample(at: 100))
        XCTAssertNil(filter.ingest(fix(speed: 0), wallNow: 1000, uptimeNow: 100))
        XCTAssertNil(filter.ingest(fix(speed: 0, time: 1001), wallNow: 1001, uptimeNow: 101))
        let reading = filter.ingest(fix(speed: 0, time: 1002), wallNow: 1002, uptimeNow: 102)
        XCTAssertEqual(reading?.metersPerSecond, 0)
        XCTAssertEqual(filter.sample(at: 103)?.metersPerSecond, 0)
        XCTAssertNil(filter.sample(at: 103.51))
    }

    func testInvalidOrPoorAccuracyCannotRenewOldSpeed() {
        for value in [fix(speed: -1), fix(speed: .nan), fix(speed: 25, accuracy: -1),
                      fix(speed: 25, accuracy: 8.01), fix(speed: 25, horizontal: 50.01)] {
            var filter = GPSFilter()
            _ = filter.ingest(fix(speed: 25, time: 999), wallNow: 999, uptimeNow: 99)
            XCTAssertNil(filter.ingest(value, wallNow: 1000, uptimeNow: 100))
            XCTAssertEqual(filter.sample(at: 100)?.measurementUptime, 99)
            XCTAssertNil(filter.sample(at: 100.51))
        }
    }

    func testCachedAndFutureFixesAreRejected() {
        var filter = GPSFilter()
        XCTAssertNil(filter.ingest(fix(speed: 25, time: 997), wallNow: 1000, uptimeNow: 100))
        XCTAssertNil(filter.ingest(fix(speed: 25, time: 1001), wallNow: 1000, uptimeNow: 100))
    }

    func testCameraSampleUsesPastGPSNotAReadingFromAfterTheFrame() throws {
        var filter = GPSFilter()
        _ = filter.ingest(fix(speed: 20), wallNow: 1000, uptimeNow: 100)
        let filtered = filter.ingest(fix(speed: 22, time: 1001), wallNow: 1001.1, uptimeNow: 101.1)
        XCTAssertEqual(filter.sample(at: 100.8)?.metersPerSecond, 20)
        XCTAssertEqual(filter.sample(at: 101.2)?.metersPerSecond, filtered?.metersPerSecond)
        XCTAssertEqual(try XCTUnwrap(filter.sample(at: 101.2)?.measurementUptime), 101, accuracy: 0.000001)
        XCTAssertNil(filter.sample(at: 99.9))
    }

    func testAccelerationUsesLocationTimesAndResetsAfterOutage() throws {
        var filter = GPSFilter()
        for i in 0...4 {
            let t = Double(i)
            _ = filter.ingest(fix(speed: 25 - 2 * t, time: 1000 + t), wallNow: 1000 + t + 0.3, uptimeNow: 100 + t + 0.3)
        }
        let acceleration = try XCTUnwrap(filter.sample(at: 104.4)?.acceleration)
        XCTAssertEqual(acceleration, -2, accuracy: 0.12)
        let returned = filter.ingest(fix(speed: 15, time: 1010), wallNow: 1010, uptimeNow: 110)
        XCTAssertNil(returned?.acceleration)
        filter.reset()
        XCTAssertNil(filter.sample(at: 110))
    }

    func testRepeatedLocationTimestampDoesNotDuplicateAccelerationSamples() {
        var filter = GPSFilter()
        _ = filter.ingest(fix(speed: 20), wallNow: 1000, uptimeNow: 100)
        _ = filter.ingest(fix(speed: 50), wallNow: 1000.5, uptimeNow: 100.5)
        XCTAssertEqual(filter.samples.count, 1)
        XCTAssertEqual(filter.sample(at: 100.5)?.metersPerSecond, 20)
    }

    func testSimulatedLocationCannotSupplyCollisionSpeed() {
        var filter = GPSFilter()
        let sample = filter.ingest(GPSFix(speed: 10, speedAccuracy: 1, horizontalAccuracy: 5,
                                          timestamp: 1000, simulated: true), wallNow: 1000, uptimeNow: 100)
        XCTAssertNil(sample)
        XCTAssertNil(filter.sample(at: 100))
        XCTAssertEqual(filter.status, "GPS fix invalid or too old")
    }

    private func fix(speed: Double, accuracy: Double = 0.5, horizontal: Double = 5,
                     time: Double = 1000) -> GPSFix {
        GPSFix(speed: speed, speedAccuracy: accuracy, horizontalAccuracy: horizontal, timestamp: time)
    }
}
