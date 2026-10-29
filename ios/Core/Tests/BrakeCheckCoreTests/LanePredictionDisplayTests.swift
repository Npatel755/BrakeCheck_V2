import XCTest
@testable import BrakeCheckCore

final class LanePredictionDisplayTests: XCTestCase {
    private typealias Axis = UFLDv2LaneDecoder.Axis
    private typealias Point = LanePredictionObservation.Point
    private func locations(_ axis: Axis, _ bin: Int, _ anchor: Int, _ slot: Int) -> Double {
        bin == (axis == .row ? 40 + 30 * slot : 30 + anchor / 2) ? 1000 : -1000
    }
    private func existence(count: Int, probability: Double = 0.9, slots: Set<Int> = [1])
        -> (Axis, Int, Int, Int) -> Double {
        { axis, category, anchor, slot in
            let selected = (slot == 1 || slot == 2) ? axis == .row : axis == .column
            guard slots.contains(slot), selected, anchor < count else { return 0 }
            return category == 1 ? log(probability / (1 - probability)) : 0
        }
    }
    private func candidate(indices: [Int], slot: Int = 1, confidence: Double = 0.9)
        -> UFLDv2LaneDecoder.Candidate {
        .init(laneSlot: slot, axis: .row, positiveAnchors: indices.count,
            meanExistenceProbability: confidence, points: indices.map {
                .init(x: 0.3 + Double($0) * 0.002,
                    y: 1 - UFLDv2LaneDecoder.rowAnchors[$0], strength: confidence, anchorIndex: $0)
            })
    }
    private func observation(_ points: [Point], at time: Double = 10) -> LanePredictionObservation {
        .init(capturedAt: time, boundaries: [.init(laneSlot: 1, confidence: 0.9, points: points)])
    }

    func testOneShortBoundaryDisplaysWithoutInventingACorridorOrOppositeBoundary() throws {
        let scores = existence(count: 8)
        let strict = UFLDv2LaneDecoder.decode(location: locations, existence: scores)
        XCTAssertTrue(strict.allSatisfy { $0.peaks.isEmpty })
        var estimator = NeuralLaneEstimator()
        XCTAssertNil(estimator.update(strict, at: 10))

        let candidates = UFLDv2LaneDecoder.displayCandidates(location: locations, existence: scores)
        let display = try XCTUnwrap(LanePredictionDisplay.observation(candidates: candidates, at: 10))
        XCTAssertEqual(display.boundaries.count, 1)
        XCTAssertEqual(display.boundaries[0].laneSlot, 1)
        XCTAssertEqual(display.boundaries[0].points.count, 8)
        XCTAssertEqual(display.boundaries[0].points.first!.x, 70.5 / 199, accuracy: 1e-12)
        XCTAssertEqual(display.boundaries[0].points.last!.y, 1 - UFLDv2LaneDecoder.rowAnchors[7], accuracy: 1e-12)
        XCTAssertEqual(display.capturedAt, 10)
    }

    func testDisplayUsesAllFourTrainedHeadsAndLeavesStockThresholdsIntact() {
        let scores = existence(count: 4, slots: [0, 1, 2, 3])
        let candidates = UFLDv2LaneDecoder.displayCandidates(location: locations, existence: scores)
        XCTAssertEqual(candidates.map(\.laneSlot), [0, 1, 2, 3])
        XCTAssertEqual(candidates.map(\.axis), [.column, .row, .row, .column])
        XCTAssertTrue(UFLDv2LaneDecoder.decodeCandidates(location: locations, existence: scores).isEmpty)
        XCTAssertEqual(UFLDv2LaneDecoder.rowMinimumAnchors, 37)
        XCTAssertEqual(UFLDv2LaneDecoder.columnMinimumAnchors, 21)
        XCTAssertTrue(UFLDv2LaneDecoder.displayCandidates(location: locations,
            existence: { axis, category, anchor, slot in
                let wrong = (slot == 1 || slot == 2) ? axis == .column : axis == .row
                return wrong && category == 1 && anchor < 4 ? 10 : 0
            }).isEmpty)
    }

    func testDisplayRejectsWeakEvidenceAndFewerThanFourValidPoints() {
        XCTAssertTrue(UFLDv2LaneDecoder.displayCandidates(location: locations,
            existence: existence(count: 3)).isEmpty)
        XCTAssertTrue(UFLDv2LaneDecoder.displayCandidates(location: locations,
            existence: existence(count: 4, probability: 0.699)).isEmpty)
        XCTAssertEqual(UFLDv2LaneDecoder.displayCandidates(location: locations,
            existence: existence(count: 4, probability: 0.701)).count, 1)
        XCTAssertTrue(UFLDv2LaneDecoder.displayCandidates(location: { axis, bin, anchor, slot in
            anchor == 2 ? .nan : self.locations(axis, bin, anchor, slot)
        }, existence: existence(count: 4)).isEmpty)
    }

    func testStrictAndDisplayPathsShareTheExactCoordinateMath() throws {
        let scores = existence(count: 40)
        let strict = try XCTUnwrap(UFLDv2LaneDecoder.decodeCandidates(location: locations, existence: scores).first)
        let display = try XCTUnwrap(UFLDv2LaneDecoder.displayCandidates(location: locations, existence: scores).first)
        XCTAssertEqual(strict, display)
    }

