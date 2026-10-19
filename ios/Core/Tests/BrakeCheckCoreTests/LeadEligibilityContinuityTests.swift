import Foundation
import XCTest
@testable import BrakeCheckCore

final class LeadEligibilityContinuityTests: XCTestCase {
    private func rear(_ time: Double) -> VehicleViewpoint {
        var history = VehicleViewpointHistory()
        for capture in [time - 0.2, time] {
            history.append(VehicleViewpoint(scores: [0.02, 0.02, 0.02, 0.30, 0.30, 0.30, 0.02, 0.02], capturedAt: capture)!)
        }
        return history.latest(at: time)!
    }
    private func lane(_ time: Double, left: Double = 0.36, right: Double = 0.64) -> LanePath {
        LanePath(bands: (0..<13).map { LaneBand(y: 0.1 + Double($0) * 0.04, left: left, right: right) },
                 confidence: 0.9, capturedAt: time)
    }
    private func car(_ id: UUID, y: Double = 0.3, x: Double = 0.5, width: Double = 0.1,
                     view: VehicleViewpoint? = nil) -> LeadVehicle {
        LeadVehicle(box: CGRect(x: x - width / 2, y: y, width: width, height: width),
                    label: "car", confidence: 0.9, viewpoint: view, trackID: id)
    }
    private func confirm(_ selector: inout LeadSelector, id: UUID) throws -> LeadSelection {
        let view = rear(0.2)
        var result: LeadSelection?
        for time in [0.2, 0.3, 0.4] {
            result = selector.update([car(id, view: view)], at: time, lane: lane(time))
        }
        return try XCTUnwrap(result)
    }

    func testRearEvidenceSurvivesMissingChecksDuringInitialDetectorConfirmation() throws {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID()
        XCTAssertNil(selector.update([car(id, view: rear(0.2))], at: 0.2, lane: lane(0.2)))
        XCTAssertNil(selector.update([car(id)], at: 0.3, lane: lane(0.3)))
        let selected = try XCTUnwrap(selector.update([car(id)], at: 0.4, lane: lane(0.4)))
        XCTAssertEqual(selected.id, id)
        XCTAssertTrue(selected.isFreshDetection)
        XCTAssertTrue(selected.hasConfirmedRearIdentity)
        XCTAssertTrue(selected.isRearConfirmationRetained)
    }

    func testRearEvidenceCannotTransferToNewIDDuringInitialConfirmation() {
        var selector = LeadSelector(requiresRearView: true)
        let original = UUID(), other = UUID()
        XCTAssertNil(selector.update([car(original, view: rear(0.2))], at: 0.2, lane: lane(0.2)))
        for time in [0.3, 0.4, 0.5] {
            XCTAssertNil(selector.update([car(other)], at: time, lane: lane(time)))
        }
    }

    func testFrontViewVetoStillAppliesBeforeDetectorConfirmation() {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID()
        XCTAssertNil(selector.update([car(id, view: rear(0.2))], at: 0.2, lane: lane(0.2)))
        let front = VehicleViewpoint(scores: [0.30, 0.30, 0.02, 0.02, 0.02, 0.02, 0.02, 0.30], capturedAt: 0.3)!
        XCTAssertNil(selector.update([car(id, view: front)], at: 0.3, lane: lane(0.3)))
        XCTAssertNil(selector.update([car(id)], at: 0.4, lane: lane(0.4)))
    }

    func testRecentMeasuredCurveCanNominateLeadDuringBriefLaneGapWithoutAuthorizingMetrics() throws {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID()
        _ = selector.update([], at: 0, lane: lane(0, left: 0.6, right: 0.8))
        let candidate = car(id, x: 0.7, width: 0.08, view: rear(0.6))
        XCTAssertFalse(LeadVehicleGeometry.supportsBlindLead(candidate))
        XCTAssertNil(selector.update([candidate], at: 0.6))
        XCTAssertNil(selector.update([candidate], at: 0.7))
        let selected = try XCTUnwrap(selector.update([candidate], at: 0.8))
        XCTAssertEqual(selected.id, id)
        XCTAssertTrue(selected.isFreshDetection)
        XCTAssertFalse(selected.isPathSupported)
    }

