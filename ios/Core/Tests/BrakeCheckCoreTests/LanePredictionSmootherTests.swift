import XCTest
@testable import BrakeCheckCore

final class LanePredictionSmootherTests: XCTestCase {
    private typealias Observation = LanePredictionObservation
    private typealias Boundary = Observation.Boundary
    private typealias Point = Observation.Point

    private func boundary(slot: Int = 1, shift: Double = 0, anchors: [Double] = [0.1, 0.2, 0.3, 0.4, 0.5]) -> Boundary {
        .init(laneSlot: slot, confidence: 0.9, points: anchors.map {
            slot == 1 || slot == 2 ? Point(x: 0.3 + 0.1 * $0 + shift, y: $0)
                : Point(x: $0, y: 0.3 + 0.1 * $0 + shift)
        })
    }
    private func observation(_ boundaries: [Boundary], at time: Double) -> Observation {
        .init(capturedAt: time, boundaries: boundaries)
    }

    func testCompatibleJitterIsReducedWithoutChangingEndpointsSupportConfidenceOrTime() throws {
        var smoother = LanePredictionSmoother()
        let first = observation([boundary()], at: 10)
        XCTAssertEqual(smoother.update(first, at: 10.05), first)
        let current = observation([boundary(shift: 0.02)], at: 10.2)
        let output = try XCTUnwrap(smoother.update(current, at: 10.25))
        XCTAssertEqual(output.capturedAt, current.capturedAt)
        XCTAssertEqual(output.boundaries.count, 1)
        XCTAssertEqual(output.boundaries[0].confidence, current.boundaries[0].confidence)
        XCTAssertEqual(output.boundaries[0].points.map(\.y), current.boundaries[0].points.map(\.y))
        XCTAssertEqual(output.boundaries[0].points.first, current.boundaries[0].points.first)
        XCTAssertEqual(output.boundaries[0].points.last, current.boundaries[0].points.last)
        for index in 1...3 {
            XCTAssertGreaterThan(output.boundaries[0].points[index].x, first.boundaries[0].points[index].x)
            XCTAssertLessThan(output.boundaries[0].points[index].x, current.boundaries[0].points[index].x)
        }
    }

    func testSingleBoundaryNeverCreatesAnOppositeBoundaryOrMissingNeighbor() throws {
        var smoother = LanePredictionSmoother()
        _ = smoother.update(observation([boundary(slot: 2)], at: 10), at: 10)
        let output = try XCTUnwrap(smoother.update(observation([boundary(slot: 2, shift: 0.02)], at: 10.2), at: 10.2))
        XCTAssertEqual(output.boundaries.map(\.laneSlot), [2])
    }

    func testColumnSlotsSmoothYAtActualXAndPreserveTheirEndpoints() throws {
        var smoother = LanePredictionSmoother()
        let first = observation([boundary(slot: 3)], at: 10)
        _ = smoother.update(first, at: 10)
        let current = observation([boundary(slot: 3, shift: 0.02)], at: 10.2)
        let output = try XCTUnwrap(smoother.update(current, at: 10.2))
        XCTAssertEqual(output.boundaries[0].points.map(\.x), current.boundaries[0].points.map(\.x))
        XCTAssertEqual(output.boundaries[0].points.first, current.boundaries[0].points.first)
        XCTAssertEqual(output.boundaries[0].points.last, current.boundaries[0].points.last)
        XCTAssertLessThan(output.boundaries[0].points[2].y, current.boundaries[0].points[2].y)
        XCTAssertGreaterThan(output.boundaries[0].points[2].y, first.boundaries[0].points[2].y)
    }

    func testDuplicateSampleIsIdempotentAndDoesNotRenewItsTimestamp() throws {
        var smoother = LanePredictionSmoother()
        _ = smoother.update(observation([boundary()], at: 10), at: 10)
        let current = observation([boundary(shift: 0.02)], at: 10.2)
        let output = try XCTUnwrap(smoother.update(current, at: 10.25))
        for time in [10.3, 10.4, 10.6] {
            XCTAssertEqual(smoother.update(current, at: time), output)
        }
        XCTAssertNil(smoother.update(current, at: 10.71))
    }

