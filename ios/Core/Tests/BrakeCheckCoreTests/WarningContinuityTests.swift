import Foundation
import XCTest
@testable import BrakeCheckCore

final class WarningContinuityTests: XCTestCase {
    private func vehicle(_ time: Double, id: UUID) -> LeadVehicle {
        LeadVehicle(box: CGRect(x: 0.455, y: 0.3, width: 0.09, height: 0.09), label: "car", confidence: 0.9,
            trackID: id, travel: VehicleTravelEvidence(direction: .uncertain, capturedAt: time,
                closingSpeed: 6, isReliableApproach: true, hasStableBearing: true))
    }
    private func offsetLane(_ time: Double) -> LanePath {
        LanePath(bands: (0..<13).map { LaneBand(y: 0.1 + Double($0) * 0.04, left: 0.65, right: 0.95) },
                 confidence: 0.9, capturedAt: time)
    }
    func testConfirmedLeadCanWarnWithoutLanePathOrGPS() {
        let risk = CollisionApproachRules.forConfirmedLead(lead: RiskSnapshot(),
            camera: CameraRangeSnapshot(distance: 6, closingSpeed: 5), confidence: 0.9,
            fresh: true, pathSupported: false, rearConfirmed: true)
        XCTAssertEqual(risk.level, .critical); XCTAssertEqual(risk.timeToCollision, 1.2)
        XCTAssertNil(risk.leadSpeed)
    }
    func testHazardCanConfirmAtTwoFramesPerSecond() throws {
        var selector = VehicleHazardSelector(); let id = UUID(); var result: LeadSelection?
        for t in [0.0, 0.5, 1.0, 1.5] {
            result = selector.update([vehicle(t, id: id)], at: t, lane: nil, road: nil, imageAspect: 16.0 / 9)
        }
        XCTAssertEqual(try XCTUnwrap(result).id, id)
    }
    func testBriefMissingHazardFrameDoesNotRestartConfirmation() throws {
        var selector = VehicleHazardSelector(); let id = UUID()
        for t in [0.0, 0.1, 0.2] {
            _ = selector.update([vehicle(t, id: id)], at: t, lane: nil, road: nil, imageAspect: 16.0 / 9)
        }
        XCTAssertNil(selector.update([], at: 0.3, lane: nil, road: nil, imageAspect: 16.0 / 9))
        let resumed = selector.update([vehicle(0.4, id: id)], at: 0.4, lane: nil, road: nil, imageAspect: 16.0 / 9)
        XCTAssertEqual(try XCTUnwrap(resumed).id, id)
    }
    func testNoisyLaneCannotVetoDirectStableCameraApproach() throws {
        var selector = VehicleHazardSelector(); let id = UUID(); var result: LeadSelection?
        for t in [0.0, 0.1, 0.2] {
            result = selector.update([vehicle(t, id: id)], at: t, lane: offsetLane(t), road: nil, imageAspect: 16.0 / 9)
        }
        XCTAssertTrue(try XCTUnwrap(result).isCameraProjectedPath)
    }