    func testExpiredMeasuredCurveCannotAcquireOffAxisLead() {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID()
        _ = selector.update([], at: 0, lane: lane(0, left: 0.6, right: 0.8))
        _ = selector.update([], at: 0.5)
        for time in [0.9, 1.0, 1.1] {
            XCTAssertNil(selector.update([car(id, x: 0.7, width: 0.08, view: rear(time))], at: time))
        }
    }

    func testRecentMeasuredCurveStillExcludesAnAdjacentBlindAxisVehicle() {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID()
        _ = selector.update([], at: 0, lane: lane(0, left: 0.6, right: 0.8))
        for time in [0.6, 0.7, 0.8] {
            XCTAssertNil(selector.update([car(id, width: 0.08, view: rear(time))], at: time))
        }
    }

    func testWeakRecentLaneCannotNominateOffAxisLeadAfterItBecomesStale() {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID(), strong = lane(0, left: 0.6, right: 0.8)
        let weak = LanePath(bands: strong.bands, confidence: 0.6, capturedAt: 0)
        _ = selector.update([], at: 0, lane: weak)
        for time in [0.6, 0.7, 0.8] {
            XCTAssertNil(selector.update([car(id, x: 0.7, width: 0.08, view: rear(time))], at: time))
        }
    }

    func testEligibleVisibleCarConfirmsWhileFormerLeadCoastsOnVisualTrack() throws {
        var selector = LeadSelector(requiresRearView: true)
        let former = UUID(), next = UUID()
        let old = try confirm(&selector, id: former)
        for time in [0.5, 0.6] {
            let pending = try XCTUnwrap(selector.update([car(next, y: 0.48, width: 0.08, view: rear(0.5))],
                at: time, lane: lane(time), trackedBox: old.vehicle.box))
            XCTAssertEqual(pending.id, former)
            XCTAssertFalse(pending.isFreshDetection)
        }
        let selected = try XCTUnwrap(selector.update([car(next, y: 0.48, width: 0.08, view: rear(0.5))],
            at: 0.7, lane: lane(0.7), trackedBox: old.vehicle.box))
        XCTAssertEqual(selected.id, next)
        XCTAssertTrue(selected.isFreshDetection)
        XCTAssertTrue(selected.hasConfirmedRearIdentity)
    }

    func testChallengerKeepsItsOwnRearProofWhileFormerLeadIsMissing() throws {
        var selector = LeadSelector(requiresRearView: true)
        let former = UUID(), next = UUID()
        _ = try confirm(&selector, id: former)
        XCTAssertEqual(selector.update([car(next, y: 0.48, width: 0.08, view: rear(0.5))],
            at: 0.5, lane: lane(0.5))?.id, former)
        XCTAssertEqual(selector.update([car(next, y: 0.48, width: 0.08)], at: 0.6, lane: lane(0.6))?.id, former)
        let selected = try XCTUnwrap(selector.update([car(next, y: 0.48, width: 0.08)], at: 0.7, lane: lane(0.7)))
        XCTAssertEqual(selected.id, next)
        XCTAssertTrue(selected.isRearConfirmationRetained)
    }

    func testOldLeadExpiryPreservesObservationsAlreadyCollectedForReplacement() throws {
        var selector = LeadSelector(requiresRearView: true)
        let former = UUID(), next = UUID()
        _ = try confirm(&selector, id: former)
        for time in [0.5, 0.7, 0.9, 1.1] { _ = selector.update([], at: time) }
        for time in [1.2, 1.3] {
            _ = selector.update([car(next, y: 0.48, width: 0.08, view: rear(1.2))], at: time)
        }
        let selected = try XCTUnwrap(selector.update([car(next, y: 0.48, width: 0.08)], at: 1.4))
        XCTAssertEqual(selected.id, next)
        XCTAssertTrue(selected.isFreshDetection)
    }

