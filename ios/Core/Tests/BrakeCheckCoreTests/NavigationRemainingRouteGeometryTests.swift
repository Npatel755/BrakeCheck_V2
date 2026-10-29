import XCTest
@testable import BrakeCheckCore

final class NavigationRemainingRouteGeometryTests: XCTestCase {
    private func point(_ east: Double, _ north: Double = 0) -> NavigationCoordinate {
        .init(latitude: north / 111_195.08, longitude: east / 111_195.08)
    }

    func testSparseTenKilometerRoadKeepsRemainingPartialSegment() throws {
        let road = [point(0), point(10_000)]
        let remaining = NavigationRemainingRouteGeometry.remainingRoute(road, origin: point(6_000))
        XCTAssertEqual(remaining.count, 2)
        XCTAssertEqual(try XCTUnwrap(remaining.first).distance(to: point(6_000)), 0, accuracy: 0.01)
        XCTAssertEqual(remaining.last, road.last)
        XCTAssertEqual(remaining[0].distance(to: remaining[1]), 4_000, accuracy: 0.1)
    }

    func testMeasuredProgressWinsOverNearbyEarlierLoopBranch() throws {
        let loop = [point(0), point(100), point(100, 100), point(0, 100), point(0)]
        let remaining = NavigationRemainingRouteGeometry.remainingRoute(loop,
            measuredProgressFraction: 0.875, origin: point(0))
        XCTAssertEqual(remaining.count, 2)
        XCTAssertEqual(try XCTUnwrap(remaining.first).distance(to: point(0, 50)), 0, accuracy: 0.01)
        XCTAssertEqual(remaining.last, loop.last)
    }

    func testPlanningAtAnOverlappingLoopOriginDoesNotSkipTheTrip() {
        let loop = [point(0), point(100), point(100, 100), point(0, 100), point(0)]
        XCTAssertEqual(NavigationRemainingRouteGeometry.remainingRoute(loop, origin: point(0)), loop)
    }

    func testNoGPSAndFarOrInvalidPlanningOriginsKeepFullRoute() {
        let road = [point(0), point(10_000)]
        XCTAssertEqual(NavigationRemainingRouteGeometry.remainingRoute(road), road)
        XCTAssertEqual(NavigationRemainingRouteGeometry.remainingRoute(road, origin: point(6_000, 300)), road)
        XCTAssertEqual(NavigationRemainingRouteGeometry.remainingRoute(road, origin: .init(latitude: .nan, longitude: 0)), road)
        for invalid in [Double.nan, .infinity, -0.1, 1.1] {
            XCTAssertEqual(NavigationRemainingRouteGeometry.remainingRoute(road,
                measuredProgressFraction: invalid, origin: point(6_000)), road)
        }
    }

    func testEndAndZeroProgressHaveExplicitGeometry() {
        let road = [point(0), point(0), point(10_000)]
        XCTAssertEqual(NavigationRemainingRouteGeometry.remainingRoute(road, measuredProgressFraction: 0), road)
        XCTAssertEqual(NavigationRemainingRouteGeometry.remainingRoute(road, measuredProgressFraction: 1), [point(10_000)])
        XCTAssertEqual(NavigationRemainingRouteGeometry.remainingRoute(road, origin: point(10_000)), [point(10_000)])
        XCTAssertEqual(NavigationRemainingRouteGeometry.remainingRoute([]), [])
        XCTAssertEqual(NavigationRemainingRouteGeometry.remainingRoute([point(1)]), [point(1)])
    }

    func testSearchCentersUsePhysicalDistanceAcrossUnevenVertices() {
        let sparse = [point(0), point(10_000)]
        let dense = [point(0), point(10), point(20), point(30), point(10_000)]
        let centers = NavigationRemainingRouteGeometry.searchCenters(sparse)
        let others = NavigationRemainingRouteGeometry.searchCenters(dense)
        XCTAssertEqual(centers.count, 3)
        for (index, meters) in [0.0, 4_500, 8_500].enumerated() {
            XCTAssertEqual(centers[index].distance(to: point(meters)), 0, accuracy: 0.01)
            XCTAssertEqual(centers[index].distance(to: others[index]), 0, accuracy: 0.01)
        }
    }

    func testPartialSegmentAndSearchCentersCrossDatelineByShortPath() throws {
        let road = [NavigationCoordinate(latitude: 0, longitude: 179.99), .init(latitude: 0, longitude: -179.99)]
        let remaining = NavigationRemainingRouteGeometry.remainingRoute(road, measuredProgressFraction: 0.5)
        XCTAssertEqual(abs(try XCTUnwrap(remaining.first).longitude), 180, accuracy: 0.000001)
        XCTAssertLessThan(remaining[0].distance(to: remaining[1]), 1_120)
        XCTAssertEqual(abs(NavigationRemainingRouteGeometry.searchCenters(road, fractions: [0.5])[0].longitude), 180, accuracy: 0.000001)
    }
}
