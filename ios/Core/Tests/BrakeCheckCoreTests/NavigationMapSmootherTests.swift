import XCTest
@testable import BrakeCheckCore

final class NavigationMapSmootherTests: XCTestCase {
    private func point(_ meters: Double) -> NavigationCoordinate {
        NavigationCoordinate(latitude: 0, longitude: meters / 111_195)
    }
    private func feed(_ smoother: inout NavigationMapSmoother, _ meters: Double, at time: Double,
                      accuracy: Double = 10, speed: Double = 0, current: Bool = true) -> Bool {
        smoother.ingest(point(meters), accuracy: accuracy, speed: speed, timestamp: time, now: time, isCurrent: current)
    }
    func testStationaryNoiseStaysWithinDeadband() {
        var smoother = NavigationMapSmoother()
        XCTAssertTrue(feed(&smoother, 0, at: 100))
        for index in 1...20 {
            XCTAssertTrue(feed(&smoother, index.isMultiple(of: 2) ? 4 : -4, at: 100 + Double(index)))
            _ = smoother.advance(seconds: 0.1)
        }
        XCTAssertEqual(smoother.coordinate!.longitude, 0, accuracy: 0.000001)
    }
    func testMovingFixInterpolatesWithoutTeleportingOrOvershooting() {
        var smoother = NavigationMapSmoother()
        _ = feed(&smoother, 0, at: 100, speed: 20)
        _ = feed(&smoother, 20, at: 101, speed: 20)
        let first = smoother.advance(seconds: 1 / 30.0)!.longitude * 111_195
        XCTAssertGreaterThan(first, 0)
        XCTAssertLessThan(first, 3)
        for _ in 0..<150 { _ = smoother.advance(seconds: 1 / 30.0) }
        let settled = smoother.coordinate!.longitude * 111_195
        XCTAssertEqual(settled, 20, accuracy: 0.01)
    }
    func testSingleLargeJumpExpandsCircleInsteadOfMovingDot() {
        var smoother = NavigationMapSmoother()
        _ = feed(&smoother, 0, at: 100)
        XCTAssertFalse(feed(&smoother, 200, at: 101))
        _ = smoother.advance(seconds: 0.1)
        XCTAssertEqual(smoother.coordinate!.longitude, 0)
        XCTAssertGreaterThan(smoother.uncertaintyRadius, 200)
        XCTAssertTrue(feed(&smoother, 2, at: 102))
        XCTAssertLessThan(smoother.uncertaintyRadius, 15)
    }
    func testUncertaintyIncludesVisualLag() {
        var smoother = NavigationMapSmoother()
        _ = feed(&smoother, 0, at: 100, speed: 20)
        _ = feed(&smoother, 20, at: 101, accuracy: 12, speed: 20)
        XCTAssertGreaterThan(smoother.uncertaintyRadius, 31)
    }
    func testStaleFreshFixAndInvalidFixesAreRejected() {
        var smoother = NavigationMapSmoother()
        XCTAssertFalse(smoother.ingest(point(0), accuracy: 10, speed: 0, timestamp: 100, now: 104, isCurrent: true))
        XCTAssertFalse(smoother.ingest(point(0), accuracy: -1, speed: 0, timestamp: 100, now: 100, isCurrent: true))
        XCTAssertFalse(smoother.ingest(NavigationCoordinate(latitude: .nan, longitude: 0), accuracy: 10, speed: 0, timestamp: 100, now: 100, isCurrent: true))
        XCTAssertNil(smoother.coordinate)
    }
    func testCachedInitialFixIsAllowedButNotExtrapolated() {
        var smoother = NavigationMapSmoother()
        XCTAssertTrue(smoother.ingest(point(35), accuracy: 50, speed: 30, timestamp: 100, now: 1000, isCurrent: false))
        for _ in 0..<300 { _ = smoother.advance(seconds: 0.1) }
        XCTAssertEqual(smoother.coordinate!.longitude, point(35).longitude, accuracy: 0.000001)
    }
    func testDuplicateAndUnorderedFixesDoNotMoveTarget() {
        var smoother = NavigationMapSmoother()
        _ = feed(&smoother, 0, at: 100)
        XCTAssertFalse(feed(&smoother, 10, at: 100))
        XCTAssertFalse(feed(&smoother, 10, at: 99))
        XCTAssertEqual(smoother.coordinate!.longitude, 0)
    }
    func testApproximateFixesStillMoveSmoothly() {
        var smoother = NavigationMapSmoother()
        _ = feed(&smoother, 0, at: 100, accuracy: 50, current: false)
        XCTAssertTrue(feed(&smoother, 20, at: 101, accuracy: 50, speed: 20, current: false))
        XCTAssertEqual(smoother.coordinate!.longitude, 0)
        let moved = smoother.advance(seconds: 0.1)!.longitude
        XCTAssertGreaterThan(moved, 0)
        XCTAssertLessThan(moved, point(20).longitude)
        XCTAssertGreaterThan(smoother.uncertaintyRadius, 50)
    }
    func testDatelineUsesShortestPath() {
        var smoother = NavigationMapSmoother()
        _ = smoother.ingest(NavigationCoordinate(latitude: 0, longitude: 179.9999), accuracy: 5, speed: 30, timestamp: 100, now: 100, isCurrent: true)
        XCTAssertTrue(smoother.ingest(NavigationCoordinate(latitude: 0, longitude: -179.9999), accuracy: 5, speed: 30, timestamp: 101, now: 101, isCurrent: true))
        XCTAssertGreaterThan(abs(smoother.advance(seconds: 0.1)!.longitude), 179)
    }

