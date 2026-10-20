import XCTest
@testable import BrakeCheckCore

final class OfflineNavigationTests: XCTestCase {
    private func point(_ x: Double, _ y: Double = 0) -> NavigationCoordinate {
        NavigationCoordinate(latitude: y / 111_195.08, longitude: x / 111_195.08)
    }
    private func graph(_ nodes: [(String, Double, Double)], _ roads: [(String, String, String)],
                       restrictions: [OfflineTurnRestriction] = []) -> OfflineRoadGraph {
        let nodes = nodes.map { OfflineRoadNode(id: $0.0, coordinate: point($0.1, $0.2)) }
        let map = Dictionary(uniqueKeysWithValues: nodes.map { ($0.id, $0.coordinate) })
        return OfflineRoadGraph(name: "Test graph", attribution: "Test data", nodes: nodes,
            edges: roads.map { OfflineRoadEdge(id: $0.0, from: $0.1, to: $0.2, name: $0.0,
                coordinates: [map[$0.1]!, map[$0.2]!], speedMetersPerSecond: 10) }, turnRestrictions: restrictions)
    }
    func testMidEdgeRoutingUsesPartialGeometryAndPreservesOneWay() throws {
        let graph = graph([("a",0,0),("b",100,0)], [("ab","a","b")])
        let router = try OfflineNavigationRouter(graph: graph)
        let route = try router.route(from: point(20, 3), to: point(80, 2), initialBearing: 90)
        XCTAssertEqual(route.distance, 60, accuracy: 0.02)
        XCTAssertEqual(route.expectedTravelTime, 6, accuracy: 0.01)
        XCTAssertEqual(route.coordinates.first!.distance(to: point(20)), 0, accuracy: 0.01)
        XCTAssertEqual(route.coordinates.last!.distance(to: point(80)), 0, accuracy: 0.01)
        XCTAssertThrowsError(try router.route(from: point(80), to: point(20))) {
            XCTAssertEqual($0 as? OfflineNavigationError, .noLegalRoute)
        }
    }
    func testDisconnectedRoadCannotBeReplacedWithDistantConnectedCarriageway() throws {
        let graph = graph([("a",0,0),("b",100,0),("c",0,12),("d",100,12),("e",100,100)],
                          [("ab","a","b"),("cd","c","d"),("de","d","e")])
        let router = try OfflineNavigationRouter(graph: graph)
        XCTAssertThrowsError(try router.route(from: point(20, 1), to: point(100, 90), initialBearing: 90)) {
            XCTAssertEqual($0 as? OfflineNavigationError, .noLegalRoute)
        }
    }
    func testHeadingKeepsFixOnCorrectDirectionalCarriageway() throws {
        let graph = graph([("a",0,0),("b",100,0),("c",0,6),("d",100,6)],
                          [("east","a","b"),("west","d","c")])
        let router = try OfflineNavigationRouter(graph: graph)
        XCTAssertEqual(try router.snap(point(50,4), bearing: 90).edgeID, "east")
        XCTAssertEqual(try router.snap(point(50,2), bearing: 270).edgeID, "west")
        XCTAssertThrowsError(try router.route(from: point(20), to: point(80,3), initialBearing: 90)) {
            XCTAssertEqual($0 as? OfflineNavigationError, .ambiguousRoadPosition)
        }
    }
    func testProhibitedTurnRequiresLegalDetourAndOnlyRestrictionIsEnforced() throws {
        let nodes: [(String,Double,Double)] = [("a",0,0),("b",100,0),("c",100,100),("d",200,0),("e",200,100)]
        let roads = [("ab","a","b"),("bc","b","c"),("bd","b","d"),("de","d","e"),("ec","e","c")]
        let restriction = OfflineTurnRestriction(fromEdgeID: "ab", toEdgeID: "bc", viaNodeID: "b")
        let router = try OfflineNavigationRouter(graph: graph(nodes, roads, restrictions: [restriction]))
        let route = try router.route(from: point(20), to: point(100,100), initialBearing: 90)
        XCTAssertGreaterThan(route.distance, 350)
        XCTAssertTrue(route.steps.contains { $0.instruction.contains("bd") })
        let only = OfflineTurnRestriction(fromEdgeID: "ab", toEdgeID: "bd", viaNodeID: "b", kind: .only)
        let onlyRouter = try OfflineNavigationRouter(graph: graph(nodes, roads, restrictions: [only]))
        XCTAssertGreaterThan(try onlyRouter.route(from: point(20), to: point(100,100), initialBearing: 90).distance, 350)
    }
    func testInitialBearingCannotResolveSameDirectionParallelRoadsOrBypassAmbiguity() throws {
        let router = try OfflineNavigationRouter(graph: graph(
            [("a",0,0),("b",100,0),("c",0,6),("d",100,6)], [("first","a","b"),("second","c","d")]))
        for bearing in [90.0, Double.nan, .infinity, -1, 360] {
            XCTAssertThrowsError(try router.route(from: point(20,3), to: point(80), initialBearing: bearing)) {
                XCTAssertEqual($0 as? OfflineNavigationError, .ambiguousRoadPosition)
            }
        }
    }
    func testAvoidanceAndDrivingAccessAreHardConstraints() throws {
        var graph = graph([("a",0,0),("b",100,0)], [("ab","a","b")])
        graph.edges[0].isToll = true
        XCTAssertThrowsError(try OfflineNavigationRouter(graph: graph).route(from: point(20), to: point(80),
            preferences: .init(avoidTolls: true)))
        graph.edges[0].isToll = false; graph.edges[0].isMotorway = true
        XCTAssertThrowsError(try OfflineNavigationRouter(graph: graph).route(from: point(20), to: point(80),
            preferences: .init(avoidMotorways: true)))
        graph.edges[0].isMotorway = false; graph.edges[0].allowsDriving = false
        XCTAssertThrowsError(try OfflineNavigationRouter(graph: graph).route(from: point(20), to: point(80)))
    }
    func testGraphRoundTripRejectsDanglingAndDuplicateRoadData() throws {
        let valid = graph([("a",0,0),("b",100,0)], [("ab","a","b")])
        XCTAssertEqual(try OfflineRoadGraph.decode(JSONEncoder().encode(valid)), valid)
        var duplicate = valid; duplicate.nodes.append(valid.nodes[0])
        XCTAssertThrowsError(try OfflineNavigationRouter(graph: duplicate))
        var dangling = valid; dangling.edges[0].to = "missing"
        XCTAssertThrowsError(try OfflineNavigationRouter(graph: dangling))
        var brokenGeometry = valid; brokenGeometry.edges[0].coordinates[0] = point(50)
        XCTAssertThrowsError(try OfflineNavigationRouter(graph: brokenGeometry))
        XCTAssertThrowsError(try OfflineNavigationRouter(graph: valid).route(from: point(20), to: point(100_000))) {
            XCTAssertEqual($0 as? OfflineNavigationError, .outsideCoverage)
        }
    }
    func testSavedRouteRoundTripAndOfflineProgressNeedNoProviderObject() throws {
        let router = try OfflineNavigationRouter(graph: graph([("a",0,0),("b",100,0)], [("ab","a","b")]))
        let route = try router.route(from: point(0), to: point(100))
        let restored = try XCTUnwrap(NavigationRouteArchive.decode(NavigationRouteArchive.encode([route])).first)
        XCTAssertEqual(restored, route)
        var tracker = restored.progressTracker()
        let fix = try XCTUnwrap(tracker.update(coordinate: point(40, 3), horizontalAccuracy: 5, timestamp: 0, now: 0))
        XCTAssertEqual(fix.progressMeters, 40, accuracy: 0.02)
        XCTAssertEqual(fix.snappedCoordinate!.distance(to: point(40)), 0, accuracy: 0.02)
        XCTAssertEqual(fix.snappedBearing!, 90, accuracy: 0.01)
        XCTAssertGreaterThan(fix.mapMatchConfidence, 0.75)
        XCTAssertTrue(fix.positionReliable)
    }
    func testOfflineMultiStopKeepsRequestedOrderAndDwellTime() throws {
        let router = try OfflineNavigationRouter(graph: graph([("a",0,0),("b",100,0),("c",200,0)],
            [("ab","a","b"),("bc","b","c")]))
        let stops = [NavigationRouteStop(title: "Pickup", coordinate: point(80), dwellTime: 30),
                     NavigationRouteStop(title: "Finish", coordinate: point(180))]
        let route = try router.route(from: point(20), stops: stops)
        XCTAssertEqual(route.distance, 160, accuracy: 0.02)
        XCTAssertEqual(route.expectedTravelTime, 46, accuracy: 0.02)
        XCTAssertEqual(route.stops.map(\.id), stops.map(\.id))
        XCTAssertEqual(route.stops[0].routeDistanceFromStart!, 60, accuracy: 0.02)
        XCTAssertEqual(route.stops[1].routeDistanceFromStart!, 160, accuracy: 0.02)
    }
    func testArchivesRejectUnreachableOrReorderedStopMilestonesAndOversizedOutput() throws {
        let router = try OfflineNavigationRouter(graph: graph([("a",0,0),("b",100,0)], [("ab","a","b")]))
        var route = try router.route(from: point(0), to: point(100))
        route.stops = [.init(title: "Beyond route", coordinate: point(100), routeDistanceFromStart: 200)]
        XCTAssertFalse(route.isValid)
        XCTAssertThrowsError(try NavigationRouteArchive.encode([route]))
        route.stops = [.init(title: "Later", coordinate: point(80), routeDistanceFromStart: 80),
                       .init(title: "Earlier", coordinate: point(20), routeDistanceFromStart: 20)]
        XCTAssertFalse(route.isValid)
        route.stops = []
        route.title = String(repeating: "x", count: 32_000_000)
        XCTAssertThrowsError(try NavigationRouteArchive.encode([route])) {
            guard case NavigationArchiveError.tooLarge = $0 else { return XCTFail("Expected oversized archive rejection") }
        }
    }
    func testOffsetPOIStopsPersistActualLegEndpointsAndProgressMilestones() throws {
        let router = try OfflineNavigationRouter(graph: graph([("a",0,0),("b",100,0),("c",200,0)],
            [("ab","a","b"),("bc","b","c")]))
        let stops = [NavigationRouteStop(title: "Building entrance", coordinate: point(80,25), dwellTime: 40),
                     NavigationRouteStop(title: "Destination", coordinate: point(180,20))]
        let route = try router.route(from: point(20), stops: stops)
        XCTAssertEqual(route.stops.map(\.id), stops.map(\.id))
        XCTAssertEqual(route.stops.map(\.title), stops.map(\.title))
        XCTAssertEqual(route.stops[0].dwellTime, 40)
        XCTAssertEqual(route.stops[0].coordinate.distance(to: point(80)), 0, accuracy: 0.02)
        XCTAssertEqual(route.stops[1].coordinate.distance(to: point(180)), 0, accuracy: 0.02)
        XCTAssertEqual(route.stops[0].coordinate.distance(to: stops[0].coordinate), 25, accuracy: 0.02)
        XCTAssertEqual(route.stops[0].routeDistanceFromStart!, 60, accuracy: 0.02)
        XCTAssertEqual(route.stops[1].routeDistanceFromStart!, route.distance, accuracy: 0.02)
        let restored = try XCTUnwrap(NavigationRouteArchive.decode(NavigationRouteArchive.encode([route])).first)
        XCTAssertEqual(restored.stops, route.stops)
        XCTAssertEqual(restored.historyKey, route.historyKey)
    }
    func testStoppingAtIntersectionDoesNotEraseTurnRestriction() throws {
        let graph = graph([("a",0,0),("b",100,0),("c",100,100)], [("ab","a","b"),("bc","b","c")],
            restrictions: [.init(fromEdgeID: "ab", toEdgeID: "bc", viaNodeID: "b")])
        let router = try OfflineNavigationRouter(graph: graph)
        XCTAssertThrowsError(try router.route(from: point(20), stops: [
            .init(title: "At intersection", coordinate: point(100)), .init(title: "Finish", coordinate: point(100,80))],
            initialBearing: 90)) {
            XCTAssertEqual($0 as? OfflineNavigationError, .noLegalRoute)
        }
    }
    func testSameRoadContinuationPreservesTollNoticeBoundary() throws {
        var graph = graph([("a",0,0),("b",100,0),("c",200,0)], [("ab","a","b"),("bc","b","c")])
        graph.edges[0].name = "Main Street"; graph.edges[1].name = "Main Street"
        graph.edges[1].isToll = true
        let route = try OfflineNavigationRouter(graph:graph).route(from:point(20),to:point(180),initialBearing:90)
        XCTAssertEqual(route.steps.count,2)
        XCTAssertNil(route.steps[0].notice)
        XCTAssertEqual(route.steps[1].notice,"Toll road")
        XCTAssertEqual(route.distance,160,accuracy:0.02)
        XCTAssertEqual(route.expectedTravelTime,16,accuracy:0.02)
    }
}
