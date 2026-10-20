import XCTest
@testable import BrakeCheckCore

final class NavigationEstimateRenderingTests: XCTestCase {
    func testSuccessiveServiceEstimatesInterpolateWithoutTeleportOrExtrapolation() throws {
        var smoother = NavigationMapSmoother()
        let origin = NavigationCoordinate(latitude: 33, longitude: -84)
        let next = NavigationCoordinate(latitude: 33.00005, longitude: -84)
        let after = NavigationCoordinate(latitude: 33.00010, longitude: -84)
        XCTAssertTrue(smoother.ingest(origin, accuracy: 5, speed: -1,
            timestamp: 10, now: 10, isCurrent: false, isEstimated: true))
        XCTAssertTrue(smoother.ingest(next, accuracy: 7, speed: -1,
            timestamp: 11, now: 11, isCurrent: false, isEstimated: true))
        XCTAssertEqual(smoother.coordinate, origin)
        let midway = try XCTUnwrap(smoother.advance(seconds: 1.0 / 60))
        XCTAssertGreaterThan(midway.latitude, origin.latitude)
        XCTAssertLessThan(midway.latitude, next.latitude)
        XCTAssertTrue(smoother.ingest(after, accuracy: 9, speed: -1,
            timestamp: 12, now: 12, isCurrent: false, isEstimated: true))
        XCTAssertEqual(smoother.coordinate, midway)
        XCTAssertFalse(smoother.ingest(after, accuracy: 9, speed: -1,
            timestamp: 12, now: 12, isCurrent: false, isEstimated: true))
        for _ in 0..<600 { _ = smoother.advance(seconds: 1.0 / 60) }
        let final = try XCTUnwrap(smoother.coordinate)
        XCTAssertLessThanOrEqual(final.latitude, after.latitude)
        XCTAssertEqual(final.latitude, after.latitude, accuracy: 0.000000001)
        XCTAssertGreaterThanOrEqual(smoother.uncertaintyRadius, 9)
        // The representable replaces the smoother on estimated/measured mode transitions.
        smoother = NavigationMapSmoother()
        XCTAssertTrue(smoother.ingest(origin, accuracy: 5, speed: 0,
            timestamp: 10, now: 20, isCurrent: false))
        XCTAssertEqual(smoother.coordinate, origin)
    }
}
