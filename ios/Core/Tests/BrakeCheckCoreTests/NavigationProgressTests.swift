import XCTest
@testable import BrakeCheckCore

final class NavigationProgressTests: XCTestCase {
    private func point(_ x: Double, _ y: Double = 0) -> NavigationCoordinate {
        let metersPerDegree = 6_371_008.8 * Double.pi / 180
        return NavigationCoordinate(latitude: y / metersPerDegree, longitude: x / metersPerDegree)
    }
    private func straight(distance: Double = 100, steps: [Double] = [50, 50]) -> NavigationProgressTracker {
        NavigationProgressTracker(coordinates: [point(0), point(100)], routeDistance: distance, stepDistances: steps)
    }
    private func fix(_ tracker: inout NavigationProgressTracker, _ x: Double, _ y: Double = 0,
                     at time: Double, accuracy: Double = 5) throws -> NavigationProgressSnapshot {
        try XCTUnwrap(tracker.update(coordinate: point(x, y), horizontalAccuracy: accuracy, timestamp: time, now: time))
    }

    func testProjectsOntoSegmentAndScalesProgressToProviderDistance() throws {
        var tracker = straight(distance: 200)
        let snapshot = try fix(&tracker, 40, 12, at: 0)
        XCTAssertEqual(snapshot.progressMeters, 80, accuracy: 0.01)
        XCTAssertEqual(snapshot.remainingMeters, 120, accuracy: 0.01)
        XCTAssertEqual(snapshot.distanceFromRoute, 12, accuracy: 0.01)
        XCTAssertEqual(snapshot.distanceToStep, 20, accuracy: 0.01)
        XCTAssertEqual(snapshot.stepIndex, 1)
        XCTAssertFalse(snapshot.offRoute)
        XCTAssertFalse(snapshot.arrived)
    }

    func testProjectsAroundCornerWithoutCuttingAcrossIt() throws {
        var tracker = NavigationProgressTracker(coordinates: [point(0), point(100), point(100, 100)],
                                                routeDistance: 200, stepDistances: [100, 50, 50])
        _ = try fix(&tracker, 80, at: 0)
        let snapshot = try fix(&tracker, 103, 25, at: 1)
        XCTAssertEqual(snapshot.progressMeters, 125, accuracy: 0.01)
        XCTAssertEqual(snapshot.distanceFromRoute, 3, accuracy: 0.01)
        XCTAssertEqual(snapshot.distanceToStep, 25, accuracy: 0.01)
        XCTAssertEqual(snapshot.stepIndex, 2)
    }

    func testGuidanceShowsHeadingThenNextInstructionAndUsesSegmentEnd() throws {
        var tracker = NavigationProgressTracker(coordinates: [point(0), point(200)],
                                                routeDistance: 200, stepDistances: [100, 50, 50])
        let heading = try fix(&tracker, 0, at: 0)
        XCTAssertEqual(heading.stepIndex, 0)
        XCTAssertEqual(heading.distanceToStep, 100, accuracy: 0.01)
        let firstTurn = try fix(&tracker, 40, at: 1)
        XCTAssertEqual(firstTurn.stepIndex, 1)
        XCTAssertEqual(firstTurn.distanceToStep, 60, accuracy: 0.01)
        let nextTurn = try fix(&tracker, 100, at: 2)
        XCTAssertEqual(nextTurn.stepIndex, 2)
        XCTAssertEqual(nextTurn.distanceToStep, 50, accuracy: 0.01)
        _ = try fix(&tracker, 150, at: 3)
        let final = try fix(&tracker, 200, at: 4)
        XCTAssertEqual(final.stepIndex, 2)
        XCTAssertEqual(final.distanceToStep, 0, accuracy: 0.01)
    }

    func testZeroLengthStepsAreSkippedAndStepLengthsAreNormalized() throws {
        var tracker = straight(distance: 200, steps: [0, 25, 25, 0])
        XCTAssertEqual(try fix(&tracker, 0, at: 0).stepIndex, 0)
        let first = try fix(&tracker, 20, at: 1)
        XCTAssertEqual(first.stepIndex, 2)
        XCTAssertEqual(first.distanceToStep, 60, accuracy: 0.01)
        let next = try fix(&tracker, 50, at: 2)
        XCTAssertEqual(next.stepIndex, 3)
        XCTAssertEqual(next.distanceToStep, 100, accuracy: 0.01)
    }

    func testEmptyStepsStillProvideDistanceToDestination() throws {
        var tracker = straight(steps: [])
        let snapshot = try fix(&tracker, 40, at: 0)
        XCTAssertEqual(snapshot.stepIndex, 0)
        XCTAssertEqual(snapshot.distanceToStep, 60, accuracy: 0.01)
    }

