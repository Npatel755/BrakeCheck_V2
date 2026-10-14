import Foundation
import XCTest
@testable import BrakeCheckCore

final class DangerConsistencyTests: XCTestCase {
    func testLeadConfirmationReusesAlreadyMeasuredCameraMotion() throws {
        var estimator = LeadRiskContinuity()
        let camera = CameraRangeSnapshot(distance: 18, closingSpeed: 2, rangeRate: -2)
        let first = estimator.update(time: 100, distance: 18, egoSpeed: 12, gpsAge: 0.1,
            confidence: 0.9, camera: camera)
        XCTAssertEqual(try XCTUnwrap(first.leadSpeed), 10, accuracy: 0.0001)
        XCTAssertEqual(first.closingSpeed, 2)
        XCTAssertEqual(first.timeToCollision, 9)
        XCTAssertEqual(first.headway, 1.5)
        XCTAssertNotNil(first.score)
        // Absolute acceleration still needs this lead's own speed history.
        XCTAssertNil(first.leadAcceleration)
    }

    func testCanonicalMotionCannotBypassFreshGPSOrMatchingRange() {
        for gpsAge in [2.1, Double.nan, Double.infinity] {
            var estimator = RiskEstimator()
            let result = estimator.update(time: 100, distance: 18, egoSpeed: 12, gpsAge: gpsAge,
                confidence: 0.9, camera: CameraRangeSnapshot(distance: 18, closingSpeed: 2, rangeRate: -2))
            XCTAssertNil(result.score); XCTAssertNil(result.leadSpeed)
        }
        for camera in [CameraRangeSnapshot(distance: 40, closingSpeed: 2, rangeRate: -2),
                       CameraRangeSnapshot(distance: 18, closingSpeed: 5, rangeRate: -2),
                       CameraRangeSnapshot(distance: 18)] {
            var estimator = RiskEstimator()
            let result = estimator.update(time: 100, distance: 18, egoSpeed: 12, gpsAge: 0,
                confidence: 0.9, camera: camera)
            XCTAssertNil(result.score); XCTAssertNil(result.leadSpeed)
        }
    }

    func testRapidClosureAtTwoHertzKeepsAbsoluteRiskAndRejectsSingleSpike() throws {
        var estimator = RiskEstimator(); var result = RiskSnapshot()
        for i in 0...17 {
            let t = Double(i) * 0.5
            result = estimator.update(time: t, distance: 80 - 8 * t, egoSpeed: 20,
                gpsAge: 0, confidence: 0.9)
            if i >= 5 {
                XCTAssertNotNil(result.score)
                XCTAssertEqual(try XCTUnwrap(result.leadSpeed), 12, accuracy: 0.0001)
            }
        }
        XCTAssertEqual(result.level, .critical)
        let spike = estimator.update(time: 8.6, distance: 30, egoSpeed: 20, gpsAge: 0, confidence: 0.9)
        XCTAssertNil(spike.score); XCTAssertNil(spike.leadSpeed)
        let resumed = estimator.update(time: 8.7, distance: 10.4, egoSpeed: 20, gpsAge: 0, confidence: 0.9)
        XCTAssertEqual(try XCTUnwrap(resumed.leadSpeed), 12, accuracy: 0.0001)
        XCTAssertEqual(resumed.level, .critical)
    }

    func testBriefDetectionMissHoldsOnlyDangerStepsAndCannotRenewHold() {
        var meter = LeadDangerContinuity(); let id = UUID()
        let measured = RiskSnapshot(level: .high, score: 78, distance: 12, closingSpeed: 5, timeToCollision: 2.4)
        XCTAssertEqual(meter.update(targetID: id, capturedAt: 10, now: 10, risk: measured).score, 78)
        let absent = RiskSnapshot(reason: "Detection gap")
        for time in [10.1, 10.3, 10.59, 11, 12.99, 13] {
            let held = meter.update(targetID: id, capturedAt: time, now: time, risk: absent)
            XCTAssertEqual(held.level, .high); XCTAssertEqual(held.score, 78)
            XCTAssertTrue(held.isHeld)
            XCTAssertNil(absent.distance); XCTAssertNil(absent.timeToCollision)
        }
        let expired = meter.update(targetID: id, capturedAt: 13.01, now: 13.01, risk: absent)
        XCTAssertNil(expired.score); XCTAssertEqual(expired.level, .unknown)
    }

