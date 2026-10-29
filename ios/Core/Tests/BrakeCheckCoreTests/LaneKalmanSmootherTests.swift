import XCTest
@testable import BrakeCheckCore

final class LaneKalmanSmootherTests: XCTestCase {
    private typealias Observation = LanePredictionObservation
    private func boundary(_ shift: Double = 0, slot: Int = 1, start: Int = 0, end: Int = 16) -> Observation.Boundary {
        let points = (start...end).map { index -> Observation.Point in
            let anchor = 0.10 + Double(index) * 0.02
            return slot == 1 || slot == 2
                ? .init(x: 0.30 + shift + 0.1 * anchor * anchor, y: anchor)
                : .init(x: anchor, y: 0.30 + shift + 0.1 * anchor * anchor)
        }
        return .init(laneSlot: slot, confidence: 0.9, points: points)
    }
    private func input(_ time: Double, shift: Double = 0, slot: Int = 1) -> Observation {
        .init(capturedAt: time, boundaries: [boundary(shift, slot: slot)])
    }

    func testReducesJitterAtTheActualThreeHzPhoneCadence() throws {
        var filter = LaneKalmanSmoother()
        var raw: [Double] = [], smoothed: [Double] = []
        for index in 0..<30 {
            let shift = index.isMultiple(of: 2) ? 0.008 : -0.008
            let current = input(Double(index) / 3, shift: shift)
            let result = try XCTUnwrap(filter.update(current, at: current.capturedAt + 0.04))
            XCTAssertEqual(result.capturedAt, current.capturedAt)
            XCTAssertEqual(result.boundaries[0].points.map(\.y), current.boundaries[0].points.map(\.y))
            XCTAssertEqual(result.boundaries[0].confidence, current.boundaries[0].confidence)
            if index > 3 {
                raw.append(current.boundaries[0].points[8].x)
                smoothed.append(result.boundaries[0].points[8].x)
            }
        }
        func changes(_ points: [Double]) -> Double {
            zip(points, points.dropFirst()).reduce(0) { $0 + abs($1.1 - $1.0) }
        }
        XCTAssertLessThan(changes(smoothed), changes(raw) * 0.85)
    }

    func testMissingLaneClearsImmediatelyAndCannotBePredictedIntoDisplay() throws {
        var filter = LaneKalmanSmoother()
        _ = filter.update(input(10), at: 10)
        XCTAssertNil(filter.update(nil, at: 10.1))
        let fresh = input(10.2, shift: 0.02)
        XCTAssertEqual(filter.update(fresh, at: 10.2), fresh)
    }

    func testDuplicateCapturesDoNotUpdateFilterOrRenewTimestamp() throws {
        var filter = LaneKalmanSmoother()
        _ = filter.update(input(10), at: 10)
        let current = input(10.2, shift: 0.02)
        let result = try XCTUnwrap(filter.update(current, at: 10.25))
        XCTAssertEqual(filter.update(current, at: 10.4), result)
        XCTAssertEqual(result.capturedAt, 10.2)
        XCTAssertNil(filter.update(current, at: 10.71))
    }

    func testBackwardFutureNonfiniteAndLongGapStartFresh() {
        for bad in [input(9), input(11), input(.nan)] {
            var filter = LaneKalmanSmoother()
            _ = filter.update(input(10), at: 10)
            XCTAssertNil(filter.update(bad, at: 10.1))
            let next = input(10.2, shift: 0.02)
            XCTAssertEqual(filter.update(next, at: 10.2), next)
        }
        var filter = LaneKalmanSmoother()
        _ = filter.update(input(10), at: 10)
        let next = input(11, shift: 0.02)
        XCTAssertEqual(filter.update(next, at: 11), next)
    }

    func testSceneJumpIsShownFreshInsteadOfDraggedTowardOldRoad() {
        var filter = LaneKalmanSmoother()
        _ = filter.update(input(10), at: 10)
        let changed = input(10.2, shift: 0.25)
        XCTAssertEqual(filter.update(changed, at: 10.2), changed)
    }

    func testReorderedAndRelabeledRowSlotsAssociateByGeometry() throws {
        var filter = LaneKalmanSmoother()
        _ = filter.update(.init(capturedAt: 10, boundaries: [boundary(), boundary(0.35, slot: 2)]), at: 10)
        let current = Observation(capturedAt: 10.2, boundaries: [boundary(0.36, slot: 1), boundary(0.01, slot: 2)])
        let output = try XCTUnwrap(filter.update(current, at: 10.2))
        XCTAssertEqual(output.boundaries.map(\.laneSlot), [1, 2])
        XCTAssertLessThan(output.boundaries[1].points[8].x, current.boundaries[1].points[8].x)
        XCTAssertGreaterThan(output.boundaries[0].points[8].x, 0.65)
    }

    func testNewSupportDoesNotFillGapsOrExtendEndpoints() throws {
        var filter = LaneKalmanSmoother()
        _ = filter.update(input(10), at: 10)
        let current = Observation(capturedAt: 10.2, boundaries: [boundary(0.01, start: 2, end: 5), boundary(0.01, start: 11, end: 14)])
        let output = try XCTUnwrap(filter.update(current, at: 10.2))
        XCTAssertEqual(output.boundaries.count, 2)
        XCTAssertEqual(output.boundaries.map { $0.points.map(\.y) }, current.boundaries.map { $0.points.map(\.y) })
    }

    func testColumnHeadsKeepTheObservedXAnchorsAndCannotBorrowRowHistory() throws {
        var filter = LaneKalmanSmoother()
        _ = filter.update(input(10, slot: 3), at: 10)
        let current = input(10.2, shift: 0.01, slot: 3)
        let output = try XCTUnwrap(filter.update(current, at: 10.2))
        XCTAssertEqual(output.boundaries[0].points.map(\.x), current.boundaries[0].points.map(\.x))
        XCTAssertLessThan(output.boundaries[0].points[8].y, current.boundaries[0].points[8].y)
        let row = input(10.4, shift: 0.01)
        XCTAssertEqual(filter.update(row, at: 10.4), row)
    }

    func testAmbiguousOldBranchesUseFreshObservation() {
        var filter = LaneKalmanSmoother()
        _ = filter.update(.init(capturedAt: 10, boundaries: [boundary(), boundary(0.002)]), at: 10)
        let current = input(10.2, shift: 0.001)
        XCTAssertEqual(filter.update(current, at: 10.2), current)
    }

    func testExplicitResetAndConflictingDuplicateStartFresh() {
        var filter = LaneKalmanSmoother()
        _ = filter.update(input(10), at: 10)
        let conflict = input(10, shift: 0.01)
        XCTAssertEqual(filter.update(conflict, at: 10), conflict)
        filter.reset()
        let next = input(10.2, shift: 0.02)
        XCTAssertEqual(filter.update(next, at: 10.2), next)
    }

    func testDefaultModeLabelsCoverAllPhoneChoices() {
        XCTAssertEqual(LaneTrackingMode.allCases.map(\.rawValue), ["raw", "averaging", "kalman"])
        XCTAssertEqual(LaneTrackingMode.kalman.title, "Kalman")
    }
}