    func testNegativeResultImmediatelyClearsHistoryInsteadOfHoldingOldPaint() {
        var smoother = LanePredictionSmoother()
        _ = smoother.update(observation([boundary()], at: 10), at: 10)
        XCTAssertNil(smoother.update(nil, at: 10.1))
        let next = observation([boundary(shift: 0.02)], at: 10.2)
        XCTAssertEqual(smoother.update(next, at: 10.2), next)
    }

    func testStaleFutureAndNonfiniteResultsClearSmoothingHistory() {
        for bad in [observation([boundary()], at: 9), observation([boundary()], at: 11),
                    observation([boundary()], at: .nan)] {
            var smoother = LanePredictionSmoother()
            _ = smoother.update(observation([boundary()], at: 10), at: 10)
            XCTAssertNil(smoother.update(bad, at: 10.1))
            let next = observation([boundary(shift: 0.02)], at: 10.2)
            XCTAssertEqual(smoother.update(next, at: 10.2), next)
        }
        var smoother = LanePredictionSmoother()
        XCTAssertNil(smoother.update(observation([boundary()], at: 10), at: .infinity))
    }

    func testLongGapAndExplicitSessionResetStartWithTheNewActualObservation() {
        var smoother = LanePredictionSmoother()
        _ = smoother.update(observation([boundary()], at: 10), at: 10)
        let afterGap = observation([boundary(shift: 0.02)], at: 10.6)
        XCTAssertEqual(smoother.update(afterGap, at: 10.6), afterGap)
        smoother.reset()
        let afterReset = observation([boundary(shift: 0.03)], at: 10.8)
        XCTAssertEqual(smoother.update(afterReset, at: 10.8), afterReset)
    }

    func testSceneJumpUsesNewGeometryImmediatelyAndDoesNotBlendAcrossTheCut() throws {
        var smoother = LanePredictionSmoother()
        _ = smoother.update(observation([boundary()], at: 10), at: 10)
        let jump = observation([boundary(shift: 0.3)], at: 10.2)
        XCTAssertEqual(smoother.update(jump, at: 10.2), jump)
        let next = observation([boundary(shift: 0.32)], at: 10.4)
        let output = try XCTUnwrap(smoother.update(next, at: 10.4))
        XCTAssertGreaterThan(output.boundaries[0].points[2].x, jump.boundaries[0].points[2].x)
        XCTAssertLessThan(output.boundaries[0].points[2].x, next.boundaries[0].points[2].x)
    }

    func testDisconnectedFragmentsRemainSeparateAndAssociateOnlyWithinSharedSupport() throws {
        var smoother = LanePredictionSmoother()
        let near = [0.1, 0.15, 0.2, 0.25], far = [0.4, 0.45, 0.5, 0.55]
        _ = smoother.update(observation([boundary(anchors: near), boundary(anchors: far)], at: 10), at: 10)
        let current = observation([boundary(shift: 0.02, anchors: near), boundary(shift: 0.03, anchors: far)], at: 10.2)
        let output = try XCTUnwrap(smoother.update(current, at: 10.2))
        XCTAssertEqual(output.boundaries.count, 2)
        for index in 0..<2 {
            XCTAssertEqual(output.boundaries[index].points.count, current.boundaries[index].points.count)
            XCTAssertEqual(output.boundaries[index].points.map(\.y), current.boundaries[index].points.map(\.y))
            XCTAssertEqual(output.boundaries[index].points.first, current.boundaries[index].points.first)
            XCTAssertEqual(output.boundaries[index].points.last, current.boundaries[index].points.last)
            XCTAssertLessThan(output.boundaries[index].points[1].x, current.boundaries[index].points[1].x)
        }
    }

    func testAmbiguousOverlappingOldFragmentsDoNotChooseAnArbitraryBranch() {
        var smoother = LanePredictionSmoother()
        _ = smoother.update(observation([boundary(), boundary(shift: 0.02)], at: 10), at: 10)
        let current = observation([boundary(shift: 0.01)], at: 10.2)
        XCTAssertEqual(smoother.update(current, at: 10.2), current)
    }

    func testNewlyVisibleSupportIsNeverExtrapolatedFromOldEndpoints() throws {
        var smoother = LanePredictionSmoother()
        _ = smoother.update(observation([boundary(anchors: [0.2, 0.25, 0.3, 0.4, 0.5])], at: 10), at: 10)
        let current = observation([boundary(shift: 0.02, anchors: [0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.55])], at: 10.2)
        let output = try XCTUnwrap(smoother.update(current, at: 10.2))
        XCTAssertEqual(output.boundaries[0].points[0], current.boundaries[0].points[0])
        XCTAssertEqual(output.boundaries[0].points[1], current.boundaries[0].points[1])
        XCTAssertEqual(output.boundaries[0].points.last, current.boundaries[0].points.last)
        XCTAssertLessThan(output.boundaries[0].points[3].x, current.boundaries[0].points[3].x)
    }

