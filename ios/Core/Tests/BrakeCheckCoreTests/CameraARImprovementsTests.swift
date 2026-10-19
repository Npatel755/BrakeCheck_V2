import XCTest
@testable import BrakeCheckCore

final class CameraARImprovementsTests: XCTestCase {
    private func lane(at time: Double = 10, shift: Double = 0, curve: Double = 0) -> LanePath {
        LanePath(bands: (0..<25).map { index in
            let y = 0.1 + Double(index) * 0.02
            let half = 0.45 * (0.7 - y), center = 0.5 + shift + curve * (y - 0.1)
            return LaneBand(y: y, left: center - half, right: center + half)
        }, confidence: 0.95, capturedAt: time, source: .neural)
    }
    private func road(at time: Double = 10, hole: Bool = false) -> RoadRegion {
        var pixels = [UInt8](repeating: 1, count: 64 * 64)
        if hole { for row in 26...34 { for column in 26...37 { pixels[row * 64 + column] = 0 } } }
        return RoadRegion(bands: (0..<30).map {
            LaneBand(y: 0.05 + Double($0) * 0.02, left: 0.05, right: 0.95)
        }, confidence: 0.95, capturedAt: time, visibility: RoadVisibilityMask(width: 64, height: 64, pixels: pixels))
    }
    private func scene(time: Double = 10, observed: Double = 10, obstaclesFresh: Bool = true) -> CameraARScene {
        CameraARScene(lane: lane(at: time), road: road(at: time), laneObservedAt: observed,
            roadObservedAt: observed, capturedAt: time, obstaclesFresh: obstaclesFresh)
    }
    func testLaneAwareRibbonFollowsObservedCurveAndNeverAdjacentRoad() {
        let lane = lane(curve: 0.04)
        let bands = CameraRouteProjection.project(route: [.init(latitude: 0, longitude: 0), .init(latitude: 0.002, longitude: 0)],
            progressFraction: 0, courseDegrees: 0, horizontalFOV: 60, aspect: 1.7,
            calibration: .init(), lane: lane, road: road(), at: 10).flatMap { $0 }
        XCTAssertGreaterThan(bands.count, 5)
        for band in bands {
            let interval = lane.bounds(at: band.y)!
            XCTAssertEqual((band.left + band.right) / 2, (interval.left + interval.right) / 2, accuracy: 1e-9)
            XCTAssertGreaterThan(band.left, interval.left)
            XCTAssertLessThan(band.right, interval.right)
        }
    }
    func testRawMaskHolesRemainInvisibleEvenWhenRoadSpansBridgeThem() {
        let mask = road(hole: true).visibility!
        XCTAssertFalse(mask.contains(x: 0.5, y: 0.5))
        XCTAssertTrue(mask.contains(x: 0.2, y: 0.5))
        let groups = CameraRouteProjection.project(route: [.init(latitude: 0, longitude: 0), .init(latitude: 0.002, longitude: 0)],
            progressFraction: 0, courseDegrees: 0, horizontalFOV: 60, aspect: 1.7,
            calibration: .init(), lane: lane(), road: road(hole: true), at: 10)
        XCTAssertFalse(groups.isEmpty)
        XCTAssertTrue(groups.flatMap { $0 }.allSatisfy { mask.contains(x: ($0.left + $0.right) / 2, y: $0.y) })
    }
    func testVisualRoadPredictionTracksCameraMovementWithoutRenewingSourceAge() throws {
        let projected = try XCTUnwrap(RoadVisualProjection.project(road(), from: lane(), to: lane(at: 10.2, shift: 0.02), at: 10.2))
        XCTAssertEqual(projected.bounds(at: 0.3)!.left, 0.07, accuracy: 0.001)
        XCTAssertEqual(projected.capturedAt, 10.2)
        XCTAssertLessThan(projected.confidence, road().confidence)
        XCTAssertNotNil(projected.visibility)
        XCTAssertNil(RoadVisualProjection.project(road(), from: lane(), to: lane(at: 10.81), at: 10.81))
        XCTAssertNil(RoadVisualProjection.project(road(), from: lane(), to: lane(at: 10.2, shift: 0.3), at: 10.2))
    }
    func testConfidenceHasAlignedDirectionalAndLostLevels() {
        func level(verified: Bool, matched: Bool = true, branch: Bool = false, scene: CameraARScene? = nil) -> CameraARConfidence {
            CameraARConfidencePolicy.level(scene: scene ?? self.scene(), verified: verified,
                routeMatched: matched, navigationReliable: true, branchAmbiguous: branch, at: 10)
        }
        XCTAssertEqual(level(verified: true), .aligned)
        XCTAssertEqual(level(verified: false), .directional)
        XCTAssertEqual(level(verified: true, matched: false), .directional)
        XCTAssertEqual(level(verified: true, branch: true), .directional)
        XCTAssertEqual(level(verified: true, scene: scene(obstaclesFresh: false)), .directional)
        let partial = CameraARScene(lane: nil, road: road(), laneObservedAt: 10, roadObservedAt: 10, capturedAt: 10)
        XCTAssertEqual(level(verified: true, scene: partial), .directional)
        XCTAssertEqual(CameraARConfidencePolicy.level(scene: scene(), verified: true, routeMatched: true,
            navigationReliable: true, branchAmbiguous: false, at: 10.5), .lost)
        XCTAssertEqual(CameraARConfidencePolicy.level(scene: scene(time: 10.9, observed: 10), verified: true,
            routeMatched: true, navigationReliable: true, branchAmbiguous: false, at: 10.9), .lost)
    }
    func testConfidenceRecoveryRequiresDistinctConsistentObservationsAndDowngradesImmediately() {
        var tracker = CameraARConfidenceTracker()
        XCTAssertEqual(tracker.update(.aligned, observedAt: 10), .directional)
        for _ in 0..<10 { XCTAssertEqual(tracker.update(.aligned, observedAt: 10), .directional) }
        XCTAssertEqual(tracker.update(.aligned, observedAt: 10.1), .directional)
        XCTAssertEqual(tracker.update(.aligned, observedAt: 10.21), .aligned)
        XCTAssertEqual(tracker.update(.directional, observedAt: 10.22), .directional)
        XCTAssertEqual(tracker.update(.lost, observedAt: 10.23), .lost)
    }
    func testThermalAndLatencyAdaptationOnlyChangesAuxiliaryBudget() {
        let nominal = ARInferencePolicy(minimumLaneInterval: 0.2, heat: .nominal, vehicleLatency: 0.05, laneLatency: 0.03, roadLatency: 0.04)
        let hot = ARInferencePolicy(minimumLaneInterval: 0.4, heat: .serious, vehicleLatency: 0.05, laneLatency: 0.03, roadLatency: 0.04)
        XCTAssertEqual(nominal.roadInterval, 0.6)
        XCTAssertGreaterThan(hot.roadInterval!, nominal.roadInterval!)
        let overloaded = ARInferencePolicy(minimumLaneInterval: 0.2, heat: .nominal, vehicleLatency: 0.21, laneLatency: 0.15, roadLatency: 0.1)
        XCTAssertNil(overloaded.roadInterval)
        XCTAssertEqual(overloaded.laneInterval, 0.5)
        XCTAssertNil(ARInferencePolicy(minimumLaneInterval: 0.2, heat: .critical, vehicleLatency: 0, laneLatency: 0, roadLatency: 0).roadInterval)
        let slow = ARInferencePolicy(minimumLaneInterval: 0.2, heat: .nominal, vehicleLatency: 0.05, laneLatency: 0.16, roadLatency: 0.13)
        XCTAssertTrue(slow.laneAllowed)
        XCTAssertEqual(slow.laneInterval, 0.5)
        XCTAssertNil(slow.roadInterval)
        XCTAssertFalse(ARInferencePolicy(minimumLaneInterval: 0.2, heat: .nominal, vehicleLatency: 0.05,
            laneLatency: 0.31, roadLatency: 0.04).laneAllowed)
        XCTAssertNotNil(ARInferencePolicy(minimumLaneInterval: 0.2, heat: .nominal, vehicleLatency: 0.05,
            laneLatency: 0.16, roadLatency: 0.13, stationary: true).roadInterval)
    }
    func testCalibrationEstimatesPitchYawAndRequiresDistinctStableFrames() throws {
        let estimate = try XCTUnwrap(CameraCalibrationGeometry.estimate(lane: lane(), fov: 60, aspect: 1.7))
        XCTAssertEqual(estimate.horizon, 0.7, accuracy: 1e-9)
        XCTAssertEqual(estimate.yawDegrees, 0, accuracy: 1e-9)
        XCTAssertGreaterThan(estimate.pitchDegrees, 0)
        var assistant = CameraCalibrationAssistant()
        for _ in 0..<20 { XCTAssertNil(assistant.update(estimate, observedAt: 10)) }
        var ready: CameraCalibrationEstimate?
        for index in 1...8 { ready = assistant.update(estimate, observedAt: 10 + Double(index) * 0.2) }
        XCTAssertNotNil(ready)
        XCTAssertNotNil(assistant.update(nil, observedAt: 12))
        XCTAssertNil(assistant.update(nil, observedAt: 13))
    }
    func testVerificationIsInvalidatedByOrientationAndMountChanges() {
        let first = CameraMountVerification.signature(width: 720, height: 1280, fov: 45, calibration: .init())
        XCTAssertNotEqual(first, CameraMountVerification.signature(width: 1280, height: 720, fov: 60, calibration: .init()))
        XCTAssertNotEqual(first, CameraMountVerification.signature(width: 720, height: 1280, fov: 45, calibration: .init(heightMeters: 1.5)))
        XCTAssertNotEqual(first, CameraMountVerification.signature(width: 720, height: 1280, fov: 45, orientation: 2, calibration: .init()))
    }
    func testSmootherUsesCurrentCorridorSupportWithoutJumpingOrCrossingItsEdges() {
        var smoother = CameraRouteSmoother()
        let first = (0..<10).map { LaneBand(y: 0.20 + Double($0) * 0.02, left: 0.48, right: 0.52) }
        _ = smoother.update([first], at: 10, lane: lane(), road: road())
        let shifted = first.map { LaneBand(y: $0.y, left: $0.left + 0.02, right: $0.right + 0.02) }
        let result = smoother.update([shifted], at: 10.1, lane: lane(at: 10.1, shift: 0.02), road: road(at: 10.1))
        for band in result[0] {
            XCTAssertGreaterThan((band.left + band.right) / 2, 0.5)
            XCTAssertLessThan((band.left + band.right) / 2, 0.52)
            XCTAssertGreaterThan(band.left, lane(at: 10.1, shift: 0.02).bounds(at: band.y)!.left)
            XCTAssertLessThan(band.right, lane(at: 10.1, shift: 0.02).bounds(at: band.y)!.right)
        }
    }
    func testBriefPredictionUsesCameraMotionAndCannotRecursivelyExtendOrReviveNegativeInference() throws {
        var tracker = CameraARSceneTracker()
        let first = CameraARScene(lane: lane(), road: road(), laneObservedAt: 10, roadObservedAt: 10, capturedAt: 10)
        _ = tracker.update(first, at: 10)
        let moved = CameraARScene(lane: lane(at: 10.1, shift: 0.009), road: road(at: 10.1),
            laneObservedAt: 10, roadObservedAt: 10, capturedAt: 10.1)
        _ = tracker.update(moved, at: 10.1)
        func missing(_ time: Double, observed: Double = 10) -> CameraARScene {
            CameraARScene(lane: nil, road: nil, laneObservedAt: 10, roadObservedAt: observed, capturedAt: time)
        }
        let predicted = tracker.update(missing(10.2), at: 10.2)
        XCTAssertTrue(predicted.isPredicted)
        let interval = try XCTUnwrap(predicted.lane?.bounds(at: 0.3))
        XCTAssertEqual((interval.left + interval.right) / 2, 0.518, accuracy: 0.001)
        XCTAssertEqual(predicted.roadObservedAt, 10)
        XCTAssertFalse(tracker.update(missing(10.3), at: 10.3).isPredicted)
        XCTAssertFalse(tracker.update(missing(10.21, observed: 10.15), at: 10.21).isPredicted)
        XCTAssertFalse(tracker.update(missing(10.22), at: 10.22).isPredicted)
    }
    private func point(_ east: Double, _ north: Double) -> NavigationCoordinate {
        let scale = 6_371_008.8 * Double.pi / 180
        return NavigationCoordinate(latitude: north / scale, longitude: east / scale)
    }
    func testMapMatchingUsesCourseToDisambiguateParallelRouteLegs() throws {
        var tracker = NavigationProgressTracker(coordinates: [point(0, 0), point(0, 200), point(10, 200), point(10, 0)],
            routeDistance: 410, stepDistances: [200, 10, 200])
        let fix = try XCTUnwrap(tracker.update(coordinate: point(9, 80), horizontalAccuracy: 5, timestamp: 10, now: 10,
            courseDegrees: 180, courseAccuracy: 5, speed: 10))
        XCTAssertGreaterThan(fix.progressMeters, 300)
        XCTAssertGreaterThan(fix.mapMatchConfidence, 0.75)
        XCTAssertFalse(fix.mapMatchAmbiguous)
    }
    func testMapMatchingMarksOverlappingSameDirectionBranchesAmbiguous() throws {
        var tracker = NavigationProgressTracker(coordinates: [point(0, 0), point(0, 100), point(30, 100), point(30, 0), point(0, 0), point(0, 100)],
            routeDistance: 360, stepDistances: [100, 30, 100, 30, 100])
        let fix = try XCTUnwrap(tracker.update(coordinate: point(0, 50), horizontalAccuracy: 5, timestamp: 10, now: 10,
            courseDegrees: 0, courseAccuracy: 5, speed: 10))
        XCTAssertTrue(fix.mapMatchAmbiguous)
        XCTAssertEqual(fix.mapMatchConfidence, 0)
        XCTAssertFalse(fix.positionReliable)
        XCTAssertEqual(fix.progressMeters, 0, accuracy: 0.01)
    }
}
