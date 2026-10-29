import XCTest
@testable import BrakeCheckCore

final class CameraARVisualContinuityTests: XCTestCase {
    private func lane(_ t: Double) -> LanePath {
        LanePath(bands: (0..<24).map { i in
            let y = 0.1 + Double(i) * 0.02, w = 0.45 * (0.7 - y)
            return LaneBand(y: y, left: 0.5 - w, right: 0.5 + w)
        }, confidence: 0.95, capturedAt: t, source: .neural)
    }
    private func road(_ t: Double) -> RoadRegion {
        RoadRegion(bands: lane(t).bands.map { LaneBand(y: $0.y, left: 0.01, right: 0.99) },
            confidence: 0.95, capturedAt: t,
            visibility: RoadVisibilityMask(width: 160, height: 90, pixels: .init(repeating: 1, count: 160 * 90)))
    }
    private func frame(_ t: Double, textured: Bool = true) -> LaneLumaFrame {
        var seed: UInt64 = 43
        let pixels: [UInt8] = (0..<(160 * 90)).map { _ in
            seed = seed &* 6364136223846793005 &+ 1442695040888963407
            return textured ? UInt8(truncatingIfNeeded: seed >> 32) : 120
        }
        return LaneLumaFrame(width: 160, height: 90, pixels: pixels, capturedAt: t)!
    }
    private func scene(_ t: Double, missingLane: Bool = false, missingRoad: Bool = false,
                       obstaclesFresh: Bool = true) -> CameraARScene {
        CameraARScene(lane: missingLane ? nil : lane(t), road: missingRoad ? nil : road(t),
            laneObservedAt: t, roadObservedAt: t, capturedAt: t, obstaclesFresh: obstaclesFresh)
    }
    private func seed(_ tracker: inout CameraARVisualContinuity) {
        _ = tracker.update(scene(10), frame: frame(10), laneMissed: false, roadMissed: false, at: 10)
    }

    func testActualImageMatchBridgesMissedLaneAndPreservesSourceTime() throws {
        var tracker = CameraARVisualContinuity(); seed(&tracker)
        let result = tracker.update(scene(10.2, missingLane: true), frame: frame(10.2), laneMissed: true, roadMissed: false, at: 10.2)
        XCTAssertTrue(result.tracking)
        XCTAssertTrue(result.scene.isPredicted)
        XCTAssertEqual(result.scene.laneObservedAt, 10)
        XCTAssertEqual(result.scene.lane?.capturedAt, 10.2)
        XCTAssertGreaterThan(result.opacity, 0.95)
        XCTAssertNotNil(result.scene.road?.visibility)
    }

    func testSuccessiveFramesBridgeTwoSecondsButCannotRenewModelAnchorIndefinitely() {
        var tracker = CameraARVisualContinuity(); seed(&tracker)
        for i in 1...15 {
            let t = 10 + Double(i) * 0.2
            let result = tracker.update(scene(t, missingLane: true), frame: frame(t), laneMissed: true, roadMissed: false, at: t)
            XCTAssertTrue(result.tracking)
            XCTAssertEqual(result.scene.laneObservedAt, 10)
        }
        let expired = tracker.update(scene(13.2, missingLane: true), frame: frame(13.2), laneMissed: true, roadMissed: false, at: 13.2)
        XCTAssertFalse(expired.tracking)
        XCTAssertNil(expired.scene.lane)
    }

    func testNegativeLaneObservationEndsAnchorInsteadOfReappearingAsMiss() {
        var tracker = CameraARVisualContinuity(); seed(&tracker)
        _ = tracker.update(scene(10.1, missingLane: true), frame: frame(10.1), laneMissed: false, roadMissed: false, at: 10.1)
        XCTAssertFalse(tracker.update(scene(10.2, missingLane: true), frame: frame(10.2), laneMissed: true, roadMissed: false, at: 10.2).tracking)
    }

    func testMissingTextureCameraOrObstaclesCannotAuthorizeContinuity() {
        for missing in ["texture", "camera", "obstacles"] {
            var tracker = CameraARVisualContinuity(); seed(&tracker)
            let result = tracker.update(scene(10.2, missingLane: true, obstaclesFresh: missing != "obstacles"),
                frame: missing == "camera" ? nil : frame(10.2, textured: missing != "texture"),
                laneMissed: true, roadMissed: false, at: 10.2)
            XCTAssertFalse(result.tracking, missing)
        }
    }