    func testNewOrLostTargetDoesNotInheritDanger() {
        for next in [nil, UUID()] {
            var meter = LeadDangerContinuity()
            _ = meter.update(targetID: UUID(), capturedAt: 10, now: 10,
                risk: RiskSnapshot(level: .critical, score: 95))
            let changed = meter.update(targetID: next, capturedAt: 10.1, now: 10.1, risk: RiskSnapshot())
            XCTAssertNil(changed.score); XCTAssertFalse(changed.isHeld)
        }
    }

    func testRisingDangerIsImmediateButFallingBandRequiresNewCaptures() {
        var meter = LeadDangerContinuity(); let id = UUID()
        let elevated = RiskSnapshot(level: .elevated, score: 52)
        let high = RiskSnapshot(level: .high, score: 78)
        _ = meter.update(targetID: id, capturedAt: 10, now: 10, risk: elevated)
        XCTAssertEqual(meter.update(targetID: id, capturedAt: 10.1, now: 10.1, risk: high).level, .high)
        XCTAssertTrue(meter.update(targetID: id, capturedAt: 10.2, now: 10.2, risk: elevated).isHeld)
        // Timer ticks cannot become additional confirmation captures.
        XCTAssertEqual(meter.update(targetID: id, capturedAt: 10.2, now: 10.56, risk: elevated).level, .high)
        XCTAssertEqual(meter.update(targetID: id, capturedAt: 10.56, now: 10.56, risk: elevated).level, .elevated)
        XCTAssertEqual(meter.update(targetID: id, capturedAt: 10.57, now: 10.57,
            risk: RiskSnapshot(level: .critical, score: 95)).level, .critical)
    }

    func testExpiredUnorderedOrInvalidScoresNeverBecomeLowerIndication() {
        for time in [Double.nan, Double.infinity, 9, 13.01] {
            var meter = LeadDangerContinuity(); let id = UUID()
            _ = meter.update(targetID: id, capturedAt: 10, now: 10, risk: RiskSnapshot(level: .high, score: 78))
            let result = meter.update(targetID: id, capturedAt: time, now: 13.01, risk: RiskSnapshot())
            XCTAssertNil(result.score)
        }
        for score in [Double.nan, Double.infinity, -1, 101] {
            var meter = LeadDangerContinuity()
            XCTAssertNil(meter.update(targetID: UUID(), capturedAt: 10, now: 10,
                risk: RiskSnapshot(level: .low, score: score)).score)
        }
    }

    func testConfirmedNoVehicleClearsImmediatelyEvenWithHeldIdentity() {
        var meter = LeadDangerContinuity(); let id = UUID()
        _ = meter.update(targetID: id, capturedAt: 10, now: 10, risk: RiskSnapshot(level: .high, score: 78))
        let cleared = meter.update(targetID: id, capturedAt: 10.1, now: 10.1,
                                   risk: RiskSnapshot(), hasVehicle: false)
        XCTAssertNil(cleared.score)
        XCTAssertEqual(cleared.level, .unknown)
    }

    func testCarStillVisibleDuringLeadGapCanHoldButCannotRenewReading() {
        var meter = LeadDangerContinuity()
        _ = meter.update(targetID: UUID(), capturedAt: 10, now: 10, risk: RiskSnapshot(level: .high, score: 78))
        let held = meter.update(targetID: nil, capturedAt: 11, now: 11,
                                risk: RiskSnapshot(level: .low, score: 5), hasVehicle: true)
        XCTAssertEqual(held.score, 78); XCTAssertTrue(held.isHeld)
        XCTAssertNil(meter.update(targetID: nil, capturedAt: 13.01, now: 13.01,
                                 risk: RiskSnapshot(), hasVehicle: true).score)
    }

    func testDelayedValidScoreCannotStartOrRenewHold() {
        var meter = LeadDangerContinuity(); let id = UUID()
        let high = RiskSnapshot(level: .high, score: 78)
        XCTAssertNil(meter.update(targetID: id, capturedAt: 10, now: 11, risk: high).score)
        _ = meter.update(targetID: id, capturedAt: 12, now: 12, risk: high)
        XCTAssertTrue(meter.update(targetID: id, capturedAt: 12, now: 14.9, risk: high).isHeld)
        XCTAssertNil(meter.update(targetID: id, capturedAt: 12, now: 15.01, risk: high).score)
    }
}
