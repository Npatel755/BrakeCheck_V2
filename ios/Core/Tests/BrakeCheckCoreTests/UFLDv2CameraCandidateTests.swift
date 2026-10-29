import XCTest
@testable import BrakeCheckCore

final class UFLDv2CameraCandidateTests: XCTestCase {
    private typealias Axis = UFLDv2LaneDecoder.Axis
    private func location(_ axis: Axis, _ bin: Int, _ anchor: Int, _ slot: Int) -> Double {
        let center = axis == .row ? 40 + 30 * slot : 20 + anchor / 2
        return bin == center ? 10 : -10
    }
    private func existence(_ counts: [Int], probability: Double)
        -> (Axis, Int, Int, Int) -> Double {
        { axis, state, anchor, slot in
            let selected = slot == 1 || slot == 2 ? axis == .row : axis == .column
            guard selected, anchor < counts[slot] else { return 0 }
            return state == 1 ? log(probability / (1 - probability)) : 0
        }
    }

    func testSharedCameraDecodePreservesBothExistingSelectionPoliciesExactly() {
        for counts in [[81, 72, 72, 81], [4, 36, 37, 20], [21, 3, 4, 4], [0, 0, 0, 0]] {
            for probability in [0.51, 0.749, 0.751, 0.95] {
                let scores = existence(counts, probability: probability)
                let shared = UFLDv2LaneDecoder.cameraCandidates(location: location, existence: scores)
                XCTAssertEqual(shared.ego, UFLDv2LaneDecoder.decodeCandidates(egoOnly: true,
                    location: location, existence: scores))
                XCTAssertEqual(shared.display, UFLDv2LaneDecoder.displayCandidates(
                    location: location, existence: scores))
            }
        }
    }

    func testCameraReadsEverySelectedTensorLocationOnlyOnce() {
        var locationReads = 0
        var existenceReads = 0
        let scores = existence([81, 72, 72, 81], probability: 0.9)
        let result = UFLDv2LaneDecoder.cameraCandidates(location: { axis, bin, anchor, slot in
            locationReads += 1
            return self.location(axis, bin, anchor, slot)
        }, existence: { axis, state, anchor, slot in
            existenceReads += 1
            return scores(axis, state, anchor, slot)
        })
        XCTAssertEqual(locationReads, 2 * 72 * 200 + 2 * 81 * 100)
        XCTAssertEqual(existenceReads, 2 * (2 * 72 + 2 * 81))
        XCTAssertEqual(result.ego.map(\.laneSlot), [1, 2])
        XCTAssertEqual(result.display.map(\.laneSlot), [0, 1, 2, 3])
    }

    func testRelaxedShortBoundaryNeverEntersStockEgoCandidates() {
        let result = UFLDv2LaneDecoder.cameraCandidates(location: location,
            existence: existence([0, 32, 0, 0], probability: 0.92))
        XCTAssertTrue(result.ego.isEmpty)
        XCTAssertEqual(result.display.map(\.laneSlot), [1])
    }
}
