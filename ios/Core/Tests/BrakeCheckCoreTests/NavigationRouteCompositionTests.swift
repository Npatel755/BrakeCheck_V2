import XCTest
@testable import BrakeCheckCore

final class NavigationRouteCompositionTests: XCTestCase {
    private func point(_ east: Double, _ north: Double = 0) -> NavigationCoordinate {
        .init(latitude: north / 111_195.08, longitude: east / 111_195.08)
    }
    private func leg(_ coordinates: [NavigationCoordinate], distance: Double,
                     time: Double, instruction: String, source: String = "Apple Maps") -> SavedNavigationRoute {
        .init(title: instruction, coordinates: coordinates,
              steps: [.init(instruction: instruction, distance: distance, expectedTravelTime: time,
                            coordinates: coordinates)], distance: distance, expectedTravelTime: time, source: source)
    }
    func testAdjacentLegsPreserveInstructionsAndRoutedStopMilestones() throws {
        let first = leg([point(0),point(100)], distance: 120, time: 12, instruction: "Head east")
        let second = leg([point(100),point(100,100)], distance: 140, time: 20, instruction: "Turn left")
        let pickup = NavigationRouteStop(title: "Pickup", coordinate: point(100,25), dwellTime: 30)
        let destination = NavigationRouteStop(title: "Finish", coordinate: point(105,100))
        let preferences = NavigationRoutePreferences(avoidTolls: true, avoidMotorways: true)
        let route = try NavigationRouteComposition.join(legs: [first,second], title: "Errands",
            stops: [pickup,destination], preferences: preferences)
        XCTAssertTrue(route.isValid)
        XCTAssertEqual(route.title, "Errands")
        XCTAssertEqual(route.source, "Apple Maps")
        XCTAssertEqual(route.preferences, preferences)
        XCTAssertEqual(route.coordinates, [point(0),point(100),point(100,100)])
        XCTAssertEqual(route.steps, first.steps + second.steps)
        XCTAssertEqual(route.distance, 260)
        XCTAssertEqual(route.expectedTravelTime, 32)
        XCTAssertEqual(route.stops.map(\.id), [pickup.id,destination.id])
        XCTAssertEqual(route.stops.map(\.title), [pickup.title,destination.title])
        XCTAssertEqual(route.stops.map(\.coordinate), [point(100),point(100,100)])
        XCTAssertEqual(route.stops.map(\.routeDistanceFromStart), [120,260])
        XCTAssertEqual(route.stops[0].dwellTime, 30)

        let intermediateOnly = try NavigationRouteComposition.join(legs: [first,second], title: "Errands",
            stops: [pickup], preferences: preferences)
        XCTAssertEqual(intermediateOnly.stops, Array(route.stops.prefix(1)))
    }
    func testSharedPOICannotJoinOpposingCarriagewayEndpoints() {
        let arriving = leg([point(0),point(100)], distance: 100, time: 10, instruction: "Arrive")
        let departing = leg([point(100,12),point(0,12)], distance: 100, time: 10, instruction: "Depart")
        let sharedPOI = NavigationRouteStop(title: "Same building", coordinate: point(100,6))
        XCTAssertThrowsError(try NavigationRouteComposition.join(legs: [arriving,departing], title: "Trip",
            stops: [sharedPOI], preferences: .init())) {
            XCTAssertEqual($0 as? NavigationRouteCompositionError, .disconnectedLegs)
        }
    }
    func testEmptyInvalidLegsAndMissingStopMetadataFail() {
        let valid = leg([point(0),point(100)], distance: 100, time: 10, instruction: "Continue")
        XCTAssertThrowsError(try NavigationRouteComposition.join(legs: [], title: "Empty", stops: [], preferences: .init())) {
            XCTAssertEqual($0 as? NavigationRouteCompositionError, .invalidLegs)
        }
        var invalid = valid
        invalid.distance = .nan
        XCTAssertThrowsError(try NavigationRouteComposition.join(legs: [invalid], title: "Invalid", stops: [], preferences: .init()))
        XCTAssertThrowsError(try NavigationRouteComposition.join(legs: [valid,valid], title: "Missing stop", stops: [], preferences: .init())) {
            XCTAssertEqual($0 as? NavigationRouteCompositionError, .invalidStops)
        }
        XCTAssertThrowsError(try NavigationRouteComposition.join(legs: [valid], title: "Invalid gap", stops: [],
            preferences: .init(), maximumGap: .nan))
    }
    func testCloseEndpointGapIsBoundedAndRepeatedVerticesAreRemoved() throws {
        let first = leg([point(0),point(0),point(100)], distance: 100, time: 10, instruction: "Continue")
        let second = leg([point(100,4),point(200,4)], distance: 100, time: 10, instruction: "Head east", source: "Offline")
        let stop = NavigationRouteStop(title: "Pickup", coordinate: point(100,2))
        let route = try NavigationRouteComposition.join(legs: [first,second], title: "Trip", stops: [stop], preferences: .init())
        XCTAssertEqual(route.coordinates, [point(0),point(100),point(100,4),point(200,4)])
        XCTAssertEqual(route.source, "Combined route")
        XCTAssertThrowsError(try NavigationRouteComposition.join(legs: [first,second], title: "Trip", stops: [stop],
            preferences: .init(), maximumGap: 3)) {
            XCTAssertEqual($0 as? NavigationRouteCompositionError, .disconnectedLegs)
        }
    }
    func testOverflowingCombinedRouteIsRejected() {
        let first = leg([point(0),point(100)], distance: .greatestFiniteMagnitude, time: 10, instruction: "Continue")
        let second = leg([point(100),point(200)], distance: .greatestFiniteMagnitude, time: 10, instruction: "Continue")
        XCTAssertThrowsError(try NavigationRouteComposition.join(legs: [first,second], title: "Overflow",
            stops: [.init(title: "Pickup", coordinate: point(100))], preferences: .init())) {
            XCTAssertEqual($0 as? NavigationRouteCompositionError, .invalidRoute)
        }
    }
}
