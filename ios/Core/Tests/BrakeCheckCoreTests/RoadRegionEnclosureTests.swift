import XCTest
@testable import BrakeCheckCore

final class RoadRegionEnclosureTests: XCTestCase {
    private func road(left: Double = 0.10, right: Double = 0.90, confidence: Double = 0.9, at time: Double = 10) -> RoadRegion {
        RoadRegion(bands: (0..<20).map { LaneBand(y: 0.05 + Double($0) * 0.03, left: left, right: right) },
                   confidence: confidence, capturedAt: time)
    }
    private func lane(left: Double, right: Double, rows: Int = 12, at time: Double = 10.2) -> LanePath {
        LanePath(bands: (0..<rows).map { LaneBand(y: 0.08 + Double($0) * 0.03, left: left, right: right) },
                 confidence: 0.9, capturedAt: time, source: .neural)
    }

    func testCorridorOnTheRoadIsEnclosedAndOneFittedToTheKerbIsNot() {
        XCTAssertEqual(road().encloses(lane(left: 0.35, right: 0.65), at: 10.2), true)
        XCTAssertEqual(road().encloses(lane(left: 0.12, right: 0.42), at: 10.2), true, "edge lane within tolerance")
        XCTAssertEqual(road().encloses(lane(left: 0.02, right: 0.30), at: 10.2), false, "left edge 8% off the road")
        XCTAssertEqual(road().encloses(lane(left: 0.70, right: 0.99), at: 10.2), false)
    }

    func testMaskThatCannotJudgeReturnsNil() {
        XCTAssertNil(road(confidence: 0.7).encloses(lane(left: 0.02, right: 0.30), at: 10.2), "low quality mask")
        XCTAssertNil(road().encloses(lane(left: 0.02, right: 0.30), at: 10.8), "stale mask")
        XCTAssertNil(road().encloses(lane(left: 0.02, right: 0.30, rows: 3), at: 10.2), "too few shared rows")
        let highLane = LanePath(bands: (0..<8).map { LaneBand(y: 0.70 + Double($0) * 0.03, left: 0.0, right: 0.3) },
                                confidence: 0.9, capturedAt: 10.2, source: .neural)
        XCTAssertNil(road().encloses(highLane, at: 10.2), "rows the mask never observed do not count")
    }

    func testMinorityOfOutsideRowsIsTolerated() {
        var bands = (0..<10).map { LaneBand(y: 0.08 + Double($0) * 0.03, left: 0.35, right: 0.65) }
        bands[0] = LaneBand(y: bands[0].y, left: 0.01, right: 0.65)
        bands[1] = LaneBand(y: bands[1].y, left: 0.01, right: 0.65)
        let lane = LanePath(bands: bands, confidence: 0.9, capturedAt: 10.2, source: .neural)
        XCTAssertEqual(road().encloses(lane, at: 10.2), true)
        bands[2] = LaneBand(y: bands[2].y, left: 0.01, right: 0.65)
        bands[3] = LaneBand(y: bands[3].y, left: 0.01, right: 0.65)
        XCTAssertEqual(road().encloses(LanePath(bands: bands, confidence: 0.9, capturedAt: 10.2, source: .neural), at: 10.2), false)
    }
}