    func testSecondSideCaptureAtFormerLeadExpiryVetoesPendingReplacement() throws {
        var selector = LeadSelector(requiresRearView: true)
        let former = UUID(), next = UUID()
        _ = try confirm(&selector, id: former)
        for time in [0.5, 0.7, 0.9, 1.1] { _ = selector.update([], at: time) }
        _ = selector.update([car(next, y: 0.48, width: 0.08, view: rear(1.2))], at: 1.2)
        for time in [1.25, 1.4] {
            let side = VehicleViewpoint(scores: [0.01, 0.01, 0.47, 0.01, 0.01, 0.01, 0.47, 0.01], capturedAt: time)!
            let selected = selector.update([car(next, y: 0.48, width: 0.08, view: side)], at: time)
            if time == 1.25 {
                XCTAssertEqual(selected?.id, former)
                XCTAssertFalse(selected?.isFreshDetection ?? true)
            } else { XCTAssertNil(selected) }
        }
        XCTAssertNil(selector.update([car(next, y: 0.48, width: 0.08)], at: 1.5))
    }

    func testAOneFrameChallengerCannotReplaceTheMissingLead() throws {
        var selector = LeadSelector(requiresRearView: true)
        let former = UUID(), next = UUID()
        let old = try confirm(&selector, id: former)
        _ = selector.update([car(next, y: 0.48, width: 0.08, view: rear(0.5))],
            at: 0.5, lane: lane(0.5), trackedBox: old.vehicle.box)
        for time in [0.6, 0.7, 0.8] {
            let held = try XCTUnwrap(selector.update([], at: time, lane: lane(time), trackedBox: old.vehicle.box))
            XCTAssertEqual(held.id, former)
            XCTAssertFalse(held.isFreshDetection)
        }
    }

    func testIndependentSideViewsVetoChallengerBeforeItCanAcquire() throws {
        var selector = LeadSelector(requiresRearView: true)
        let former = UUID(), next = UUID()
        let old = try confirm(&selector, id: former)
        _ = selector.update([car(next, y: 0.48, width: 0.08, view: rear(0.5))],
            at: 0.5, lane: lane(0.5), trackedBox: old.vehicle.box)
        for time in [0.6, 0.8] {
            let side = VehicleViewpoint(scores: [0.01, 0.01, 0.47, 0.01, 0.01, 0.01, 0.47, 0.01], capturedAt: time)!
            let held = try XCTUnwrap(selector.update([car(next, y: 0.48, width: 0.08, view: side)],
                at: time, lane: lane(time), trackedBox: old.vehicle.box))
            XCTAssertEqual(held.id, former)
            XCTAssertFalse(held.isFreshDetection)
        }
    }

    func testMissingLeadDoesNotHandOffToAdjacentRearCar() throws {
        var selector = LeadSelector(requiresRearView: true)
        let former = UUID(), adjacent = UUID()
        let old = try confirm(&selector, id: former)
        for time in [0.5, 0.6, 0.7, 0.8] {
            let held = try XCTUnwrap(selector.update([car(adjacent, y: 0.48, x: 0.8, width: 0.08, view: rear(time))],
                at: time, lane: lane(time), trackedBox: old.vehicle.box))
            XCTAssertEqual(held.id, former)
            XCTAssertFalse(held.isFreshDetection)
        }
    }

    func testSustainedLaneExitConfirmsReplacementDuringBoundaryGrace() throws {
        var selector = LeadSelector(requiresRearView: true)
        let former = UUID(), next = UUID()
        _ = try confirm(&selector, id: former)
        var selected: LeadSelection?
        for time in [0.5, 0.6, 0.7, 0.8] {
            selected = selector.update([car(former), car(next, y: 0.48, x: 0.8, width: 0.08, view: rear(0.5))],
                at: time, lane: lane(time, left: 0.72, right: 0.88))
            if time < 0.8 {
                XCTAssertEqual(selected?.id, former)
                XCTAssertFalse(selected?.isFreshDetection ?? true)
            }
        }
        XCTAssertEqual(try XCTUnwrap(selected).id, next)
        XCTAssertTrue(selected?.isFreshDetection == true)
    }
}
