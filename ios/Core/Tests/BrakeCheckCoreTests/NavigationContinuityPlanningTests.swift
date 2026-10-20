import XCTest
@testable import BrakeCheckCore

final class NavigationContinuityPlanningTests: XCTestCase {
    func testOutagePresentationIsBoundedExpiringAndRejectsTeleport() throws {
        let origin = NavigationCoordinate(latitude: 0, longitude: 0)
        var estimator = NavigationPresentationEstimator()
        XCTAssertTrue(estimator.ingest(coordinate: origin, bearing: 90, speed: 30, accuracy: 5, timestamp: 100, now: 100))
        XCTAssertFalse(try XCTUnwrap(estimator.estimate(now: 101)).isEstimated)
        let estimated = try XCTUnwrap(estimator.estimate(now: 104))
        XCTAssertTrue(estimated.isEstimated)
        XCTAssertEqual(origin.distance(to: estimated.coordinate), 80, accuracy: 0.01)
        XCTAssertGreaterThan(estimated.uncertaintyRadius, 50)
        XCTAssertNil(estimator.estimate(now: 104.01))
        XCTAssertFalse(estimator.ingest(coordinate: .init(latitude: 1, longitude: 0), bearing: 90,
            speed: 30, accuracy: 5, timestamp: 101, now: 101))
        XCTAssertNil(estimator.estimate(now: 99))
    }
    func testMissingCourseCannotInventTravelDuringOutage() throws {
        let origin = NavigationCoordinate(latitude: 0, longitude: 0)
        var estimator = NavigationPresentationEstimator()
        XCTAssertTrue(estimator.ingest(coordinate: origin, bearing: nil, speed: 30, accuracy: 5, timestamp: 100, now: 100))
        XCTAssertEqual(try XCTUnwrap(estimator.estimate(now: 104)).coordinate, origin)
        XCTAssertTrue(try XCTUnwrap(estimator.estimate(now: 104)).isEstimated)
    }
    func testETAUsesMeasuredPaceAndTrafficWithoutInstantaneousSpeedSpikes() throws {
        var estimator = NavigationETAEstimator()
        let initial = try XCTUnwrap(estimator.update(remainingDistance: 1000, routeDistance: 1000,
            expectedTravelTime: 100, timestamp: 0))
        XCTAssertEqual(initial.remainingTime, 100)
        var latest = initial
        for time in 1...60 {
            latest = try XCTUnwrap(estimator.update(remainingDistance: 1000 - Double(time) * 2,
                routeDistance: 1000, expectedTravelTime: 100, timestamp: Double(time)))
        }
        XCTAssertTrue(latest.usesObservedPace)
        XCTAssertGreaterThan(latest.remainingTime, latest.baselineRemainingTime * 1.5)
        XCTAssertNil(estimator.update(remainingDistance: 900, routeDistance: 1000, expectedTravelTime: 100, timestamp: 61))
        XCTAssertEqual(try XCTUnwrap(estimator.update(remainingDistance: 0, routeDistance: 1000,
            expectedTravelTime: 100, timestamp: 62)).remainingTime, 0)
    }
    func testHistoryRequiresComparableRepeatedTripsAndPrunesInvalidData() throws {
        var calendar = Calendar(identifier: .gregorian); calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let now = Date(timeIntervalSince1970: 1_792_411_200) // Monday morning in UTC.
        let records = (1...3).map { NavigationTrafficObservation(routeKey: "commute", departure: now.addingTimeInterval(Double(-7 * $0 * 86_400)),
            duration: 150, baselineDuration: 100, distance: 1000) }
        XCTAssertNil(NavigationTrafficHistory.pattern(for: "commute", departure: now,
            observations: Array(records.prefix(2)), calendar: calendar, now: now))
        let pattern = try XCTUnwrap(NavigationTrafficHistory.pattern(for: "commute", departure: now,
            observations: records, calendar: calendar, now: now))
        XCTAssertEqual(pattern.multiplier, 1.5)
        XCTAssertEqual(pattern.sampleCount, 3)
        XCTAssertNil(NavigationTrafficHistory.pattern(for: "other", departure: now,
            observations: records, calendar: calendar, now: now))
        let plan = try XCTUnwrap(NavigationTravelPlan.arriving(at: now, duration: 100,
            historicalMultiplier: pattern.multiplier, stopTime: 30))
        XCTAssertEqual(plan.departure, now.addingTimeInterval(-180))
        XCTAssertEqual(plan.arrival, now)
        XCTAssertTrue(plan.usesHistoricalTraffic)
        XCTAssertEqual(NavigationTrafficHistory.normalized(records + records, now: now).count, 3)
    }
    func testWrongHeadingProducesNoRouteSnapAndCannotAdvance() throws {
        let a = NavigationCoordinate(latitude: 0, longitude: 0)
        let b = NavigationCoordinate(latitude: 0, longitude: 0.001)
        var tracker = NavigationProgressTracker(coordinates: [a,b], routeDistance: 111, stepDistances: [111])
        let wrong = try XCTUnwrap(tracker.update(coordinate: a, horizontalAccuracy: 5, timestamp: 0, now: 0,
            courseDegrees: 270, courseAccuracy: 5, speed: 10))
        XCTAssertNil(wrong.snappedCoordinate)
        XCTAssertNil(wrong.snappedBearing)
        XCTAssertFalse(wrong.positionReliable)
        XCTAssertEqual(wrong.mapMatchConfidence, 0)
        XCTAssertFalse(wrong.arrived)
    }
    func testStopMilestoneIsOptionalInOlderArchivesAndRejectsInvalidValues() throws {
        var stop = NavigationRouteStop(title: "Stop", coordinate: .init(latitude: 0, longitude: 0))
        let legacy = try JSONEncoder().encode(stop)
        XCTAssertFalse(String(decoding: legacy, as: UTF8.self).contains("routeDistanceFromStart"))
        XCTAssertNil(try JSONDecoder().decode(NavigationRouteStop.self, from: legacy).routeDistanceFromStart)
        for invalid in [-1.0, Double.nan, Double.infinity] {
            stop.routeDistanceFromStart = invalid
            XCTAssertFalse(stop.isValid)
        }
        stop.routeDistanceFromStart = 0
        XCTAssertTrue(stop.isValid)
    }
    func testSnapsFollowReliableDirectionalMatchAndHideAmbiguousOrUnreachableMatches() throws {
        func point(_ east: Double, _ north: Double = 0) -> NavigationCoordinate {
            .init(latitude: north / 111_195.08, longitude: east / 111_195.08)
        }
        var directional = NavigationProgressTracker(coordinates: [point(0), point(0,200), point(10,200), point(10)],
            routeDistance: 410, stepDistances: [200,10,200])
        let matched = try XCTUnwrap(directional.update(coordinate: point(9,80), horizontalAccuracy: 5,
            timestamp: 10, now: 10, courseDegrees: 180, courseAccuracy: 5, speed: 10))
        XCTAssertTrue(matched.positionReliable)
        XCTAssertEqual(try XCTUnwrap(matched.snappedCoordinate).distance(to: point(10,80)), 0, accuracy: 0.01)
        XCTAssertEqual(try XCTUnwrap(matched.snappedBearing), 180, accuracy: 0.01)

        var ambiguous = NavigationProgressTracker(coordinates: [point(0), point(0,100), point(30,100),
            point(30), point(0), point(0,100)], routeDistance: 360, stepDistances: [100,30,100,30,100])
        let branch = try XCTUnwrap(ambiguous.update(coordinate: point(0,50), horizontalAccuracy: 5,
            timestamp: 10, now: 10, courseDegrees: 0, courseAccuracy: 5, speed: 10))
        XCTAssertTrue(branch.mapMatchAmbiguous)
        XCTAssertNil(branch.snappedCoordinate)
        XCTAssertNil(branch.snappedBearing)
        XCTAssertFalse(branch.positionReliable)
        XCTAssertEqual(branch.progressMeters, 0)

        var stationary = NavigationProgressTracker(coordinates: [point(0),point(100)],
            routeDistance: 100, stepDistances: [100])
        _ = stationary.update(coordinate: point(0), horizontalAccuracy: 5, timestamp: 0, now: 0, speed: 0)
        let unreachable = try XCTUnwrap(stationary.update(coordinate: point(80), horizontalAccuracy: 5,
            timestamp: 1, now: 1, speed: 0))
        XCTAssertFalse(unreachable.positionReliable)
        XCTAssertNil(unreachable.snappedCoordinate)
        XCTAssertNil(unreachable.snappedBearing)
        XCTAssertEqual(unreachable.progressMeters, 0)
    }
    func testMeasuredSnapWrapsAcrossAntimeridian() throws {
        var tracker = NavigationProgressTracker(coordinates: [
            .init(latitude: 0, longitude: 179.999), .init(latitude: 0, longitude: -179.999)],
            routeDistance: 200, stepDistances: [200])
        let fix = try XCTUnwrap(tracker.update(coordinate: .init(latitude: 0, longitude: 180),
            horizontalAccuracy: 5, timestamp: 0, now: 0))
        let snapped = try XCTUnwrap(fix.snappedCoordinate)
        XCTAssertTrue(snapped.isValid)
        XCTAssertEqual(abs(snapped.longitude), 180, accuracy: 0.0000001)
        XCTAssertEqual(try XCTUnwrap(fix.snappedBearing), 90, accuracy: 0.01)
    }
    func testHistoryDistinguishesStopsAndPathsButIgnoresPolylineDensification() {
        func point(_ x: Double, _ y: Double = 0) -> NavigationCoordinate {
            .init(latitude: y / 111_195.08, longitude: x / 111_195.08)
        }
        func route(_ points: [NavigationCoordinate], stops: [NavigationRouteStop] = []) -> SavedNavigationRoute {
            .init(title: "Trip", coordinates: points, steps: [], distance: 1000, expectedTravelTime: 100, stops: stops)
        }
        let direct = route([point(0),point(1000)])
        let densified = route([point(0),point(250),point(500),point(750),point(1000)])
        let differentPath = route([point(0),point(500,500),point(1000)])
        XCTAssertEqual(direct.historyKey, densified.historyKey)
        XCTAssertNotEqual(direct.historyKey, differentPath.historyKey)
        let pickup = NavigationRouteStop(title: "Pickup", coordinate: point(500), routeDistanceFromStart: 500)
        XCTAssertNotEqual(direct.historyKey, route(direct.coordinates, stops: [pickup]).historyKey)
        let destination = NavigationRouteStop(title: "Finish", coordinate: point(1000), routeDistanceFromStart: 1000)
        XCTAssertEqual(direct.historyKey, route(direct.coordinates, stops: [destination]).historyKey)
    }
}
