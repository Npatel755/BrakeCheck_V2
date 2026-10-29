import Foundation
import XCTest
@testable import BrakeCheckCore

final class FinalIterationTests: XCTestCase {
    private func rear(_ time: Double) -> VehicleViewpoint {
        var history = VehicleViewpointHistory()
        _ = history.append(VehicleViewpoint(scores: [0.02,0.02,0.02,0.30,0.30,0.30,0.02,0.02], capturedAt: time - 0.2)!)
        _ = history.append(VehicleViewpoint(scores: [0.02,0.02,0.02,0.30,0.30,0.30,0.02,0.02], capturedAt: time)!)
        return history.latest(at: time)!
    }
    private func car(id: UUID = UUID(), center: Double = 0.5, y: Double = 0.34,
                     width: Double = 0.07, confidence: Double = 0.9, label: String = "car",
                     view: VehicleViewpoint? = nil, travel: VehicleTravelEvidence? = nil) -> LeadVehicle {
        LeadVehicle(box: CGRect(x: center - width / 2, y: y, width: width, height: width),
            label: label, confidence: confidence, viewpoint: view, trackID: id, travel: travel)
    }
    func testLaneExitGraceRetainsIdentityWithoutSupplyingCameraDanger() throws {
        var selector = LeadSelector(requiresRearView: true); let id = UUID()
        for t in [0.2,0.3,0.4] { _ = selector.update([car(id: id, view: rear(t))], at: t, lane: lane(t)) }
        let outside = try XCTUnwrap(selector.update([car(id: id, view: rear(0.5))], at: 0.5,
            lane: lane(0.5, left: 0.75, right: 0.95)))
        XCTAssertEqual(outside.id, id)
        XCTAssertFalse(outside.isFreshDetection)
        let risk = CollisionApproachRules.forConfirmedLead(lead: RiskSnapshot(),
            camera: CameraRangeSnapshot(distance: 5, closingSpeed: 5, rangeRate: -5),
            confidence: 0.9, fresh: outside.isFreshDetection, pathSupported: outside.isPathSupported,
            rearConfirmed: outside.hasConfirmedRearIdentity)
        XCTAssertNil(risk.score)
        let recovered = try XCTUnwrap(selector.update([car(id: id, view: rear(0.6))], at: 0.6, lane: lane(0.6)))
        XCTAssertEqual(recovered.id, id); XCTAssertTrue(recovered.isFreshDetection)
    }
    func testClippedForegroundDetectionCannotConsumeRearClassifierSlot() {
        let clipped = LeadVehicle(box: CGRect(x: 0.76, y: 0.08, width: 0.24, height: 0.6), label: "car", confidence: 0.99)
        XCTAssertFalse(VehicleViewpointEligibility.canClassify(clipped, sourceWidth: 1280, sourceHeight: 720))
        XCTAssertTrue(VehicleViewpointEligibility.canClassify(car(), sourceWidth: 1280, sourceHeight: 720))
    }
    func testCentralLeadWinsOverCloserAdjacentTruckWithoutLane() throws {
        var selector = LeadSelector(requiresRearView: true)
        let central = UUID(), adjacent = UUID(); var result: LeadSelection?
        for t in [0.2,0.3,0.4] {
            result = selector.update([car(id: adjacent, center: 0.33, y: 0.23, width: 0.12,
                confidence: 0.99, label: "truck", view: rear(t)), car(id: central, view: rear(t))], at: t)
        }
        XCTAssertEqual(try XCTUnwrap(result).id, central)
    }
    func testOffAxisRearCarCannotAcquireBlindLead() {
        var selector = LeadSelector(requiresRearView: true); let id = UUID()
        for t in [0.2,0.3,0.4,0.5] {
            XCTAssertNil(selector.update([car(id: id, center: 0.78, width: 0.1, view: rear(t))], at: t))
        }
    }
    func testFirstRearSlotPrefersCentralCarWithoutLane() {
        var scheduler = VehicleViewpointScheduler(); let central = UUID(), adjacent = UUID()
        let result = scheduler.choose([car(id: adjacent, center: 0.33, y: 0.23, width: 0.12,
            confidence: 0.99, label: "truck"), car(id: central)], at: 0, preferredID: nil, lane: nil)
        XCTAssertEqual(result?.trackID, central)
    }
    func testAdjacentTrafficCannotBlockForwardRecoveryWithoutLane() {
        var recovery = VehicleDetectionRecovery()
        let crop = recovery.propose([car(center: 0.12, width: 0.09)], at: 0,
            preferredBox: nil, lastDetectedAt: nil, trackedBox: nil, lane: nil,
            imageAspect: 16.0 / 9, interval: 0.75, captureAge: 0.05, vehicleLatency: 0.08)
        XCTAssertNotNil(crop)
    }
    func testWeakMatchedTargetCanUseRecovery() {
        var recovery = VehicleDetectionRecovery(); let target = car(confidence: 0.55)
        XCTAssertNotNil(recovery.propose([target], at: 0, preferredBox: target.box, lastDetectedAt: 0,
            trackedBox: nil, lane: nil, imageAspect: 16.0 / 9, interval: 0.75,
            captureAge: 0.05, vehicleLatency: 0.08))
    }
    func testReliableClosingFitCanWarnBeforeOnePointFiveSeconds() {
        let camera = CameraRangeSnapshot(distance: 9, closingSpeed: 5, rangeRate: -5)
        let risk = CollisionApproachRules.forConfirmedLead(lead: RiskSnapshot(), camera: camera,
            confidence: 0.9, fresh: true, pathSupported: false, rearConfirmed: true)
        XCTAssertEqual(risk.level, .critical); XCTAssertEqual(risk.timeToCollision, 1.8)
    }
    private func lane(_ t: Double, left: Double = 0.35, right: Double = 0.65) -> LanePath {
        LanePath(bands: (0..<13).map { LaneBand(y: 0.1 + Double($0) * 0.04, left: left, right: right) },
            confidence: 0.9, capturedAt: t)
    }
    func testMeasuredCurvedLaneCanStillAcquireOffCenterLead() throws {
        var selector = LeadSelector(requiresRearView: true); let id = UUID(); var result: LeadSelection?
        for t in [0.2,0.3,0.4] {
            result = selector.update([car(id: id, center: 0.75, view: rear(t))], at: t,
                lane: lane(t, left: 0.65, right: 0.85))
        }
        XCTAssertEqual(try XCTUnwrap(result).id, id); XCTAssertTrue(result?.isPathSupported == true)
    }
    func testRearProofStillRequiredForCentralLead() {
        var selector = LeadSelector(requiresRearView: true); let id = UUID()
        for t in [0.2,0.3,0.4,0.5] { XCTAssertNil(selector.update([car(id: id)], at: t)) }
    }
    func testCentralChallengerReplacesRememberedAdjacentLeadAfterLaneLoss() throws {
        var selector = LeadSelector(requiresRearView: true); let adjacent = UUID(), central = UUID()
        for t in [0.2,0.3,0.4] {
            _ = selector.update([car(id: adjacent, center: 0.72, view: rear(t))], at: t,
                lane: lane(t, left: 0.65, right: 0.85))
        }
        var result: LeadSelection?
        for t in [1.3,1.4,1.5,1.6] {
            result = selector.update([car(id: adjacent, center: 0.72, view: rear(t)),
                car(id: central, y: 0.4, view: rear(t))], at: t)
        }
        XCTAssertEqual(try XCTUnwrap(result).id, central)
    }
    func testWeakUnconfirmedForwardCarCanUseRecovery() {
        var recovery = VehicleDetectionRecovery()
        XCTAssertNotNil(recovery.propose([car(confidence: 0.55)], at: 0, preferredBox: nil, lastDetectedAt: nil,
            trackedBox: nil, lane: nil, imageAspect: 16.0 / 9, interval: 0.75,
            captureAge: 0.05, vehicleLatency: 0.08))
    }
    func testRecoveredCarCanReplaceSubThresholdFullFrameDetection() {
        let weak = car(confidence: 0.55), improved = car(confidence: 0.9)
        let merged = VehicleDetectionRecovery.merge(fullFrame: [weak], recovery: [improved], imageAspect: 16.0 / 9)
        XCTAssertEqual(merged.count, 1); XCTAssertEqual(merged.first?.confidence, 0.9)
    }
    func testStrongForwardCarAvoidsUnnecessaryRecoveryWithoutLane() {
        var recovery = VehicleDetectionRecovery()
        XCTAssertNil(recovery.propose([car()], at: 0, preferredBox: nil, lastDetectedAt: nil,
            trackedBox: nil, lane: nil, imageAspect: 16.0 / 9, interval: 0.75,
            captureAge: 0.05, vehicleLatency: 0.08))
    }
    func testStrongUniqueYOLOBoxSkipsVisualTracking() {
        let target = car()
        XCTAssertFalse(VehicleTrackSupport.needsVisualTracking([target, car(center: 0.1)], previousBox: target.box))
    }
    func testWeakMissingAmbiguousAndJumpedBoxesKeepVisualTracking() {
        let target = car()
        for vehicles in [[], [car(confidence: 0.55)], [target, car(center: 0.51)], [car(width: 0.1)]] {
            XCTAssertTrue(VehicleTrackSupport.needsVisualTracking(vehicles, previousBox: target.box))
        }
    }
    func testClippedYOLOBoxCannotSkipVisualTracking() {
        let clipped = LeadVehicle(box: CGRect(x: 0.4, y: 0, width: 0.2, height: 0.2), label: "car", confidence: 0.99)
        XCTAssertTrue(VehicleTrackSupport.needsVisualTracking([clipped], previousBox: clipped.box))
    }
    func testReliableProportionalGrowthKeepsFreshLeadNumbers() throws {
        var selector = LeadSelector(requiresRearView: true); let id = UUID()
        for t in [0.2,0.3,0.4] { _ = selector.update([car(id: id, view: rear(t))], at: t) }
        let travel = VehicleTravelEvidence(direction: .uncertain, capturedAt: 0.5, closingSpeed: 8,
            isReliableApproach: true, hasStableBearing: true)
        let result = try XCTUnwrap(selector.update([car(id: id, width: 0.098, travel: travel)], at: 0.5))
        XCTAssertEqual(result.id, id); XCTAssertTrue(result.isFreshDetection)
    }
    func testUnsupportedScaleJumpStillPausesFreshNumbers() throws {
        var selector = LeadSelector(requiresRearView: true); let id = UUID()
        for t in [0.2,0.3,0.4] { _ = selector.update([car(id: id, view: rear(t))], at: t) }
        let result = try XCTUnwrap(selector.update([car(id: id, width: 0.098)], at: 0.5))
        XCTAssertEqual(result.id, id); XCTAssertFalse(result.isFreshDetection)
    }
    func testStaleOrSidewaysMotionCannotAuthorizeScaleJump() {
        let id = UUID(), prior = car(id: id)
        for mode in 0...2 {
            let motion = VehicleTravelEvidence(direction: .uncertain, capturedAt: mode == 0 ? 0.1 : 0.5,
                closingSpeed: 8, isReliableApproach: mode != 1, hasStableBearing: mode != 2)
            XCTAssertFalse(VehicleTrackSupport.reliableScaleGrowth(from: prior,
                to: car(id: id, width: 0.098, travel: motion), at: 0.5))
        }
    }
    private func measuredCar(_ id: UUID, time: Double, confidence: Double = 0.9, start: Double = 20,
                             closing: Double = 5) -> LeadVehicle {
        let distance = start - closing * time
        let width = 1.8 / (2 * tan(60 * Double.pi / 360) * distance)
        return car(id: id, width: width, confidence: confidence)
    }
    func testPreconfirmationRangeHistoryIsReadyOnFirstConfirmedReading() throws {
        var history = VehicleRangeHistory(), selector = LeadSelector(requiresRearView: true)
        let id = UUID(); var result: LeadSelection?; var captured: VehicleRangeCapture?
        for i in 0...12 {
            let t = Double(i) * 0.1, measured = measuredCar(id, time: t, start: 12)
            captured = history.update([measured], at: t, horizontalFOV: 60, detectorGeneration: 0)[id]
            if i >= 10 {
                let candidate = LeadVehicle(box: measured.box, label: measured.label, confidence: measured.confidence,
                    viewpoint: rear(t), trackID: id)
                result = selector.update([candidate], at: t)
            }
        }
        XCTAssertEqual(try XCTUnwrap(result).id, id)
        let current = try XCTUnwrap(captured)
        XCTAssertTrue(current.isCurrent(for: id, at: 12 * 0.1))
        let risk = CollisionApproachRules.forConfirmedLead(lead: RiskSnapshot(), camera: current.snapshot,
            confidence: 0.9, fresh: true, pathSupported: false, rearConfirmed: true)
        XCTAssertEqual(risk.level, .critical); XCTAssertEqual(try XCTUnwrap(risk.timeToCollision), 1.2, accuracy: 0.0001)
    }
    func testDifferentIDAndTimestampCannotUseWarmRangeCapture() throws {
        var history = VehicleRangeHistory(); let id = UUID(); var captured: VehicleRangeCapture?
        for i in 0...12 {
            let t = Double(i) * 0.1
            captured = history.update([measuredCar(id, time: t)], at: t, horizontalFOV: 60, detectorGeneration: 0)[id]
        }
        let current = try XCTUnwrap(captured)
        XCTAssertFalse(current.isCurrent(for: UUID(), at: 1.2))
        XCTAssertFalse(current.isCurrent(for: id, at: 1.3))
        let other = UUID()
        let next = history.update([measuredCar(other, time: 1.3)], at: 1.3, horizontalFOV: 60, detectorGeneration: 0)
        XCTAssertNil(next[other]?.snapshot.closingSpeed)
    }
    func testWarmRangesStillCannotWarnBeforeRearConfirmation() throws {
        var history = VehicleRangeHistory(); let id = UUID(); var captured: VehicleRangeCapture?
        for i in 0...12 {
            let t = Double(i) * 0.1
            captured = history.update([measuredCar(id, time: t, start: 12)], at: t, horizontalFOV: 60, detectorGeneration: 0)[id]
        }
        XCTAssertNotNil(captured?.snapshot.closingSpeed)
        let risk = CollisionApproachRules.forConfirmedLead(lead: RiskSnapshot(), camera: try XCTUnwrap(captured).snapshot,
            confidence: 0.9, fresh: true, pathSupported: false, rearConfirmed: false)
        XCTAssertNil(risk.score)
    }
    func testLightingFOVAndLongGapInvalidateWarmMotion() {
        for mode in 0...2 {
            var history = VehicleRangeHistory(); let id = UUID()
            for i in 0...12 {
                let t = Double(i) * 0.1
                _ = history.update([measuredCar(id, time: t)], at: t, horizontalFOV: 60, detectorGeneration: 0)
            }
            let t = mode == 2 ? 2.0 : 1.3
            let result = history.update([measuredCar(id, time: t)], at: t,
                horizontalFOV: mode == 0 ? 61 : 60, detectorGeneration: mode == 1 ? 1 : 0)
            XCTAssertNil(result[id]?.snapshot.closingSpeed); XCTAssertNotNil(result[id]?.snapshot.distance)
        }
    }
    func testMissingWarmVehicleProducesNoHeldNumericCapture() throws {
        var history = VehicleRangeHistory(); let id = UUID()
        for i in 0...12 {
            let t = Double(i) * 0.1
            _ = history.update([measuredCar(id, time: t)], at: t, horizontalFOV: 60, detectorGeneration: 0)
        }
        XCTAssertTrue(history.update([], at: 1.3, horizontalFOV: 60, detectorGeneration: 0).isEmpty)
        let resumed = history.update([measuredCar(id, time: 1.4)], at: 1.4, horizontalFOV: 60, detectorGeneration: 0)
        XCTAssertEqual(try XCTUnwrap(resumed[id]?.snapshot.closingSpeed), 5, accuracy: 0.0001)
    }
    func testWeakAndClippedWarmCapturesDoNotReuseMotion() {
        var history = VehicleRangeHistory(); let id = UUID()
        for i in 0...12 {
            let t = Double(i) * 0.1
            _ = history.update([measuredCar(id, time: t)], at: t, horizontalFOV: 60, detectorGeneration: 0)
        }
        let weak = history.update([measuredCar(id, time: 1.3, confidence: 0.5)], at: 1.3,
            horizontalFOV: 60, detectorGeneration: 0)
        XCTAssertNil(weak[id]?.snapshot.closingSpeed)
        let clipped = LeadVehicle(box: CGRect(x: 0.4, y: 0, width: 0.2, height: 0.2), label: "car", confidence: 0.9, trackID: id)
        XCTAssertNil(history.update([clipped], at: 1.4, horizontalFOV: 60, detectorGeneration: 0)[id]?.snapshot.distance)
        XCTAssertNil(history.update([measuredCar(id, time: 1.5)], at: 1.5, horizontalFOV: 60, detectorGeneration: 0)[id]?.snapshot.closingSpeed)
    }
    func testWarmOutlierPublishesNilSnapshotInsteadOfFallbackRange() throws {
        var history = VehicleRangeHistory(); let id = UUID()
        for i in 0...12 {
            let t = Double(i) * 0.1
            _ = history.update([measuredCar(id, time: t)], at: t, horizontalFOV: 60, detectorGeneration: 0)
        }
        let result = history.update([measuredCar(id, time: 0, start: 40)], at: 1.3, horizontalFOV: 60, detectorGeneration: 0)
        let captured = try XCTUnwrap(result[id])
        XCTAssertTrue(captured.isCurrent(for: id, at: 1.3)); XCTAssertNil(captured.snapshot.distance)
        XCTAssertNil(captured.snapshot.closingSpeed)
        let next = history.update([measuredCar(id, time: 1.4)], at: 1.4, horizontalFOV: 60, detectorGeneration: 0)
        XCTAssertEqual(try XCTUnwrap(next[id]?.snapshot.closingSpeed), 5, accuracy: 0.0001)
    }
    func testWarmHistoryRejectsUnorderedTimeAndInvalidFOV() {
        var history = VehicleRangeHistory(); let id = UUID()
        _ = history.update([car(id: id)], at: 1, horizontalFOV: 60, detectorGeneration: 0)
        XCTAssertTrue(history.update([car(id: id)], at: 1, horizontalFOV: 60, detectorGeneration: 0).isEmpty)
        XCTAssertTrue(history.update([car(id: id)], at: 1.1, horizontalFOV: .nan, detectorGeneration: 0).isEmpty)
        XCTAssertNil(history.update([car(id: id)], at: 1.2, horizontalFOV: 60, detectorGeneration: 0)[id]?.snapshot.closingSpeed)
    }
    func testEarlierWarningRequiresCoherentFittedClosingMotion() {
        for mode in 0...3 {
            let camera = CameraRangeSnapshot(distance: 9, closingSpeed: 5,
                rangeRate: mode == 1 ? nil : mode == 2 ? -1 : -5)
            let risk = CollisionApproachRules.evaluateCamera(camera, confidence: mode == 0 ? 0.75 : 0.9)
            if mode == 0 || mode == 3 { XCTAssertEqual(risk.level, .critical) }
            else { XCTAssertEqual(risk.level, .high) }
        }
        let weakFit = CollisionApproachRules.evaluateCamera(
            CameraRangeSnapshot(distance: 9, closingSpeed: 5, rangeRate: -5), confidence: 0.69)
        XCTAssertEqual(weakFit.level, .high)
        let boundary = CollisionApproachRules.evaluateCamera(CameraRangeSnapshot(distance: 10, closingSpeed: 5, rangeRate: -5), confidence: 0.9)
        XCTAssertEqual(boundary.level, .high)
    }
    func testEarlierWarningStillSurvivesMissingMeasurementsForFiveSeconds() {
        let risk = CollisionApproachRules.evaluateCamera(CameraRangeSnapshot(distance: 9, closingSpeed: 5, rangeRate: -5), confidence: 0.9)
        var latch = BrakeWarningLatch()
        if risk.level == .critical { latch.trigger(at: 10) }
        XCTAssertNil(CollisionApproachRules.evaluateCamera(CameraRangeSnapshot(), confidence: 0.9).score)
        XCTAssertTrue(latch.isActive(at: 14.999)); XCTAssertFalse(latch.isActive(at: 15))
    }
    func testGPSDirectionDisagreementCannotEraseConfirmedRearLead() throws {
        var selector = LeadSelector(requiresRearView: true); let id = UUID()
        for t in [0.2,0.3,0.4] { _ = selector.update([car(id: id, view: rear(t))], at: t) }
        let motion = VehicleTravelEvidence(direction: .rapidApproach, capturedAt: 0.5,
            closingSpeed: 10, isReliableApproach: true, hasStableBearing: true)
        let result = try XCTUnwrap(selector.update([car(id: id, travel: motion)], at: 0.5))
        XCTAssertEqual(result.id, id); XCTAssertTrue(result.hasConfirmedRearIdentity)
        XCTAssertTrue(result.isFreshDetection)
    }
    func testGPSDirectionDisagreementDoesNotAuthorizeUnconfirmedLead() {
        var selector = LeadSelector(requiresRearView: true); let id = UUID()
        for t in [0.2,0.3,0.4,0.5] {
            let motion = VehicleTravelEvidence(direction: .rapidApproach, capturedAt: t,
                closingSpeed: 10, isReliableApproach: true, hasStableBearing: true)
            XCTAssertNil(selector.update([car(id: id, view: rear(t), travel: motion)], at: t))
        }
    }
    func testFrontViewStillVetoesRememberedLeadDespiteGPSProtection() {
        var selector = LeadSelector(requiresRearView: true); let id = UUID()
        for t in [0.2,0.3,0.4] { _ = selector.update([car(id: id, view: rear(t))], at: t) }
        let front = VehicleViewpoint(scores: [0.9,0.03,0.01,0.01,0.01,0.01,0.01,0.02], capturedAt: 0.5)!
        let motion = VehicleTravelEvidence(direction: .rapidApproach, capturedAt: 0.5,
            closingSpeed: 10, isReliableApproach: true, hasStableBearing: true)
        XCTAssertNil(selector.update([car(id: id, view: front, travel: motion)], at: 0.5))
    }
}