    func testMovingMarkerReachesLatestMeasuredFixWithoutTargetFilteringOrPrediction() {
        var smoother = NavigationMapSmoother()
        _ = feed(&smoother, 0, at: 100, speed: 20)
        _ = feed(&smoother, 20, at: 101, speed: 20)
        for _ in 0..<30 { _ = smoother.advance(seconds: 1 / 60.0) }
        XCTAssertEqual(smoother.coordinate!.longitude * 111_195, 20, accuracy: 3)
        for _ in 0..<90 { _ = smoother.advance(seconds: 1 / 60.0) }
        XCTAssertEqual(smoother.coordinate!.longitude * 111_195, 20, accuracy: 0.01)
        XCTAssertLessThanOrEqual(smoother.coordinate!.longitude * 111_195, 20.00001)
    }

    func testOneHertzHighwayFixesKeepSingleFilterLagBoundedAtSixtyHertz() {
        var smoother = NavigationMapSmoother()
        var latestMeasurement = 0.0
        for frame in 0...7200 {
            let time = Double(frame) / 60
            if frame.isMultiple(of: 60) {
                latestMeasurement = time * 20
                XCTAssertTrue(feed(&smoother, latestMeasurement, at: 100 + time, speed: 20))
            }
            let displayed = smoother.advance(seconds: 1 / 60.0)!.longitude * 111_195
            XCTAssertLessThanOrEqual(displayed, latestMeasurement + 0.0001)
            XCTAssertLessThan(latestMeasurement - displayed, 20)
            if frame % 60 >= 30 { XCTAssertLessThan(latestMeasurement - displayed, 3) }
        }
    }

    func testEstimateHandoverPreservesPositionAndAcceptsEarlierMeasuredCaptureTime() {
        var smoother = NavigationMapSmoother()
        _ = feed(&smoother, 0, at: 100, speed: 20)
        _ = feed(&smoother, 20, at: 101, speed: 20)
        for _ in 0..<30 { _ = smoother.advance(seconds: 1 / 60.0) }
        let beforeEstimate = smoother.coordinate
        XCTAssertTrue(smoother.ingest(point(21), accuracy: 15, speed: -1, timestamp: 102.05,
            now: 102.05, isCurrent: false, isEstimated: true))
        XCTAssertEqual(smoother.coordinate, beforeEstimate)
        let beforeRecovery = smoother.advance(seconds: 1 / 60.0)
        XCTAssertTrue(smoother.ingest(point(20.5), accuracy: 10, speed: 20, timestamp: 102,
            now: 102.1, isCurrent: true))
        XCTAssertEqual(smoother.coordinate, beforeRecovery)
        for _ in 0..<120 { _ = smoother.advance(seconds: 1 / 60.0) }
        XCTAssertEqual(smoother.coordinate!.longitude * 111_195, 20.5, accuracy: 0.01)
        XCTAssertFalse(feed(&smoother, 50, at: 101.5, speed: 20))
    }

    func testExpiredEstimateReturnsToCachedFixAndCannotContinueMoving() {
        var smoother = NavigationMapSmoother()
        _ = feed(&smoother, 0, at: 100, speed: 20)
        _ = feed(&smoother, 20, at: 101, speed: 20)
        XCTAssertTrue(smoother.ingest(point(60), accuracy: 35, speed: -1, timestamp: 104,
            now: 104, isCurrent: false, isEstimated: true))
        for _ in 0..<30 { _ = smoother.advance(seconds: 1 / 60.0) }
        XCTAssertTrue(smoother.ingest(point(20), accuracy: 10, speed: 20, timestamp: 101,
            now: 106, isCurrent: false))
        for _ in 0..<180 { _ = smoother.advance(seconds: 1 / 60.0) }
        XCTAssertEqual(smoother.coordinate!.longitude * 111_195, 20, accuracy: 0.0001)
        XCTAssertTrue(feed(&smoother, 100, at: 107, speed: 20))
        XCTAssertEqual(smoother.coordinate!.longitude * 111_195, 100, accuracy: 0.0001)
    }

    func testExpiredEstimateInputAndHandoverTeleportAreRejected() {
        var smoother = NavigationMapSmoother()
        _ = feed(&smoother, 0, at: 100, speed: 20)
        XCTAssertFalse(smoother.ingest(point(20), accuracy: 10, speed: -1, timestamp: 101,
            now: 104, isCurrent: false, isEstimated: true))
        XCTAssertTrue(smoother.ingest(point(20), accuracy: 10, speed: -1, timestamp: 101,
            now: 101, isCurrent: false, isEstimated: true))
        XCTAssertFalse(feed(&smoother, 1000, at: 101.1, speed: 20))
        XCTAssertTrue(feed(&smoother, 22, at: 101.2, speed: 20))
    }

    func testHeadingCrossesNorthByShortestPathAndIgnoresInvalidTime() {
        let clockwise = NavigationMapSmoother.interpolatedHeading(from: 359, toward: 1, seconds: 0.1)
        XCTAssertGreaterThan(clockwise, 359)
        XCTAssertLessThan(clockwise, 360)
        let counterclockwise = NavigationMapSmoother.interpolatedHeading(from: 1, toward: 359, seconds: 0.1)
        XCTAssertGreaterThan(counterclockwise, 0)
        XCTAssertLessThan(counterclockwise, 1)
        XCTAssertEqual(NavigationMapSmoother.interpolatedHeading(from: 45, toward: 90, seconds: .nan), 45)
        XCTAssertEqual(NavigationMapSmoother.interpolatedHeading(from: 45, toward: 90, seconds: -1), 45)
    }
}
