import XCTest
@testable import BrakeCheckCore

final class PerformanceStatisticsTests: XCTestCase {
    func testHistogramRejectsInvalidSamplesAndHasBoundedStorage() throws {
        var stats = PerformanceStatistics()
        for value in [Double.nan, .infinity, -.infinity, -0.01] { stats.record(seconds: value) }
        XCTAssertEqual(stats.count, 0)
        XCTAssertNil(stats.percentileUpperBound(0.95))
        for _ in 0..<100_000 { stats.record(seconds: 0.010) }
        XCTAssertEqual(stats.count, 100_000)
        XCTAssertEqual(stats.buckets.count, PerformanceStatistics.boundaries.count + 1)
        XCTAssertEqual(stats.buckets.reduce(0, +), stats.count)
        XCTAssertEqual(stats.meanSeconds, 0.010, accuracy: 0.000001)
        XCTAssertEqual(stats.percentileUpperBound(0.95), 0.012)
        XCTAssertNil(stats.percentileUpperBound(.nan))
        XCTAssertNil(stats.percentileUpperBound(1.01))
        let restored = try JSONDecoder().decode(PerformanceStatistics.self, from: JSONEncoder().encode(stats))
        XCTAssertEqual(restored.count, stats.count)
        XCTAssertEqual(restored.maximumSeconds, stats.maximumSeconds)
    }
    func testLongTailIsReportedWithoutUnboundedSampleHistory() {
        var stats = PerformanceStatistics()
        for _ in 0..<95 { stats.record(seconds: 0.004) }
        for _ in 0..<4 { stats.record(seconds: 0.150) }
        stats.record(seconds: 15)
        XCTAssertEqual(stats.percentileUpperBound(0.50), 0.004)
        XCTAssertEqual(stats.percentileUpperBound(0.95), 0.004)
        XCTAssertEqual(stats.percentileUpperBound(0.99), 0.150)
        XCTAssertEqual(stats.percentileUpperBound(1), 15)
    }
    func testStallsAtDifferentRefreshRatesAndForegroundResets() {
        for hz in [30.0, 60.0, 120.0] {
            var stats = DisplayPacingStatistics()
            let step = 1 / hz
            stats.record(timestamp: 10, targetInterval: step)
            stats.record(timestamp: 10 + step, targetInterval: step)
            stats.record(timestamp: 10 + 4 * step, targetInterval: step)
            XCTAssertEqual(stats.delayedCallbacks, 1)
            XCTAssertEqual(stats.estimatedMissedRefreshes, 2)
            stats.resetContinuity()
            stats.record(timestamp: 500, targetInterval: step)
            XCTAssertEqual(stats.estimatedMissedRefreshes, 2)
        }
    }
    func testRefreshChangesAndOutOfOrderCallbacksDoNotInventHitches() {
        var stats = DisplayPacingStatistics()
        stats.record(timestamp: 10, targetInterval: 1 / 120)
        stats.record(timestamp: 10.1, targetInterval: 1 / 30)
        stats.record(timestamp: 9, targetInterval: 1 / 30)
        stats.record(timestamp: .nan, targetInterval: 1 / 30)
        XCTAssertEqual(stats.delayedCallbacks, 0)
        XCTAssertEqual(stats.intervals.count, 0)
        XCTAssertEqual(stats.callbacks, 3)
    }
}
