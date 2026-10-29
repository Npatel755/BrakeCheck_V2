import XCTest
@testable import BrakeCheckCore

final class LaneCorridorDisplayTests: XCTestCase {
    private typealias Observation = LanePredictionObservation
    private func boundary(_ slot: Int, rows: [Double] = [0.10, 0.14, 0.18, 0.22, 0.26, 0.30]) -> Observation.Boundary {
        .init(laneSlot: slot, confidence: 0.9, points: rows.map { y in
            let halfWidth = 0.30 - y * 0.65
            let bend = y * y * 0.4
            return .init(x: 0.5 + bend + (slot == 1 ? -halfWidth : halfWidth), y: y)
        })
    }
    private func observation(_ boundaries: [Observation.Boundary], at time: Double = 10) -> Observation {
        .init(capturedAt: time, boundaries: boundaries)
    }

    func testFilledPolygonFollowsBothCurvedBoundariesAndTheirObservedSupport() throws {
        let input = observation([boundary(1), boundary(2)])
        let sections = LaneCorridorDisplay.sections(input, at: 10.1)
        XCTAssertEqual(sections.count, 1)
        let section = try XCTUnwrap(sections.first)
        XCTAssertEqual(section.left, input.boundaries[0].points)
        XCTAssertEqual(section.right, input.boundaries[1].points)
        XCTAssertEqual(section.polygon.count, 12)
        XCTAssertEqual(section.polygon.first, section.left.first)
        XCTAssertEqual(section.polygon.last, section.right.first)
    }

    func testSingleBoundaryAndOuterNeighborsCannotCreateFilledEgoCorridor() {
        for slots in [[1], [2], [0, 3], [0, 2], [1, 3]] {
            XCTAssertTrue(LaneCorridorDisplay.sections(observation(slots.map { boundary($0) }), at: 10).isEmpty)
        }
    }

    func testFreshnessAndInvalidTimesCannotKeepTunnelVisible() {
        let input = observation([boundary(1), boundary(2)])
        for time in [9.9, 10.501, .nan, .infinity] {
            XCTAssertTrue(LaneCorridorDisplay.sections(input, at: time).isEmpty)
        }
        XCTAssertTrue(LaneCorridorDisplay.sections(nil, at: 10).isEmpty)
    }

    func testDisconnectedPaintDoesNotFillAcrossTheMissingSection() {
        let first = [0.1, 0.12, 0.14, 0.16]
        let last = [0.3, 0.32, 0.34, 0.36]
        let input = observation([boundary(1, rows: first), boundary(1, rows: last),
                                 boundary(2, rows: first + last)])
        let sections = LaneCorridorDisplay.sections(input, at: 10)
        XCTAssertEqual(sections.count, 2)
        XCTAssertLessThanOrEqual(sections[0].left.last!.y, 0.16)
        XCTAssertGreaterThanOrEqual(sections[1].left.first!.y, 0.30)
    }

    func testOnlyOverlappingSpanCanBeFilled() {
        let input = observation([boundary(1, rows: [0.10, 0.14, 0.18, 0.22, 0.26]),
                                 boundary(2, rows: [0.18, 0.22, 0.26, 0.30])])
        let sections = LaneCorridorDisplay.sections(input, at: 10)
        XCTAssertEqual(sections.count, 1)
        XCTAssertEqual(sections.first?.left.first?.y, 0.18)
        XCTAssertEqual(sections.first?.left.last?.y, 0.26)
    }

    func testReversedPointsAreSortedButDuplicateRowsAndCrossedSidesAreRejected() {
        let l = boundary(1), r = boundary(2)
        let reverse = Observation.Boundary(laneSlot: 1, confidence: 0.9, points: l.points.reversed())
        XCTAssertEqual(LaneCorridorDisplay.sections(observation([reverse, r]), at: 10).count, 1)
        let crossedLeft = Observation.Boundary(laneSlot: 1, confidence: 0.9, points: r.points)
        let crossedRight = Observation.Boundary(laneSlot: 2, confidence: 0.9, points: l.points)
        XCTAssertTrue(LaneCorridorDisplay.sections(observation([crossedLeft, crossedRight]), at: 10).isEmpty)
        let duplicate = Observation.Boundary(laneSlot: 1, confidence: 0.9, points: l.points + [l.points[0]])
        XCTAssertTrue(LaneCorridorDisplay.sections(observation([duplicate, r]), at: 10).isEmpty)
    }

    func testAmbiguousBranchesCannotPaintArbitraryLane() {
        let input = observation([boundary(1), boundary(1), boundary(2)])
        XCTAssertTrue(LaneCorridorDisplay.sections(input, at: 10).isEmpty)
    }
}
