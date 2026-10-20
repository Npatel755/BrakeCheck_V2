import XCTest
@testable import BrakeCheckCore

final class OSMRoadGraphImporterTests: XCTestCase {
    private func node(_ id: Int, _ x: Double, _ y: Double = 0, tags: [String: String] = [:]) -> [String: Any] {
        ["type": "node", "id": id, "lat": y / 111_195.08, "lon": x / 111_195.08, "tags": tags]
    }
    private func way(_ id: Int, _ nodes: [Int], tags: [String: String] = [:]) -> [String: Any] {
        ["type": "way", "id": id, "nodes": nodes, "tags": ["highway": "residential", "name": "Road \(id)"].merging(tags) { _, new in new }]
    }
    private func relation(_ id: Int, from: Int, to: Int, via: Int, viaType: String = "node",
                          tags: [String: String] = [:]) -> [String: Any] {
        ["type": "relation", "id": id, "tags": ["type": "restriction", "restriction": "no_left_turn"].merging(tags) { _, new in new },
         "members": [["type": "way", "ref": from, "role": "from"], ["type": "way", "ref": to, "role": "to"],
                     ["type": viaType, "ref": via, "role": "via"]]]
    }
    private func imported(_ elements: [[String: Any]]) throws -> OSMRoadGraphImportResult {
        try OSMRoadGraphImporter.importData(JSONSerialization.data(withJSONObject: ["elements": elements]), name: "Fixture")
    }
    func testOneWayReverseRoundaboutAndMPHSpeeds() throws {
        let graph = try imported([node(1,0),node(2,100),node(3,200),node(4,300),
            way(10,[1,2],tags:["oneway":"yes","maxspeed":"30 mph"]),
            way(11,[2,3],tags:["oneway":"-1"]),
            way(12,[3,4],tags:["junction":"roundabout"]) ]).graph
        XCTAssertEqual(graph.edges.count, 3)
        let first = try XCTUnwrap(graph.edges.first { $0.id == "way:10:0:f" })
        XCTAssertEqual(first.speedMetersPerSecond, 30 * 0.44704 * 0.8, accuracy: 0.001)
        XCTAssertNotNil(graph.edges.first { $0.id == "way:11:0:r" && $0.from == "node:3" })
        XCTAssertFalse(graph.edges.contains { $0.id == "way:12:0:r" })
    }
    func testPrivateConditionalReversibleAndBarrierRoadsAreExcluded() throws {
        let result = try imported([node(1,0),node(2,100),node(3,200),node(4,300),node(5,400),
            node(6,500,tags:["barrier":"gate"]),node(7,600),
            way(10,[1,2]),way(11,[2,3],tags:["access":"private"]),
            way(12,[3,4],tags:["oneway:conditional":"yes @ (08:00-10:00)"]),
            way(13,[4,5],tags:["oneway":"reversible"]),way(14,[5,6,7])])
        XCTAssertEqual(result.graph.edges.count, 2)
        XCTAssertEqual(result.omittedWayCount, 4)
        XCTAssertGreaterThan(result.warnings.count, 2)
    }
    func testViaNodeTurnRestrictionsBecomeDirectedEdgeRules() throws {
        let graph = try imported([node(1,0),node(2,100),node(3,100,100),node(4,200),
            way(10,[1,2]),way(11,[2,3]),way(12,[2,4]),relation(20,from:10,to:11,via:2)]).graph
        XCTAssertTrue(graph.turnRestrictions.contains { $0.fromEdgeID == "way:10:0:f" && $0.toEdgeID == "way:11:0:f" })
        let router = try OfflineNavigationRouter(graph: graph)
        let start = NavigationCoordinate(latitude: 0, longitude: 20 / 111_195.08)
        let end = NavigationCoordinate(latitude: 80 / 111_195.08, longitude: 100 / 111_195.08)
        // A legal detour can reverse at the dead-end; it must not take the prohibited direct turn.
        let route = try router.route(from: start, to: end, initialBearing: 90)
        XCTAssertGreaterThan(route.distance, 300)
    }
    func testUnsupportedViaWayAndConditionalRestrictionsRemoveAffectedRoads() throws {
        let result = try imported([node(1,0),node(2,100),node(3,200),node(4,300),node(5,400),
            way(10,[1,2]),way(11,[2,3]),way(12,[3,4]),way(13,[4,5]),
            relation(20,from:10,to:12,via:11,viaType:"way")])
        XCTAssertTrue(result.graph.edges.allSatisfy { $0.id.hasPrefix("way:13:") })
        XCTAssertEqual(result.omittedWayCount, 3)
        let conditional = try imported([node(1,0),node(2,100),node(3,200),node(4,300),
            way(10,[1,2]),way(11,[2,3]),way(12,[3,4]),
            relation(20,from:10,to:11,via:2,tags:["restriction:conditional":"no_left_turn @ (Mo-Fr)"])])
        XCTAssertTrue(conditional.graph.edges.allSatisfy { $0.id.hasPrefix("way:12:") })
    }
    func testCrossingGeometryDoesNotCreateIntersectionAndMissingNodesFailClosed() throws {
        let result = try imported([node(1,-100),node(2,100),node(3,0,-100),node(4,0,100),
            way(10,[1,2]),way(11,[3,4]),way(12,[1,999])])
        XCTAssertEqual(result.omittedWayCount, 1)
        let router = try OfflineNavigationRouter(graph: result.graph)
        XCTAssertThrowsError(try router.route(from: .init(latitude: 0,longitude: -80 / 111_195.08),
            to: .init(latitude: 80 / 111_195.08, longitude: 0), initialBearing: 90)) {
            XCTAssertEqual($0 as? OfflineNavigationError, .noLegalRoute)
        }
    }
    func testNoUTurnOnSameWayDoesNotProhibitContinuingStraight() throws {
        let graph = try imported([node(1,0),node(2,100),node(3,200),way(10,[1,2,3]),
            relation(20,from:10,to:10,via:2,tags:["restriction":"no_u_turn"])]).graph
        XCTAssertTrue(graph.turnRestrictions.contains { $0.fromEdgeID == "way:10:0:f" && $0.toEdgeID == "way:10:0:r" })
        XCTAssertFalse(graph.turnRestrictions.contains { $0.fromEdgeID == "way:10:0:f" && $0.toEdgeID == "way:10:1:f" })
        let route = try OfflineNavigationRouter(graph: graph).route(
            from: .init(latitude:0,longitude:20 / 111_195.08), to: .init(latitude:0,longitude:180 / 111_195.08),initialBearing:90)
        XCTAssertEqual(route.distance,160,accuracy:0.02)
    }
    func testShapeNodesMergeIntoOneInstructionWhileRightTurnRemainsDistinct() throws {
        let graph = try imported([node(1,0),node(2,50),node(3,100),node(4,150),node(5,200),node(6,200,-100),
            way(10,[1,2,3,4,5],tags:["name":"Main Street","oneway":"yes","maxspeed":"36"]),
            way(11,[5,6],tags:["name":"Side Street","oneway":"yes","maxspeed":"36"]) ]).graph
        let router = try OfflineNavigationRouter(graph: graph)
        let start = NavigationCoordinate(latitude:0,longitude:20 / 111_195.08)
        let straight = try router.route(from:start,to:.init(latitude:0,longitude:180 / 111_195.08),initialBearing:90)
        XCTAssertEqual(straight.steps.count,1)
        XCTAssertEqual(straight.steps[0].instruction,"Head along Main Street")
        XCTAssertEqual(straight.steps[0].coordinates.count,5)
        XCTAssertEqual(straight.steps[0].coordinates,straight.coordinates)
        XCTAssertEqual(straight.distance,160,accuracy:0.02)
        XCTAssertEqual(straight.expectedTravelTime,20,accuracy:0.01)
        let turning = try router.route(from:start,to:.init(latitude:-80 / 111_195.08,longitude:200 / 111_195.08),initialBearing:90)
        XCTAssertEqual(turning.steps.count,2)
        XCTAssertEqual(turning.steps[1].instruction,"Turn right onto Side Street")
        XCTAssertEqual(turning.steps[0].distance,180,accuracy:0.02)
        XCTAssertEqual(turning.steps[1].distance,80,accuracy:0.02)
        XCTAssertEqual(turning.distance,260,accuracy:0.02)
        XCTAssertEqual(turning.expectedTravelTime,32.5,accuracy:0.01)
    }
}