    func testBrakeWarningSurvivesLostRiskAndNewTargetForFiveSeconds() {
        var latch = BrakeWarningLatch()
        let critical = CollisionApproachRules.forConfirmedLead(lead: RiskSnapshot(),
            camera: CameraRangeSnapshot(distance: 6, closingSpeed: 5), confidence: 0.9,
            fresh: true, pathSupported: false, rearConfirmed: true)
        if critical.level == .critical { latch.trigger(at: 10) }
        var estimator = CameraRangeEstimator()
        let missing = estimator.update(targetID: UUID(), time: 10.1, distance: 6, confidence: 0.9, fresh: false)
        XCTAssertNil(missing.distance); XCTAssertNil(missing.closingSpeed)
        let newTarget = estimator.update(targetID: UUID(), time: 10.2, distance: 40, confidence: 0.9, fresh: true)
        let risk = CollisionApproachRules.forConfirmedLead(lead: RiskSnapshot(), camera: newTarget,
            confidence: 0.9, fresh: true, pathSupported: false, rearConfirmed: true)
        XCTAssertNil(risk.score); XCTAssertNil(risk.timeToCollision)
        XCTAssertTrue(latch.isActive(at: 10.1)); XCTAssertTrue(latch.isActive(at: 14.999))
        XCTAssertFalse(latch.isActive(at: 15))
    }
    func testRepeatedCriticalCapturesExtendWarningFromLastTrigger() {
        var latch = BrakeWarningLatch()
        latch.trigger(at: 10); latch.trigger(at: 14.9)
        XCTAssertTrue(latch.isActive(at: 19.899)); XCTAssertFalse(latch.isActive(at: 19.9))
        latch.trigger(at: 20)
        XCTAssertTrue(latch.isActive(at: 24.999)); XCTAssertFalse(latch.isActive(at: 25))
    }
    func testInvalidClockValuesCannotCreateOrCancelWarning() {
        var latch = BrakeWarningLatch()
        latch.trigger(at: .nan); latch.trigger(at: .infinity)
        XCTAssertFalse(latch.isActive(at: 1))
        latch.trigger(at: 2); latch.trigger(at: -.infinity)
        XCTAssertTrue(latch.isActive(at: 6.99)); XCTAssertFalse(latch.isActive(at: .nan))
    }
    func testMeasuredSteadyAndOpeningGapsGiveCurrentLowerIndication() {
        for rate in [0.0, 2.0] {
            let camera = CameraRangeSnapshot(distance: 12, closingSpeed: 0, rangeRate: rate)
            let risk = CollisionApproachRules.forConfirmedLead(lead: RiskSnapshot(), camera: camera,
                confidence: 0.9, fresh: true, pathSupported: false, rearConfirmed: true)
            XCTAssertEqual(risk.level, .low); XCTAssertEqual(risk.score, 0)
            XCTAssertNil(risk.timeToCollision); XCTAssertNil(risk.leadSpeed)
        }
        XCTAssertNil(CollisionApproachRules.evaluateCamera(CameraRangeSnapshot(distance: 12), confidence: 0.9).score)
    }
    func testAccelerationProjectionCanWarnBeforeLinearTTC() throws {
        let camera = CameraRangeSnapshot(distance: 5, closingSpeed: 1, rangeRate: -1, closingAcceleration: 6)
        let risk = CollisionApproachRules.forConfirmedLead(lead: RiskSnapshot(), camera: camera,
            confidence: 0.9, fresh: true, pathSupported: false, rearConfirmed: true)
        let expected = (-1 + sqrt(61.0)) / 6
        XCTAssertEqual(try XCTUnwrap(risk.timeToCollision), expected, accuracy: 0.0001)
        XCTAssertEqual(risk.level, .critical); XCTAssertNil(risk.leadAcceleration)
        XCTAssertTrue(risk.reason.contains("Relative gap acceleration"))
    }
    func testOpeningGapAccelerationCanOnlyProjectWithinThreeSeconds() throws {
        let soon = CameraRangeSnapshot(distance: 2, closingSpeed: 0, rangeRate: 1, closingAcceleration: 3)
        let risk = CollisionApproachRules.evaluateCamera(soon, confidence: 0.9)
        XCTAssertEqual(try XCTUnwrap(risk.timeToCollision), (1 + sqrt(13.0)) / 3, accuracy: 0.0001)
        XCTAssertEqual(risk.level, .high)
        let distant = CameraRangeSnapshot(distance: 40, closingSpeed: 0, rangeRate: 1, closingAcceleration: 3)
        let bounded = CollisionApproachRules.evaluateCamera(distant, confidence: 0.9)
        XCTAssertNil(bounded.timeToCollision); XCTAssertEqual(bounded.score, 0)
    }
    func testInvalidOrEasingAccelerationCannotReduceLinearWarning() {
        for acceleration in [Double.nan, Double.infinity, 13, -3, 0.3] {
            let camera = CameraRangeSnapshot(distance: 6, closingSpeed: 5, rangeRate: -5,
                closingAcceleration: acceleration)
            let risk = CollisionApproachRules.evaluateCamera(camera, confidence: 0.9)
            XCTAssertEqual(risk.level, .critical); XCTAssertEqual(risk.timeToCollision, 1.2)
        }
    }
    func testInconsistentAccelerationInputsUseLinearMotion() {
        let camera = CameraRangeSnapshot(distance: 5, closingSpeed: 1, rangeRate: -5, closingAcceleration: 6)
        XCTAssertEqual(CollisionApproachRules.evaluateCamera(camera, confidence: 0.9).timeToCollision, 5)
    }
    func testCameraRiskUsesItsFitBeforeFallbackMotion() {
        let risk = CollisionApproachRules.evaluateCamera(CameraRangeSnapshot(distance: 10, closingSpeed: 2),
            confidence: 0.9, fallbackClosingSpeed: 10)
        XCTAssertEqual(risk.timeToCollision, 5); XCTAssertEqual(risk.level, .low)
        let fallback = CollisionApproachRules.evaluateCamera(CameraRangeSnapshot(distance: 10),
            confidence: 0.9, fallbackClosingSpeed: 10)
        XCTAssertEqual(fallback.level, .critical)
    }
    func testWeakOrInvalidMeasurementsCannotProduceCameraRisk() {
        for confidence in [0.64, Double.nan, Double.infinity] {
            XCTAssertNil(CollisionApproachRules.evaluateCamera(
                CameraRangeSnapshot(distance: 6, closingSpeed: 5), confidence: confidence).score)
        }
        for distance in [0.0, 151, Double.nan, Double.infinity] {
            let result = CollisionApproachRules.evaluateCamera(CameraRangeSnapshot(distance: distance, closingSpeed: 5), confidence: 0.9)
            XCTAssertNil(result.score); XCTAssertNil(result.distance)
        }
        for closing in [-1.0, 61, Double.nan] {
            XCTAssertNil(CollisionApproachRules.evaluateCamera(
                CameraRangeSnapshot(distance: 6, closingSpeed: closing), confidence: 0.9).score)
        }
    }
    func testCameraTTCFillsEqualScoreAndPreservesLeadFields() {
        let lead = RiskSnapshot(level: .critical, score: 95, distance: 8, leadSpeed: 10,
            leadAcceleration: -6, timeToCollision: 1.4, headway: 0.5)
        let approach = RiskSnapshot(level: .critical, score: 95, distance: 8, closingSpeed: 8, timeToCollision: 1)
        let result = CollisionApproachRules.combine(lead: lead, approach: approach)
        XCTAssertEqual(result.timeToCollision, 1); XCTAssertEqual(result.leadSpeed, 10)
        XCTAssertEqual(result.leadAcceleration, -6); XCTAssertEqual(result.headway, 0.5)
    }
    func testWeakFramesCannotRenewHazardConfirmationIndefinitely() throws {
        var selector = VehicleHazardSelector(); let id = UUID()
        for t in [0.0, 0.1, 0.2] {
            _ = selector.update([vehicle(t, id: id)], at: t, lane: nil, road: nil, imageAspect: 16.0 / 9)
        }
        for t in [0.3, 0.5, 0.7, 0.9] {
            XCTAssertNil(selector.update([], at: t, lane: nil, road: nil, imageAspect: 16.0 / 9))
        }
        XCTAssertNil(selector.update([vehicle(1, id: id)], at: 1, lane: nil, road: nil, imageAspect: 16.0 / 9))
        XCTAssertNil(selector.update([vehicle(1.1, id: id)], at: 1.1, lane: nil, road: nil, imageAspect: 16.0 / 9))
        XCTAssertEqual(try XCTUnwrap(selector.update([vehicle(1.2, id: id)], at: 1.2,
            lane: nil, road: nil, imageAspect: 16.0 / 9)).id, id)
    }
    func testDifferentHazardIDRequiresFreshConfirmation() throws {
        var selector = VehicleHazardSelector(); let first = UUID(), next = UUID()
        for t in [0.0, 0.1, 0.2] {
            _ = selector.update([vehicle(t, id: first)], at: t, lane: nil, road: nil, imageAspect: 16.0 / 9)
        }
        XCTAssertNil(selector.update([vehicle(0.3, id: next)], at: 0.3, lane: nil, road: nil, imageAspect: 16.0 / 9))
        XCTAssertNil(selector.update([vehicle(0.4, id: next)], at: 0.4, lane: nil, road: nil, imageAspect: 16.0 / 9))
        XCTAssertEqual(try XCTUnwrap(selector.update([vehicle(0.5, id: next)], at: 0.5,
            lane: nil, road: nil, imageAspect: 16.0 / 9)).id, next)
    }
    private func samples(base: Double = 0, count: Int = 16, acceleration: Double = 3,
                         noise: Double = 0) -> [TimedValue] {
        (0..<count).map { i in
            let t = Double(i) * 0.1
            let wobble = i % 2 == 0 ? noise : -noise
            return TimedValue(time: base + t, value: 20 - 2 * t - 0.5 * acceleration * t * t + wobble)
        }
    }
    func testQuadraticFitMeasuresCurrentClosureAndRelativeAcceleration() throws {
        let input = samples(), fit = try XCTUnwrap(CameraMotionFit.estimate(input, distance: input.last!.value))
        XCTAssertEqual(fit.rangeRate, -6.5, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(fit.closingAcceleration), 3, accuracy: 0.0001)
    }
    func testQuadraticFitHandlesLargeUptimeWithoutPrecisionLoss() throws {
        let input = samples(base: 1_000_000_000)
        let fit = try XCTUnwrap(CameraMotionFit.estimate(input, distance: input.last!.value))
        XCTAssertEqual(fit.rangeRate, -6.5, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(fit.closingAcceleration), 3, accuracy: 0.0001)
    }
    func testConstantMotionAndSmallNoiseDoNotInventAcceleration() throws {
        for noise in [0.0, 0.1] {
            let input = samples(acceleration: 0, noise: noise)
            let fit = try XCTUnwrap(CameraMotionFit.estimate(input, distance: input.last!.value))
            XCTAssertEqual(fit.rangeRate, -2, accuracy: 0.04); XCTAssertNil(fit.closingAcceleration)
        }
    }
    func testUnsettledBoxWobbleCannotSupplyAcceleration() {
        let input = samples(noise: 0.8)
        XCTAssertNil(CameraMotionFit.estimate(input, distance: input.last!.value))
    }
    func testShortWindowUsesLinearFitWhileAccelerationSamplesAccumulate() throws {
        let input = (0...5).map { TimedValue(time: Double($0) * 0.2, value: 20 - 2 * Double($0) * 0.2) }
        let fit = try XCTUnwrap(CameraMotionFit.estimate(input, distance: 18))
        XCTAssertEqual(fit.rangeRate, -2, accuracy: 0.0001); XCTAssertNil(fit.closingAcceleration)
    }
    func testUnorderedRepeatedOrSeparatedSamplesCannotSupplyMotion() {
        var repeated = samples(); repeated[5] = repeated[4]
        var backwards = samples(); backwards.swapAt(5, 6)
        var separated = samples(); separated[8] = TimedValue(time: 5, value: 15)
        var invalid = samples(); invalid[8] = TimedValue(time: 0.8, value: .nan)
        for input in [repeated, backwards, separated, invalid] {
            XCTAssertNil(CameraMotionFit.estimate(input, distance: 15))
        }
    }
    func testFreshCameraRangePublishesAccelerationAndPausesDuringCoast() throws {
        var estimator = CameraRangeEstimator(); let id = UUID(); var result = CameraRangeSnapshot()
        for sample in samples(base: 100) {
            result = estimator.update(targetID: id, time: sample.time, distance: sample.value, confidence: 0.9, fresh: true)
        }
        XCTAssertEqual(try XCTUnwrap(result.closingSpeed), 6.5, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(result.closingAcceleration), 3, accuracy: 0.0001)
        let coast = estimator.update(targetID: id, time: 101.6, distance: 13, confidence: 0.9, fresh: false)
        XCTAssertNil(coast.distance); XCTAssertNil(coast.closingSpeed)
        XCTAssertNil(coast.rangeRate); XCTAssertNil(coast.closingAcceleration)
        let resumed = estimator.update(targetID: id, time: 101.7, distance: 20 - 2 * 1.7 - 1.5 * 1.7 * 1.7,
            confidence: 0.9, fresh: true)
        XCTAssertEqual(try XCTUnwrap(resumed.closingSpeed), 7.1, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(resumed.closingAcceleration), 3, accuracy: 0.0001)
    }
    func testNewTargetAndLongGapRemoveAccelerationHistory() {
        for newID in [false, true] {
            var estimator = CameraRangeEstimator(); let id = UUID()
            for sample in samples() {
                _ = estimator.update(targetID: id, time: sample.time, distance: sample.value, confidence: 0.9, fresh: true)
            }
            let result = estimator.update(targetID: newID ? UUID() : id, time: newID ? 1.6 : 2.2,
                distance: 13, confidence: 0.9, fresh: true)
            XCTAssertNil(result.closingSpeed); XCTAssertNil(result.closingAcceleration)
        }
    }
    func testReliableRapidClosureIsNotMisclassifiedAsRangeSpike() throws {
        var estimator = CameraRangeEstimator(); let id = UUID(); var result = CameraRangeSnapshot()
        for i in 0...17 {
            let t = Double(i) * 0.5
            result = estimator.update(targetID: id, time: t, distance: 80 - 8 * t, confidence: 0.9, fresh: true)
            if i >= 5 { XCTAssertNotNil(result.distance) }
        }
        XCTAssertEqual(result.distance, 12); XCTAssertEqual(try XCTUnwrap(result.closingSpeed), 8, accuracy: 0.0001)
        let spike = estimator.update(targetID: id, time: 8.6, distance: 30, confidence: 0.9, fresh: true)
        XCTAssertNil(spike.distance); XCTAssertNil(spike.closingAcceleration)
        let recovered = estimator.update(targetID: id, time: 8.7, distance: 10.4, confidence: 0.9, fresh: true)
        XCTAssertEqual(try XCTUnwrap(recovered.closingSpeed), 8, accuracy: 0.0001)
    }
    func testIncreasingClosurePipelineCanWarnWithoutLaneOrGPS() throws {
        var estimator = CameraRangeEstimator(); var latch = BrakeWarningLatch()
        let id = UUID(); var risk = RiskSnapshot()
        for i in 0...15 {
            let t = Double(i) * 0.1
            let distance = 27 - 2 * t - 3 * t * t
            let camera = estimator.update(targetID: id, time: t, distance: distance, confidence: 0.9, fresh: true)
            risk = CollisionApproachRules.forConfirmedLead(lead: RiskSnapshot(), camera: camera,
                confidence: 0.9, fresh: true, pathSupported: false, rearConfirmed: true)
            if risk.level == .critical { latch.trigger(at: t) }
        }
        XCTAssertEqual(risk.level, .critical)
        XCTAssertGreaterThan(try XCTUnwrap(risk.distance) / XCTUnwrap(risk.closingSpeed), 1.5)
        XCTAssertLessThan(try XCTUnwrap(risk.timeToCollision), 1.5)
        XCTAssertNil(risk.leadSpeed); XCTAssertNil(risk.leadAcceleration)
        XCTAssertTrue(latch.isActive(at: 6.49))
    }
}
