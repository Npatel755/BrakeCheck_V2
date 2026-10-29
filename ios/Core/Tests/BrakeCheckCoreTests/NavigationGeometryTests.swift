import XCTest
@testable import BrakeCheckCore

final class NavigationGeometryTests: XCTestCase {
    private func point(_ east: Double, _ north: Double = 0) -> NavigationCoordinate {
        let meters = 6_371_008.8 * Double.pi / 180
        return .init(latitude: north / meters, longitude: east / meters)
    }
    func testDenseLongRouteMatchesExhaustiveN60MatcherThroughOffRouteRecovery() throws {
        let dense = (0...20_000).map { point(Double($0) * 5 - 50_000) }
        var indexed = NavigationProgressTracker(coordinates: dense, routeDistance: 100_000, stepDistances: [50_000, 50_000])
        var reference = ExhaustiveNavigationProgressTracker(coordinates: dense,
            routeDistance: 100_000, stepDistances: [50_000, 50_000])
        let fixes: [(Double, Double)] = [(0, 5), (15, 7), (30, 80), (45, 80), (60, 80), (75, 3), (90, 3)]
        for (time, fix) in fixes.enumerated() {
            let coordinate = point(fix.0, fix.1), timestamp = Double(time)
            let first = try XCTUnwrap(indexed.update(coordinate: coordinate, horizontalAccuracy: 5,
                timestamp: timestamp, now: timestamp, courseDegrees: 90, courseAccuracy: 5, speed: 15))
            let second = try XCTUnwrap(reference.update(coordinate: coordinate, horizontalAccuracy: 5,
                timestamp: timestamp, now: timestamp, courseDegrees: 90, courseAccuracy: 5, speed: 15))
            XCTAssertEqual(first.progressMeters, second.progressMeters, accuracy: 0.001)
            XCTAssertEqual(first.distanceFromRoute, second.distanceFromRoute, accuracy: 0.001)
            XCTAssertEqual(first.positionReliable, second.positionReliable)
            XCTAssertEqual(first.offRoute, second.offRoute)
            XCTAssertEqual(first.mapMatchAmbiguous, second.mapMatchAmbiguous)
            XCTAssertEqual(first.arrived, second.arrived)
        }
    }
    func testSpatialCandidatesRetainParallelRoadAmbiguityAmongDistantRoads() throws {
        var nodes = [OfflineRoadNode](), edges = [OfflineRoadEdge]()
        for index in 0..<1_000 {
            let north = index < 2 ? Double(index) * 6 : Double(index) * 200
            let a = point(0, north), b = point(100, north)
            nodes += [.init(id: "a\(index)", coordinate: a), .init(id: "b\(index)", coordinate: b)]
            edges.append(.init(id: "road\(index)", from: "a\(index)", to: "b\(index)", coordinates: [a, b]))
        }
        let router = try OfflineNavigationRouter(graph: .init(name: "Roads", attribution: "Test", nodes: nodes, edges: edges))
        let snap = try router.snap(point(50, 3), bearing: 90)
        XCTAssertLessThan(snap.confidence, 0.65)
        XCTAssertThrowsError(try router.route(from: point(20, 3), to: point(80), initialBearing: 90)) {
            XCTAssertEqual($0 as? OfflineNavigationError, .ambiguousRoadPosition)
        }
    }
    func testSpatialIndexIncludesDateLineAndPolarNeighbors() {
        let dateLine = NavigationSpatialIndex(bounds: [.init(coordinates: [
            .init(latitude: 0, longitude: 179.99), .init(latitude: 0, longitude: -179.99)])])
        XCTAssertEqual(dateLine.candidates(near: .init(latitude: 0, longitude: 179.999), radius: 30), [0])
        let polar = NavigationSpatialIndex(bounds: [.init(coordinates: [
            .init(latitude: 89.9999, longitude: -20), .init(latitude: 89.9999, longitude: 20)])])
        XCTAssertEqual(polar.candidates(near: .init(latitude: 89.9999, longitude: 60), radius: 100), [0])
    }
    func testPreparedProjectionKeepsFreshnessOcclusionAndInvalidRouteGuards() {
        let route = (0...10_000).map { point(0, Double($0)) }
        let geometry = CameraRouteGeometry(route: route)
        let lane = LanePath(bands: (0..<25).map { index in
            let y = 0.1 + Double(index) * 0.02, half = 0.45 * (0.7 - y)
            return LaneBand(y: y, left: 0.5 - half, right: 0.5 + half)
        }, confidence: 0.95, capturedAt: 10, source: .neural)
        let road = RoadRegion(bands: (0..<30).map { LaneBand(y: 0.05 + Double($0) * 0.02, left: 0.05, right: 0.95) },
            confidence: 0.95, capturedAt: 10)
        func project(_ prepared: CameraRouteGeometry, at time: Double, boxes: [CGRect] = []) -> [[LaneBand]] {
            CameraRouteProjection.project(geometry: prepared, progressFraction: 0, courseDegrees: 0,
                horizontalFOV: 60, aspect: 16.0 / 9, calibration: .init(), lane: lane, road: road,
                excluding: boxes, at: time)
        }
        XCTAssertFalse(project(geometry, at: 10).isEmpty)
        XCTAssertTrue(project(geometry, at: 11).isEmpty)
        let occluded = project(geometry, at: 10, boxes: [CGRect(x: 0.35, y: 0.35, width: 0.3, height: 0.1)])
        XCTAssertTrue(occluded.flatMap { $0 }.allSatisfy { $0.y < 0.34 || $0.y > 0.46 })
        XCTAssertTrue(project(CameraRouteGeometry(route: [.init(latitude: .nan, longitude: 0), point(0)]), at: 10).isEmpty)
        let raw = CameraRouteProjection.project(route: route, progressFraction: 0, courseDegrees: 0,
            horizontalFOV: 60, aspect: 16.0 / 9, calibration: .init(), lane: lane, road: road, at: 10)
        let prepared = project(geometry, at: 10)
        XCTAssertEqual(raw.count, prepared.count)
        for (a, b) in zip(raw.flatMap({ $0 }), prepared.flatMap({ $0 })) {
            XCTAssertEqual(a.y, b.y); XCTAssertEqual(a.left, b.left); XCTAssertEqual(a.right, b.right)
        }
    }
}
