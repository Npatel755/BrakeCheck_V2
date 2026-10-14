import XCTest
@testable import BrakeCheckCore

final class CameraNavigationTests: XCTestCase {
    private func lane(at time: Double = 10) -> LanePath {
        LanePath(bands: (0..<25).map { index in
            let y = 0.10 + Double(index) * 0.02
            let half = 0.45 * (0.70 - y)
            return LaneBand(y: y, left: 0.5 - half, right: 0.5 + half)
        }, confidence: 0.95, capturedAt: time, source: .neural)
    }
    private func road(at time: Double = 10) -> RoadRegion {
        RoadRegion(bands: (0..<30).map { index in
            LaneBand(y: 0.05 + Double(index) * 0.02, left: 0.05, right: 0.95)
        }, confidence: 0.95, capturedAt: time)
    }
    private func route(bend: Double = 0) -> [NavigationCoordinate] {
        [NavigationCoordinate(latitude: 40, longitude: -74),
         NavigationCoordinate(latitude: 40 + 20 / 111_320.0, longitude: -74),
         NavigationCoordinate(latitude: 40 + 70 / 111_320.0, longitude: -74 + bend / (111_320 * cos(40 * .pi / 180)))]
    }
    private func project(bend: Double = 0, lane: LanePath? = nil, road: RoadRegion? = nil,
                         boxes: [CGRect] = [], calibration: CameraRouteCalibration = .init()) -> [[LaneBand]] {
        CameraRouteProjection.project(route: route(bend: bend), progressFraction: 0,
            courseDegrees: 0, horizontalFOV: 60, aspect: 16.0 / 9,
            calibration: calibration, lane: lane ?? self.lane(), road: road ?? self.road(),
            excluding: boxes, at: 10)
    }
    func testUFLDNoLaneClassSuppressesEveryPeak() {
        let rows = UFLDLaneDecoder.decode { bin, _, _ in bin == 200 ? 1000 : -1000 }
        XCTAssertEqual(rows.count, 18)
        XCTAssertTrue(rows.allSatisfy { $0.peaks.isEmpty })
    }
    func testUFLDGridAndAnchorCoordinatesAndFiniteSoftmax() throws {
        let rows = UFLDLaneDecoder.decode { bin, _, _ in bin == 99 ? 1000 : -1000 }
        XCTAssertEqual(rows.first!.y, 1 - 287.0 / 288, accuracy: 1e-10)
        XCTAssertEqual(rows.last!.y, 1 - 121.0 / 288, accuracy: 1e-10)
        XCTAssertEqual(rows[5].peaks.count, 4)
        XCTAssertEqual(rows[5].peaks[0].x, (100.0 * 799 / 199 - 1) / 800, accuracy: 1e-10)
        XCTAssertEqual(rows[5].peaks[0].strength, 1, accuracy: 1e-10)
    }
    func testUFLDRejectsNaNsAndVehiclePaint() {
        XCTAssertTrue(UFLDLaneDecoder.decode { _, _, _ in .nan }.allSatisfy { $0.peaks.isEmpty })
        let rows = UFLDLaneDecoder.decode(excluding: [CGRect(x: 0.3, y: 0, width: 0.4, height: 1)]) {
            bin, _, _ in bin == 99 ? 100 : -100
        }
        XCTAssertTrue(rows.allSatisfy { $0.peaks.isEmpty })
    }
    func testStraightRouteStaysCenteredOnMeasuredPavement() {
        let bands = project().flatMap { $0 }
        XCTAssertGreaterThan(bands.count, 20)
        for band in bands {
            XCTAssertEqual((band.left + band.right) / 2, 0.5, accuracy: 0.001)
            XCTAssertGreaterThanOrEqual(band.left, 0.05)
            XCTAssertLessThanOrEqual(band.right, 0.95)
        }
        XCTAssertGreaterThan(bands.first!.right - bands.first!.left, bands.last!.right - bands.last!.left)
    }
    func testUpcomingTurnsNeverInventUnobservedDrivingCorridors() {
        for bend in [-15.0, 15.0] {
            let bands = project(bend: bend).flatMap { $0 }
            XCTAssertFalse(bands.isEmpty)
            for band in bands {
                XCTAssertEqual((band.left + band.right) / 2, 0.5, accuracy: 0.001)
                XCTAssertLessThanOrEqual(band.y, lane().bands.last!.y)
            }
        }
    }
    func testStaleRoadOrLaneImmediatelyHidesRoute() {
        XCTAssertTrue(project(lane: lane(at: 9)).isEmpty)
        XCTAssertTrue(project(road: road(at: 9)).isEmpty)
    }
    func testNoLaneOrRoadDoesNotInventGeometry() {
        XCTAssertTrue(CameraRouteProjection.project(route: route(), progressFraction: 0, courseDegrees: 0,
            horizontalFOV: 60, aspect: 1.7, calibration: .init(), lane: nil, road: road(), at: 10).isEmpty)
        XCTAssertTrue(CameraRouteProjection.project(route: route(), progressFraction: 0, courseDegrees: 0,
            horizontalFOV: 60, aspect: 1.7, calibration: .init(), lane: lane(), road: nil, at: 10).isEmpty)
    }
    func testVehicleOcclusionSplitsRibbonWithoutBridging() {
        let groups = project(boxes: [CGRect(x: 0.35, y: 0.35, width: 0.3, height: 0.10)])
        XCTAssertGreaterThanOrEqual(groups.count, 2)
        XCTAssertTrue(groups.flatMap { $0 }.allSatisfy { $0.y < 0.34 || $0.y > 0.46 })
    }
    func testInvalidMountAndCourseDisagreementSuppressProjection() {
        XCTAssertTrue(project(calibration: .init(heightMeters: .nan)).isEmpty)
        XCTAssertTrue(CameraRouteProjection.project(route: route(), progressFraction: 0, courseDegrees: 180,
            horizontalFOV: 60, aspect: 1.7, calibration: .init(), lane: lane(), road: road(), at: 10).isEmpty)
    }
    func testSmoothingClearsOnObservationLossAndRespectsCurrentBounds() {
        var smoother = CameraRouteSmoother()
        let first = project()
        XCTAssertFalse(smoother.update(first, at: 10).isEmpty)
        let shifted = first.map { $0.map { LaneBand(y: $0.y, left: $0.left + 0.02, right: $0.right + 0.02) } }
        let result = smoother.update(shifted, at: 10.1)
        for (group, current) in zip(result, shifted) {
            for (band, observation) in zip(group, current) {
                XCTAssertGreaterThanOrEqual(band.left, observation.left - 1e-10)
                XCTAssertLessThanOrEqual(band.right, observation.right + 1e-10)
            }
        }
        XCTAssertTrue(smoother.update([], at: 10.2).isEmpty)
    }
}