    func testLongUnsupportedAnchorGapsSplitWithoutExtrapolating() throws {
        let input = candidate(indices: [2, 3, 4, 5, 20, 21, 22, 23])
        let display = try XCTUnwrap(LanePredictionDisplay.observation(candidates: [input], at: 10))
        XCTAssertEqual(display.boundaries.count, 2)
        XCTAssertEqual(display.boundaries.map(\.laneSlot), [1, 1])
        XCTAssertEqual(display.boundaries.map { $0.points.count }, [4, 4])
        XCTAssertEqual(display.boundaries[0].points.last!.y, input.points[3].y)
        XCTAssertEqual(display.boundaries[1].points.first!.y, input.points[4].y)
        XCTAssertEqual(display.boundaries.flatMap(\.points).count, input.points.count)
    }

    func testSmallAnchorGapsPreserveOnlyObservedSamples() throws {
        let input = candidate(indices: [2, 3, 5, 6])
        let display = try XCTUnwrap(LanePredictionDisplay.observation(candidates: [input], at: 10))
        XCTAssertEqual(display.boundaries.count, 1)
        XCTAssertEqual(display.boundaries[0].points.map(\.y), input.points.map(\.y))
    }

    func testVehicleMaskSplitsPaintRatherThanJoiningAcrossAnOccluder() throws {
        let input = observation((1...7).map { Point(x: 0.5, y: Double($0) / 10) })
        let visible = try XCTUnwrap(LanePredictionDisplay.visible(input, at: 10.1,
            excluding: [CGRect(x: 0.4, y: 0.35, width: 0.2, height: 0.1)]))
        XCTAssertEqual(visible.boundaries.count, 2)
        XCTAssertEqual(visible.boundaries[0].points.map(\.y), [0.1, 0.2, 0.3])
        XCTAssertEqual(visible.boundaries[1].points.map(\.y), [0.5, 0.6, 0.7])
        XCTAssertEqual(visible.capturedAt, input.capturedAt)
    }

    func testVehicleMaskAlsoBlocksASegmentWithBothEndpointsOutsideTheBox() throws {
        let input = observation([0.1, 0.2, 0.8, 0.9].map { Point(x: $0, y: 0.4) })
        let visible = try XCTUnwrap(LanePredictionDisplay.visible(input, at: 10.1,
            excluding: [CGRect(x: 0.4, y: 0.35, width: 0.2, height: 0.1)]))
        XCTAssertEqual(visible.boundaries.count, 2)
        XCTAssertEqual(visible.boundaries.map { $0.points.map(\.x) }, [[0.1, 0.2], [0.8, 0.9]])
    }

    func testCurrentVehicleMasksCanHideAFormerlyVisiblePrediction() throws {
        let input = observation([.init(x: 0.5, y: 0.2), .init(x: 0.5, y: 0.3)])
        XCTAssertNotNil(LanePredictionDisplay.visible(input, at: 10.1))
        XCTAssertNil(LanePredictionDisplay.visible(input, at: 10.2,
            excluding: [CGRect(x: 0.45, y: 0.1, width: 0.1, height: 0.3)]))
        let unmasked = try XCTUnwrap(LanePredictionDisplay.visible(input, at: 10.3))
        XCTAssertEqual(unmasked.capturedAt, 10)
    }

    func testStaleFutureAndNonfiniteTimestampsNeverDisplay() {
        let input = observation([.init(x: 0.3, y: 0.2), .init(x: 0.31, y: 0.3)])
        XCTAssertNotNil(LanePredictionDisplay.visible(input, at: 10.5))
        XCTAssertNil(LanePredictionDisplay.visible(input, at: 10.501))
        XCTAssertNil(LanePredictionDisplay.visible(input, at: 9.99))
        for invalid in [Double.nan, .infinity, -.infinity] {
            XCTAssertNil(LanePredictionDisplay.visible(input, at: invalid))
            XCTAssertNil(LanePredictionDisplay.visible(observation(input.boundaries[0].points, at: invalid), at: 10))
            XCTAssertNil(LanePredictionDisplay.observation(candidates: [candidate(indices: [0, 1, 2, 3])], at: invalid))
        }
    }

    func testInvalidCoordinatesBreakThePolylineInsteadOfConnectingItsNeighbors() throws {
        let input = observation([.init(x: 0.2, y: 0.2), .init(x: 0.3, y: 0.3),
            .init(x: .nan, y: 0.4), .init(x: 0.5, y: 0.5), .init(x: 0.6, y: 0.6)])
        let visible = try XCTUnwrap(LanePredictionDisplay.visible(input, at: 10))
        XCTAssertEqual(visible.boundaries.count, 2)
        XCTAssertEqual(visible.boundaries.map { $0.points.count }, [2, 2])
        XCTAssertTrue(visible.boundaries.flatMap(\.points).allSatisfy { $0.x.isFinite && $0.y.isFinite })
    }

    func testInvalidSlotsAndConfidenceFailClosed() {
        for confidence in [0.69, Double.nan, .infinity, 1.1] {
            XCTAssertNil(LanePredictionDisplay.observation(candidates: [candidate(indices: [0, 1, 2, 3], confidence: confidence)], at: 10))
        }
        XCTAssertNil(LanePredictionDisplay.observation(candidates: [candidate(indices: [0, 1, 2, 3], slot: 4)], at: 10))
        XCTAssertNil(LanePredictionDisplay.visible(.init(capturedAt: 10, boundaries: [
            .init(laneSlot: -1, confidence: 0.9, points: [.init(x: 0.2, y: 0.2), .init(x: 0.3, y: 0.3)])
        ]), at: 10))
    }
}