    func testSingleNoisyFixDoesNotConfirmOffRouteOrAdvanceProgress() throws {
        var tracker = straight()
        _ = try fix(&tracker, 10, at: 0)
        let noise = try fix(&tracker, 50, 60, at: 1)
        XCTAssertEqual(noise.progressMeters, 10, accuracy: 0.01)
        XCTAssertEqual(noise.distanceFromRoute, 60, accuracy: 0.01)
        XCTAssertFalse(noise.offRoute)
        XCTAssertFalse(noise.positionReliable)
        let recovered = try fix(&tracker, 25, at: 2)
        XCTAssertFalse(recovered.offRoute)
        XCTAssertTrue(recovered.positionReliable)
        XCTAssertEqual(recovered.progressMeters, 25, accuracy: 0.01)
    }

    func testOffRouteNeedsRepeatedFixesAndRecoveryNeedsFreshConfirmation() throws {
        var tracker = straight()
        _ = try fix(&tracker, 0, at: 0)
        XCTAssertFalse(try fix(&tracker, 20, 60, at: 1).offRoute)
        XCTAssertFalse(try fix(&tracker, 20, 60, at: 2).offRoute)
        let outside = try fix(&tracker, 20, 60, at: 3)
        XCTAssertTrue(outside.offRoute)
        XCTAssertEqual(outside.progressMeters, 0, accuracy: 0.01)
        let firstRecovery = try fix(&tracker, 25, at: 4)
        XCTAssertTrue(firstRecovery.offRoute)
        XCTAssertEqual(firstRecovery.progressMeters, 0, accuracy: 0.01)
        let recovered = try fix(&tracker, 30, at: 5)
        XCTAssertFalse(recovered.offRoute)
        XCTAssertEqual(recovered.progressMeters, 30, accuracy: 0.01)
    }

    func testUncertainRouteDistancePausesWithoutDeclaringOffRoute() throws {
        var tracker = straight()
        _ = try fix(&tracker, 0, at: 0)
        for time in [1.0, 2.0, 3.0] {
            let uncertain = try fix(&tracker, 40, 25, at: time)
            XCTAssertFalse(uncertain.offRoute)
            XCTAssertFalse(uncertain.positionReliable)
            XCTAssertEqual(uncertain.progressMeters, 0, accuracy: 0.01)
        }
    }

    func testUnreachableButNearbyFixCannotAuthorizeGuidance() throws {
        var tracker = straight()
        _ = try fix(&tracker, 0, at: 0)
        let jump = try XCTUnwrap(tracker.update(coordinate: point(80), horizontalAccuracy: 5,
            timestamp: 1, now: 1, speed: 0))
        XCTAssertFalse(jump.positionReliable)
        XCTAssertEqual(jump.progressMeters, 0, accuracy: 0.01)
        XCTAssertFalse(jump.offRoute) // Rerouting still requires confirmation.
    }

    func testParkedFixIgnoresRetainedOppositeCourse() throws {
        var tracker = straight()
        for time in [0.0, 1.0, 2.0] {
            let fix = try XCTUnwrap(tracker.update(coordinate: point(0), horizontalAccuracy: 5,
                timestamp: time, now: time, courseDegrees: 270, courseAccuracy: 5, speed: 0))
            XCTAssertTrue(fix.positionReliable)
            XCTAssertFalse(fix.offRoute)
        }
    }

    func testInvalidStaleFutureAndUnorderedFixesAreRejectedWithoutRenewingState() throws {
        var tracker = straight()
        _ = try fix(&tracker, 10, at: 10)
        XCTAssertNil(tracker.update(coordinate: point(20), horizontalAccuracy: 5, timestamp: 11, now: 14))
        XCTAssertNil(tracker.update(coordinate: point(20), horizontalAccuracy: 5, timestamp: 12, now: 11))
        XCTAssertNil(tracker.update(coordinate: point(20), horizontalAccuracy: 5, timestamp: 10, now: 10))
        XCTAssertNil(tracker.update(coordinate: point(20), horizontalAccuracy: 5, timestamp: 9, now: 10))
        for accuracy in [-1.0, 35.01, .nan, .infinity] {
            XCTAssertNil(tracker.update(coordinate: point(20), horizontalAccuracy: accuracy, timestamp: 11, now: 11))
        }
        for invalid in [NavigationCoordinate(latitude: 91, longitude: 0),
                        NavigationCoordinate(latitude: 0, longitude: 181),
                        NavigationCoordinate(latitude: .nan, longitude: 0)] {
            XCTAssertNil(tracker.update(coordinate: invalid, horizontalAccuracy: 5, timestamp: 11, now: 11))
        }
        XCTAssertEqual(try fix(&tracker, 40, at: 12).progressMeters, 40, accuracy: 0.01)
    }

    func testImplausibleGPSJumpDoesNotPoisonNextGoodFix() throws {
        var tracker = straight()
        _ = try fix(&tracker, 0, at: 0)
        XCTAssertNil(tracker.update(coordinate: point(10_000), horizontalAccuracy: 5, timestamp: 1, now: 1))
        XCTAssertEqual(try fix(&tracker, 40, at: 2).progressMeters, 40, accuracy: 0.01)
    }