    func testDifferentSlotAndNonmonotonicSupportAreNotSmoothed() {
        var smoother = LanePredictionSmoother()
        _ = smoother.update(observation([boundary(slot: 1)], at: 10), at: 10)
        let differentSlot = observation([boundary(slot: 2, shift: 0.02)], at: 10.2)
        XCTAssertEqual(smoother.update(differentSlot, at: 10.2), differentSlot)
        smoother.reset()
        _ = smoother.update(observation([boundary()], at: 10), at: 10)
        let nonmonotonic = observation([boundary(shift: 0.02, anchors: [0.1, 0.2, 0.15, 0.4, 0.5])], at: 10.2)
        XCTAssertEqual(smoother.update(nonmonotonic, at: 10.2), nonmonotonic)
    }

    func testInvalidConfidenceAndCoordinatesNeverReintroduceRejectedPaint() throws {
        var smoother = LanePredictionSmoother()
        _ = smoother.update(observation([boundary()], at: 10), at: 10)
        let weak = Boundary(laneSlot: 1, confidence: 0.69, points: boundary().points)
        XCTAssertNil(smoother.update(observation([weak], at: 10.2), at: 10.2))
        let invalid = Boundary(laneSlot: 1, confidence: 0.9,
            points: [.init(x: 0.3, y: 0.1), .init(x: 0.31, y: 0.2), .init(x: .nan, y: 0.3),
                .init(x: 0.33, y: 0.4), .init(x: 0.34, y: 0.5)])
        let output = try XCTUnwrap(smoother.update(observation([invalid], at: 10.4), at: 10.4))
        XCTAssertEqual(output.boundaries.map { $0.points.count }, [2, 2])
        XCTAssertTrue(output.boundaries.flatMap(\.points).allSatisfy { $0.x.isFinite && $0.y.isFinite })
    }

    func testVehicleMaskAfterSmoothingStillSplitsWithoutDrawingAcrossTheVehicle() throws {
        var smoother = LanePredictionSmoother()
        _ = smoother.update(observation([boundary(anchors: [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7])], at: 10), at: 10)
        let current = observation([boundary(shift: 0.02, anchors: [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7])], at: 10.2)
        let smoothed = try XCTUnwrap(smoother.update(current, at: 10.2))
        let visible = try XCTUnwrap(LanePredictionDisplay.visible(smoothed, at: 10.2,
            excluding: [CGRect(x: 0.2, y: 0.35, width: 0.3, height: 0.1)]))
        XCTAssertEqual(visible.boundaries.count, 2)
        XCTAssertEqual(visible.boundaries[0].points.map(\.y), [0.1, 0.2, 0.3])
        XCTAssertEqual(visible.boundaries[1].points.map(\.y), [0.5, 0.6, 0.7])
        XCTAssertEqual(visible.capturedAt, current.capturedAt)
    }

    func testEndpointPreservationCannotIntroduceCrossingBetweenNeighboringSlots() {
        func line(slot: Int, x: Double, anchors: [Double]) -> Boundary {
            .init(laneSlot: slot, confidence: 0.9, points: anchors.map { .init(x: x, y: $0) })
        }
        var smoother = LanePredictionSmoother()
        let prior = observation([
            line(slot: 1, x: 0.53, anchors: [0.1, 0.2, 0.3, 0.4]),
            line(slot: 2, x: 0.55, anchors: [0.2, 0.3, 0.4])
        ], at: 10)
        _ = smoother.update(prior, at: 10)
        let current = observation([
            line(slot: 1, x: 0.49, anchors: [0.1, 0.2, 0.3, 0.4]),
            line(slot: 2, x: 0.50, anchors: [0.2, 0.3, 0.4])
        ], at: 10.2)
        // With independent blending, left's interior at y=.2 would pass x=.5,
        // while right's measured first endpoint remains exactly x=.5.
        XCTAssertEqual(smoother.update(current, at: 10.2), current)
    }
}
