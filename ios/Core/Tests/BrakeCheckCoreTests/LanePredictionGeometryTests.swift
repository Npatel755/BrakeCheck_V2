import XCTest
@testable import BrakeCheckCore

final class LanePredictionGeometryTests: XCTestCase {
    private typealias Point = LanePredictionObservation.Point
    private func observation(_ coordinates: [Double], slot: Int = 1) -> LanePredictionObservation {
        let points = coordinates.enumerated().map { index, coordinate -> Point in
            let anchor = 0.1 + Double(index) * 0.01
            return slot == 1 || slot == 2 ? .init(x: coordinate, y: anchor) : .init(x: anchor, y: coordinate)
        }
        return .init(capturedAt: 10, boundaries: [.init(laneSlot: slot, confidence: 0.9, points: points)])
    }

    func testSevereZigzagIsRejectedInsteadOfStraightenedIntoAConfidentLine() {
        let input = observation([0.3, 0.38, 0.31, 0.39, 0.30, 0.38, 0.31, 0.39])
        XCTAssertNil(LanePredictionGeometry.filtered(input))
        XCTAssertNil(LanePredictionGeometry.filtered(observation([0.3, 0.50, 0.31, 0.51])))
    }

    func testColumnHeadZigzagIsCheckedOnItsOwnObservedAxis() {
        XCTAssertNil(LanePredictionGeometry.filtered(observation([0.3, 0.38, 0.31, 0.39, 0.30, 0.38], slot: 3)))
    }

    func testSmallSpatialJitterIsReducedWithoutChangingSupportOrTimestamp() throws {
        let input = observation([0.3, 0.305, 0.301, 0.306, 0.302, 0.307, 0.303])
        let output = try XCTUnwrap(LanePredictionGeometry.filtered(input))
        XCTAssertEqual(output.capturedAt, input.capturedAt)
        let before = input.boundaries[0], after = output.boundaries[0]
        XCTAssertEqual(after.points.count, before.points.count)
        XCTAssertEqual(after.points.first, before.points.first)
        XCTAssertEqual(after.points.last, before.points.last)
        XCTAssertEqual(after.points.map(\.y), before.points.map(\.y))
        XCTAssertEqual(after.confidence, before.confidence)
        XCTAssertLessThan(after.points[1].x, before.points[1].x)
        XCTAssertLessThanOrEqual(zip(after.points, before.points).map { abs($0.x - $1.x) }.max()!, 0.006)
    }

    func testSmoothBendAndSingleBoundaryRemainSingleAndUnextended() throws {
        let input = observation((0..<24).map { 0.25 + pow(Double($0) / 23, 2) * 0.12 })
        XCTAssertEqual(try XCTUnwrap(LanePredictionGeometry.filtered(input)), input)
        XCTAssertEqual(LanePredictionGeometry.filtered(input)?.boundaries.count, 1)
    }

    func testSeparateFragmentsNeverGetJoinedAndBadFragmentDoesNotHideGoodSide() throws {
        let good = observation([0.3, 0.31, 0.32, 0.33])
        let bad = observation([0.5, 0.58, 0.51, 0.59, 0.5], slot: 2)
        let another = observation([0.3, 0.31, 0.32, 0.33], slot: 0)
        let combined = LanePredictionObservation(capturedAt: 10,
            boundaries: good.boundaries + bad.boundaries + another.boundaries)
        let output = try XCTUnwrap(LanePredictionGeometry.filtered(combined))
        XCTAssertEqual(output.boundaries.map(\.laneSlot), [1, 0])
        XCTAssertEqual(output.boundaries[0], good.boundaries[0])
    }

    func testReversingOrDuplicateAnchorAxesAndInvalidTimesFailClosed() {
        let input = observation([0.3, 0.31, 0.32, 0.33])
        let boundary = input.boundaries[0]
        let scrambled = [boundary.points[0], boundary.points[2], boundary.points[1], boundary.points[3]]
        XCTAssertNil(LanePredictionGeometry.filtered(.init(capturedAt: 10,
            boundaries: [.init(laneSlot: 1, confidence: 0.9, points: scrambled)])))
        XCTAssertNil(LanePredictionGeometry.filtered(.init(capturedAt: .nan, boundaries: input.boundaries)))
        XCTAssertNil(LanePredictionGeometry.filtered(nil))
    }

    func testActualV5CandidatesKeepNormalPairAndRejectRecordedZigzag() throws {
        struct Fixture: Decodable {
            struct Frame: Decodable {
                struct Candidate: Decodable {
                    struct Point: Decodable {
                        let x, y, strength: Double
                        let anchorIndex: Int
                    }
                    let laneSlot, positiveAnchors: Int
                    let axis: String
                    let meanExistenceProbability: Double
                    let points: [Point]
                }
                let capturedAt: Double
                let candidates: [Candidate]
            }
            let checkpointSHA256: String
            let frames: [Frame]
        }
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("Fixtures/V5LaneGeometry.json")
        let fixture = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: url))
        XCTAssertEqual(fixture.checkpointSHA256, "2700da1e5b3956a732e652b12632174ae4013360fd1dbf8e4694d599afaca594")
        for frame in fixture.frames {
            let candidates = frame.candidates.map { candidate in
                UFLDv2LaneDecoder.Candidate(laneSlot: candidate.laneSlot,
                    axis: candidate.axis == "row" ? .row : .column,
                    positiveAnchors: candidate.positiveAnchors,
                    meanExistenceProbability: candidate.meanExistenceProbability,
                    points: candidate.points.map { .init(x: $0.x, y: $0.y,
                        strength: $0.strength, anchorIndex: $0.anchorIndex) })
            }
            let observed = try XCTUnwrap(LanePredictionDisplay.observation(candidates: candidates, at: frame.capturedAt))
            let filtered = LanePredictionGeometry.filtered(observed)
            if frame.capturedAt == 75 {
                let good = try XCTUnwrap(filtered)
                XCTAssertEqual(good.boundaries.map(\.laneSlot), [1, 2])
                for (before, after) in zip(observed.boundaries, good.boundaries) {
                    XCTAssertEqual(after.points.first, before.points.first)
                    XCTAssertEqual(after.points.last, before.points.last)
                    XCTAssertEqual(after.points.map(\.y), before.points.map(\.y))
                }
            } else {
                XCTAssertEqual(frame.capturedAt, 124.75)
                XCTAssertNil(filtered)
            }
        }
    }
}
