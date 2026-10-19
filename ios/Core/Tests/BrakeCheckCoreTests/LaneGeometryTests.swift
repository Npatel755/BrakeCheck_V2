import XCTest
@testable import BrakeCheckCore

final class LaneGeometryTests: XCTestCase {
    private func rows(bend: Double = 0, dashed: Bool = false, onlyLeft: Bool = false) -> [LaneMarkingRow] {
        (0..<20).map { i in
            let y = 0.06 + Double(i) * 0.024
            let half = LeadCorridor().halfWidth(at: y)!
            let center = 0.5 + bend * pow(y / 0.55, 2)
            var peaks: [LanePeak] = []
            if !dashed || i % 2 == 0 { peaks.append(LanePeak(x: center - half, strength: 0.9)) }
            if !onlyLeft && (!dashed || i % 2 == 1) { peaks.append(LanePeak(x: center + half, strength: 0.9)) }
            return LaneMarkingRow(y: y, peaks: peaks)
        }
    }
    func testTwoFramesRequiredForLaneEstimate() throws {
        var e = LaneEstimator()
        XCTAssertNil(e.update(rows(), at: 0))
        XCTAssertNotNil(e.update(rows(), at: 0.2))
    }
    func testBothDirectionsOfCurveFit() throws {
        for bend in [-0.20, 0.20] {
            var e = LaneEstimator()
            _ = e.update(rows(bend: bend), at: 0)
            let lane = try XCTUnwrap(e.update(rows(bend: bend), at: 0.2))
            let bounds = try XCTUnwrap(lane.bounds(at: 0.48))
            XCTAssertEqual((bounds.left + bounds.right) / 2, 0.5 + bend * pow(0.48 / 0.55, 2), accuracy: 0.015)
        }
    }
    func testDashedBoundariesFitIndependently() throws {
        var e = LaneEstimator()
        _ = e.update(rows(bend: 0.12, dashed: true), at: 0)
        let lane = try XCTUnwrap(e.update(rows(bend: 0.12, dashed: true), at: 0.2))
        XCTAssertNotNil(lane.bounds(at: 0.4))
    }
    func testOneBoundaryDoesNotInventLaneWidth() {
        var e = LaneEstimator()
        for i in 0...4 { XCTAssertNil(e.update(rows(onlyLeft: true), at: Double(i) * 0.2)) }
    }
    func testBriefOcclusionHoldsThenExpiresLane() throws {
        var e = LaneEstimator()
        _ = e.update(rows(), at: 0)
        let original = try XCTUnwrap(e.update(rows(), at: 0.2))
        let held = try XCTUnwrap(e.update([], at: 0.4))
        XCTAssertEqual(held.capturedAt, original.capturedAt)
        XCTAssertLessThan(held.confidence, original.confidence)
        XCTAssertNil(e.update([], at: 0.8))
    }
    func testReappearanceAfterStaleLaneRequiresConfirmationAgain() {
        var e = LaneEstimator()
        _ = e.update(rows(), at: 0)
        _ = e.update(rows(), at: 0.2)
        XCTAssertNil(e.update([], at: 0.8))
        XCTAssertNil(e.update(rows(), at: 1))
        XCTAssertNotNil(e.update(rows(), at: 1.2))
    }
    func testSparsePaintAndFlatBrightnessDoNotCreateLane() {
        var e = LaneEstimator()
        let sparse = Array(rows().prefix(3))
        XCTAssertNil(e.update(sparse, at: 0))
        XCTAssertNil(e.update(sparse, at: 0.2))
        let scan = LaneMarkingScanner.scan(width: 320, height: 180, excluding: [],
                                          luma: { _, _ in 185 }, chroma: { _, _ in (128, 128) })
        XCTAssertTrue(scan.allSatisfy { $0.peaks.isEmpty })
    }
    func testScannerAndEstimatorRecognizeCurvedWhiteAndYellowPaint() throws {
        for yellow in [false, true] {
            let scan = LaneMarkingScanner.scan(width: 320, height: 180, excluding: [],
                luma: { x, rawY in
                    let y = 1 - Double(rawY) / 179
                    guard let half = LeadCorridor().halfWidth(at: y) else { return 65 }
                    let center = 0.5 + 0.18 * pow(y / 0.55, 2)
                    let nx = Double(x) / 319
                    return min(abs(nx - center + half), abs(nx - center - half)) < 0.006 ? 220 : 65
                }, chroma: { _, _ in yellow ? (90, 155) : (128, 128) })
            XCTAssertGreaterThan(scan.filter { $0.peaks.count >= 2 }.count, 8)
            var e = LaneEstimator()
            _ = e.update(scan, at: 0)
            let lane = try XCTUnwrap(e.update(scan, at: 0.2))
            let bounds = try XCTUnwrap(lane.bounds(at: 0.45))
            XCTAssertEqual((bounds.left + bounds.right) / 2, 0.5 + 0.18 * pow(0.45 / 0.55, 2), accuracy: 0.03)
        }
    }
    func testVehicleMaskExcludesBrightVehiclePixels() {
        let scan = LaneMarkingScanner.scan(width: 320, height: 180,
            excluding: [CGRect(x: 0, y: 0, width: 1, height: 1)],
            luma: { x, _ in x % 20 < 4 ? 230 : 60 }, chroma: { _, _ in (128, 128) })
        XCTAssertTrue(scan.allSatisfy { $0.peaks.isEmpty })
    }
    func testOutlierDoesNotDistortCurveFit() throws {
        var input = rows(bend: 0.14)
        let y = input[9].y
        input[9] = LaneMarkingRow(y: y, peaks: [
            LanePeak(x: input[9].peaks[0].x + 0.065, strength: 0.9), input[9].peaks[1]])
        var e = LaneEstimator()
        _ = e.update(input, at: 0)
        let lane = try XCTUnwrap(e.update(input, at: 0.2))
        let bounds = try XCTUnwrap(lane.bounds(at: 0.4))
        XCTAssertEqual((bounds.left + bounds.right) / 2, 0.5 + 0.14 * pow(0.4 / 0.55, 2), accuracy: 0.015)
    }
    func testInvalidLaneCoordinatesAndTimeAreNotUsable() {
        let valid = rows().map { row in LaneBand(y: row.y, left: row.peaks[0].x, right: row.peaks[1].x) }
        XCTAssertFalse(LanePath(bands: valid, confidence: .nan, capturedAt: 0).isUsable(at: 0))
        XCTAssertFalse(LanePath(bands: valid, confidence: 0.8, capturedAt: 0).isUsable(at: 0.6))
        XCTAssertFalse(LanePath(bands: valid, confidence: 0.8, capturedAt: 0).isUsable(at: -1))
        var e = LaneEstimator()
        XCTAssertNil(e.update(rows(), at: .nan))
    }
    func testBoundsDoNotExtrapolateBeyondObservedRoad() throws {
        var e = LaneEstimator()
        _ = e.update(rows(), at: 0)
        let lane = try XCTUnwrap(e.update(rows(), at: 0.2))
        XCTAssertNil(lane.bounds(at: 0.01))
        XCTAssertNil(lane.bounds(at: 0.60))
    }
    func testAspectFillCropsVerticallyOnWideLandscapePhone() {
        let view = CGSize(width: 844, height: 390)
        let frame = CameraGeometry.aspectFillRect(in: view)
        XCTAssertEqual(frame.width, 844, accuracy: 0.001)
        XCTAssertGreaterThan(frame.height, view.height)
        XCTAssertLessThan(frame.minY, 0)
        XCTAssertEqual(frame.midX, view.width / 2, accuracy: 0.001)
        XCTAssertEqual(frame.midY, view.height / 2, accuracy: 0.001)
    }
    func testAspectFillCropsHorizontallyOnTallerViewport() {
        let view = CGSize(width: 600, height: 420)
        let frame = CameraGeometry.aspectFillRect(in: view)
        XCTAssertEqual(frame.height, view.height, accuracy: 0.001)
        XCTAssertLessThan(frame.minX, 0)
        XCTAssertEqual(frame.width / frame.height, 16.0 / 9.0, accuracy: 0.001)
        XCTAssertEqual(CameraGeometry.aspectFillRect(in: .zero), .zero)
    }
}
