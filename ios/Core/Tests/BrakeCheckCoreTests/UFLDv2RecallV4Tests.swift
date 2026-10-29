import XCTest
@testable import BrakeCheckCore

final class UFLDv2RecallV4Tests: XCTestCase {
    private struct Fixture: Decodable {
        struct Sample: Decodable {
            struct Head: Decodable {
                struct Anchor: Decodable {
                    let existence: [Double]
                    let start: Int
                    let local: [Double]
                }
                let axis: String
                let anchors: [Anchor]
            }
            struct Expected: Decodable {
                let slot: Int
                let positiveAnchors: Int
                let probability: Double
                let points: [[Double]]
            }
            let image_id: String
            let heads: [Head]
            let display: [Expected]
            let ego: [Expected]
        }
        let checkpoint_sha256: String
        let samples: [Sample]
    }

    func testEightRealValidationOutputsMatchPythonRecallV4AndStockEgo() throws {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("Fixtures/UFLDv2RecallV4.json")
        let fixture = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: url))
        XCTAssertEqual(fixture.checkpoint_sha256, "4838156c4bd7c237d9189ff617e150f38d96f5018b48ac53e472ea7d4b23f378")
        XCTAssertEqual(fixture.samples.count, 8)
        for sample in fixture.samples {
            let result = UFLDv2LaneDecoder.cameraCandidates(location: { axis, bin, anchor, slot in
                let head = sample.heads[slot]
                XCTAssertEqual(axis, head.axis == "row" ? .row : .column)
                let entry = head.anchors[anchor]
                let index = bin - entry.start
                return entry.local.indices.contains(index) ? entry.local[index] : -10000
            }, existence: { _, state, anchor, slot in
                sample.heads[slot].anchors[anchor].existence[state]
            })
            // Python enumerates row heads before column heads; Swift retains
            // slot order for its camera consumers. Compare by trained identity.
            for (actual, expected) in [(result.display, sample.display.sorted { $0.slot < $1.slot }),
                                      (result.ego, sample.ego.sorted { $0.slot < $1.slot })] {
                XCTAssertEqual(actual.map(\.laneSlot), expected.map(\.slot), sample.image_id)
                for (candidate, target) in zip(actual, expected) {
                    XCTAssertEqual(candidate.positiveAnchors, target.positiveAnchors)
                    XCTAssertEqual(candidate.meanExistenceProbability, target.probability, accuracy: 2e-7)
                    XCTAssertEqual(candidate.points.count, target.points.count)
                    for (point, coordinates) in zip(candidate.points, target.points) {
                        XCTAssertEqual(point.x, coordinates[0], accuracy: 2e-7)
                        XCTAssertEqual(point.y, coordinates[1], accuracy: 2e-7)
                    }
                }
            }
            if sample.image_id == "a2d2-018f2db6dd980480" {
                XCTAssertEqual(result.display.map(\.laneSlot), [1, 2])
                XCTAssertTrue(result.ego.isEmpty)
                let observation = try XCTUnwrap(LanePredictionDisplay.observation(candidates: result.display, at: 10))
                XCTAssertEqual(Set(observation.boundaries.map(\.laneSlot)), [1, 2])
                let filtered = try XCTUnwrap(LanePredictionGeometry.filtered(observation))
                XCTAssertEqual(Set(filtered.boundaries.map(\.laneSlot)), [1, 2])
            }
        }
    }

    func testRecallGeometryRejectsUnsupportedJumpsReversalsAndTooFewPoints() {
        func candidate(_ points: [[Double]], confidence: Double = 0.72) -> UFLDv2LaneDecoder.Candidate {
            .init(laneSlot: 1, axis: .row, positiveAnchors: points.count,
                meanExistenceProbability: confidence, points: points.enumerated().map {
                    .init(x: $0.element[0], y: $0.element[1], strength: confidence, anchorIndex: $0.offset)
                })
        }
        let normal = [[0.3, 0.4], [0.31, 0.42], [0.32, 0.44], [0.33, 0.46]]
        XCTAssertTrue(UFLDv2LaneDecoder.acceptsRecallDisplay(candidate(normal)))
        XCTAssertFalse(UFLDv2LaneDecoder.acceptsRecallDisplay(candidate(normal, confidence: 0.69)))
        XCTAssertFalse(UFLDv2LaneDecoder.acceptsRecallDisplay(candidate(Array(normal.prefix(3)))))
        XCTAssertFalse(UFLDv2LaneDecoder.acceptsRecallDisplay(candidate([[0.1, 0.1], [0.11, 0.12], [0.5, 0.2], [0.51, 0.22]])))
        XCTAssertFalse(UFLDv2LaneDecoder.acceptsRecallDisplay(candidate([[0.3, 0.4], [0.4, 0.41], [0.3, 0.42], [0.4, 0.43]])))
    }
}
