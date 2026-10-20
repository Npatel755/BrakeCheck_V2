import XCTest
@testable import BrakeCheckCore

final class StableTopKTests: XCTestCase {
    private struct Candidate: Equatable { let index: Int; let cost: Int; let x: Int }

    func testPatchCandidateAndAlternativeSelectionMatchFullStableSort() {
        var random: UInt64 = 17
        for _ in 0..<100 {
            let candidates = (0..<481).map { index -> Candidate in
                random = random &* 6364136223846793005 &+ 1442695040888963407
                return Candidate(index: index, cost: Int((random >> 32) % 97), x: index % 37 - 18)
            }
            let sorted = candidates.sorted { $0.cost < $1.cost }
            let selected = StableTopK.smallest(candidates, count: 12) { $0.cost < $1.cost }
            XCTAssertEqual(selected, Array(sorted.prefix(12)))
            let bestX = selected[7].x
            let alternatives = candidates.filter { abs($0.x - bestX) >= 4 }
            XCTAssertEqual(StableTopK.smallest(alternatives, count: 4) { $0.cost < $1.cost },
                Array(sorted.filter { abs($0.x - bestX) >= 4 }.prefix(4)))
        }
    }

    func testTiesEdgesAndSmallInputsPreserveInputOrder() {
        let equal = (0..<30).map { Candidate(index: $0, cost: 1, x: 0) }
        XCTAssertEqual(StableTopK.smallest(equal, count: 12) { $0.cost < $1.cost }, Array(equal.prefix(12)))
        XCTAssertEqual(StableTopK.smallest(equal, count: 0) { $0.cost < $1.cost }, [])
        XCTAssertEqual(StableTopK.smallest(equal, count: -1) { $0.cost < $1.cost }, [])
        XCTAssertEqual(StableTopK.smallest(Array(equal.prefix(2)), count: 12) { $0.cost < $1.cost }, Array(equal.prefix(2)))
        XCTAssertEqual(StableTopK.smallest([Int](), count: 12, by: <), [])
    }
}