    func testMissedRoadUsesFreshMeasuredLaneButNegativeRoadClearsAnchor() {
        var tracker = CameraARVisualContinuity(); seed(&tracker)
        let continued = tracker.update(scene(10.2, missingRoad: true), frame: frame(10.2), laneMissed: false, roadMissed: true, at: 10.2)
        XCTAssertTrue(continued.tracking)
        XCTAssertEqual(continued.scene.roadObservedAt, 10)
        _ = tracker.update(scene(10.3, missingRoad: true), frame: frame(10.3), laneMissed: false, roadMissed: false, at: 10.3)
        XCTAssertFalse(tracker.update(scene(10.4, missingRoad: true), frame: frame(10.4), laneMissed: false, roadMissed: true, at: 10.4).tracking)
    }

    func testCameraGapClockResetAndExplicitResetDiscardOldAnchors() {
        for t in [9.9, 10.5] {
            var tracker = CameraARVisualContinuity(); seed(&tracker)
            XCTAssertFalse(tracker.update(scene(t, missingLane: true), frame: frame(t), laneMissed: true, roadMissed: false, at: t).tracking)
        }
        var tracker = CameraARVisualContinuity(); seed(&tracker); tracker.reset()
        XCTAssertFalse(tracker.update(scene(10.1, missingLane: true), frame: frame(10.1), laneMissed: true, roadMissed: false, at: 10.1).tracking)
    }

    func testExtendedConfidenceRequiresFreshVerifiedTrackingNotJustOldPolygon() {
        let now = 12.0
        func level(_ evidence: CameraARTrackingEvidence?) -> CameraARConfidence {
            CameraARConfidencePolicy.level(scene: CameraARScene(lane: lane(now), road: road(now),
                laneObservedAt: 10, roadObservedAt: 10, capturedAt: now, isPredicted: true,
                trackingEvidence: evidence), verified: true, routeMatched: true,
                navigationReliable: true, branchAmbiguous: false, at: now)
        }
        XCTAssertEqual(level(nil), .lost)
        XCTAssertEqual(level(.init(validatedAt: 12, consecutiveMatches: 10, accumulatedCycleError: 0.005)), .aligned)
        XCTAssertEqual(level(.init(validatedAt: 11.8, consecutiveMatches: 10, accumulatedCycleError: 0.005)), .lost)
        XCTAssertEqual(level(.init(validatedAt: 12, consecutiveMatches: 1, accumulatedCycleError: 0.005)), .lost)
        XCTAssertEqual(level(.init(validatedAt: 12, consecutiveMatches: 10, accumulatedCycleError: 0.03)), .lost)
    }

    func testUnrelatedImageAndConflictingFreshLaneCannotAdvanceTracking() {
        var tracker = CameraARVisualContinuity(); seed(&tracker)
        let first = tracker.update(scene(10.1, missingLane: true), frame: frame(10.1), laneMissed: true, roadMissed: false, at: 10.1)
        XCTAssertTrue(first.tracking)
        let reversed = LaneLumaFrame(width: 160, height: 90, pixels: frame(10.2).pixels.reversed(), capturedAt: 10.2)!
        XCTAssertFalse(tracker.update(scene(10.2, missingLane: true), frame: reversed, laneMissed: true, roadMissed: false, at: 10.2).tracking)
        seed(&tracker)
        let changed = LanePath(bands: lane(10.2).bands.map { LaneBand(y: $0.y, left: $0.left + 0.08, right: $0.right + 0.08) },
            confidence: 0.95, capturedAt: 10.2, source: .neural)
        let conflict = CameraARScene(lane: changed, road: nil, laneObservedAt: 10.2, roadObservedAt: 10.2, capturedAt: 10.2)
        XCTAssertFalse(tracker.update(conflict, frame: frame(10.2), laneMissed: false, roadMissed: true, at: 10.2).tracking)
    }

    func testBothModelsCanMissForTwoSecondsWithoutErodingCorridorSupport() throws {
        var tracker = CameraARVisualContinuity(); seed(&tracker)
        let original = lane(10)
        for i in 1...20 {
            let t = 10 + Double(i) * 0.1
            let result = tracker.update(scene(t, missingLane: true, missingRoad: true), frame: frame(t),
                laneMissed: true, roadMissed: true, at: t)
            XCTAssertTrue(result.tracking)
            let supported = try XCTUnwrap(result.scene.lane)
            XCTAssertEqual(supported.bands.first!.y, original.bands.first!.y, accuracy: 0.005)
            XCTAssertEqual(supported.bands.last!.y, original.bands.last!.y, accuracy: 0.005)
            XCTAssertEqual(result.scene.laneObservedAt, 10)
            XCTAssertEqual(result.scene.roadObservedAt, 10)
            XCTAssertEqual(CameraARConfidencePolicy.level(scene: result.scene, verified: true, routeMatched: true,
                navigationReliable: true, branchAmbiguous: false, at: t), .aligned)
        }
    }
}