    func testLoopCrossingCannotSkipToDestinationFromStationaryFixes() throws {
        let coordinates = [point(0), point(100), point(100, 100), point(0, 100), point(0)]
        var tracker = NavigationProgressTracker(coordinates: coordinates, routeDistance: 400, stepDistances: [100, 100, 100, 100])
        for time in [0.0, 1.0, 2.0, 3.0] {
            let snapshot = try fix(&tracker, 0, at: time)
            XCTAssertEqual(snapshot.progressMeters, 0, accuracy: 0.01)
            XCTAssertFalse(snapshot.arrived)
            XCTAssertFalse(snapshot.offRoute)
        }
    }

    func testCompleteLoopArrivesOnlyAfterTraversingRouteAndRepeatedTerminalFixes() throws {
        let coordinates = [point(0), point(100), point(100, 100), point(0, 100), point(0)]
        var tracker = NavigationProgressTracker(coordinates: coordinates, routeDistance: 400, stepDistances: [100, 100, 100, 100])
        let journey: [(Double, Double)] = [(0, 0), (50, 0), (100, 0), (100, 50), (100, 100),
                                           (50, 100), (0, 100), (0, 50), (0, 0)]
        for (index, coordinate) in journey.enumerated() {
            let snapshot = try fix(&tracker, coordinate.0, coordinate.1, at: Double(index))
            XCTAssertEqual(snapshot.progressMeters, Double(index) * 50, accuracy: 0.01)
            XCTAssertFalse(snapshot.arrived)
        }
        XCTAssertFalse(try fix(&tracker, 0, at: 9).arrived)
        XCTAssertTrue(try fix(&tracker, 0, at: 10).arrived)
    }

    func testArrivalRequiresActualProgressAndThreeFreshTerminalFixes() throws {
        var tracker = straight()
        _ = try fix(&tracker, 0, at: 0)
        _ = try fix(&tracker, 50, at: 1)
        XCTAssertFalse(try fix(&tracker, 89, at: 2).arrived)
        XCTAssertFalse(try fix(&tracker, 95, at: 3).arrived)
        XCTAssertFalse(try fix(&tracker, 99, at: 4).arrived)
        XCTAssertTrue(try fix(&tracker, 100, at: 5).arrived)
    }

    func testFixesAtDestinationCannotInventPreviouslyTraveledRoute() throws {
        var tracker = straight()
        for time in [0.0, 1.0, 2.0, 3.0] {
            XCTAssertFalse(try fix(&tracker, 100, at: time).arrived)
        }
    }

    func testOffRouteFixesNearEndNeitherAdvanceNorAuthorizeArrival() throws {
        var tracker = straight()
        _ = try fix(&tracker, 0, at: 0)
        _ = try fix(&tracker, 60, at: 1)
        for time in [2.0, 3.0, 4.0] {
            let snapshot = try fix(&tracker, 100, 60, at: time)
            XCTAssertEqual(snapshot.progressMeters, 60, accuracy: 0.01)
            XCTAssertFalse(snapshot.arrived)
        }
    }

    func testArrivalNeedsAccurateFixesEvenWhenAlongRoute() throws {
        var tracker = straight()
        _ = try fix(&tracker, 0, at: 0)
        _ = try fix(&tracker, 60, at: 1)
        for time in [2.0, 3.0, 4.0] {
            XCTAssertFalse(try fix(&tracker, 100, at: time, accuracy: 25).arrived)
        }
    }

    func testAntimeridianUsesShortSegmentAndDuplicateVerticesAreHarmless() throws {
        let start = NavigationCoordinate(latitude: 0, longitude: 179.999)
        let end = NavigationCoordinate(latitude: 0, longitude: -179.999)
        var tracker = NavigationProgressTracker(coordinates: [start, start, end], routeDistance: 200, stepDistances: [200])
        let snapshot = try XCTUnwrap(tracker.update(coordinate: NavigationCoordinate(latitude: 0, longitude: 180),
            horizontalAccuracy: 5, timestamp: 0, now: 0))
        XCTAssertEqual(snapshot.progressMeters, 100, accuracy: 0.01)
        XCTAssertEqual(snapshot.distanceFromRoute, 0, accuracy: 0.01)
    }

    func testInvalidOrDegenerateRoutesCannotProduceProgress() {
        for (coordinates, distance, steps) in [([point(0)], 100.0, [100.0]),
            ([point(0), point(0)], 100, [100]), ([point(0), point(100)], 0, [100]),
            ([point(0), point(100)], Double.nan, [100]), ([point(0), point(100)], 100, [-1]),
            ([NavigationCoordinate(latitude: 100, longitude: 0), point(100)], 100, [100])] {
            var tracker = NavigationProgressTracker(coordinates: coordinates, routeDistance: distance, stepDistances: steps)
            XCTAssertNil(tracker.update(coordinate: point(0), horizontalAccuracy: 5, timestamp: 0, now: 0))
        }
    }

    func testPausedNavigationDoesNotShowStraightAheadArrow() {
        XCTAssertEqual(NavigationManeuver.symbol(for: "Navigation paused"), "pause.fill")
        XCTAssertEqual(NavigationManeuver.symbol(for: ""), "pause.fill")
    }
}
