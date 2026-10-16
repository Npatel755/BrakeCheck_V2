import XCTest
@testable import BrakeCheckCore

final class CommaLaneMarkingDecoderTests: XCTestCase {
    func testTwoObservedPaintRunsProduceTwoLaneCandidates() {
        let rows = CommaLaneMarkingDecoder.decode { x, _ in
            (156...162).contains(x) || (346...352).contains(x) ? 0.92 : 0.03
        }
        XCTAssertEqual(rows.count, 24)
        XCTAssertTrue(rows.allSatisfy { $0.peaks.count == 2 })
        XCTAssertTrue(rows[0].peaks.contains { abs($0.x - 159.0 / 511.0) < 0.001 })
    }

    func testWidePaintAndVehicleOcclusionDoNotBecomeBoundaries() {
        let box = CGRect(x: 0.27, y: 0, width: 0.09, height: 0.7)
        let rows = CommaLaneMarkingDecoder.decode(excluding: [box]) { x, _ in
            (156...162).contains(x) || (250...300).contains(x) ? 0.92 : 0.03
        }
        XCTAssertTrue(rows.allSatisfy { $0.peaks.isEmpty })
    }
}
