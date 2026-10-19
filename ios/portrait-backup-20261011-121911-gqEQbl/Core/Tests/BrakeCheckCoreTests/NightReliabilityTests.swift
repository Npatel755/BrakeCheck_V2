import Foundation
import XCTest
@testable import BrakeCheckCore

final class NightReliabilityTests: XCTestCase {
    private let box = CGRect(x: 0.35, y: 0.25, width: 0.30, height: 0.22)
    private func lane(_ time: Double) -> LanePath {
        LanePath(bands: (0..<13).map { LaneBand(y: 0.1 + Double($0) * 0.04, left: 0.30, right: 0.70) },
                 confidence: 0.9, capturedAt: time)
    }
    private func confirm(_ selector: inout LeadSelector, id: UUID) throws {
        var history = VehicleViewpointHistory()
        for time in [0.0, 0.2] {
            history.append(VehicleViewpoint(scores: [0.02, 0.02, 0.02, 0.30, 0.30, 0.30, 0.02, 0.02], capturedAt: time)!)
        }
        var selection: LeadSelection?
        for time in [0.2, 0.3, 0.4] {
            selection = selector.update([LeadVehicle(box: box, label: "car", confidence: 0.9,
                viewpoint: history.latest(at: time), trackID: id)], at: time, lane: lane(time))
        }
        XCTAssertEqual(try XCTUnwrap(selection).id, id)
    }
    func testConfirmedLeadSurvivesLaneSupportedGlareWithoutInventingRange() throws {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID(); try confirm(&selector, id: id)
        for time in [0.6, 1.0, 1.5, 2.0] {
            let held = try XCTUnwrap(selector.update([], at: time, lane: lane(time)))
            XCTAssertEqual(held.id, id); XCTAssertFalse(held.isFreshDetection)
        }
        let fresh = try XCTUnwrap(selector.update([LeadVehicle(box: box, label: "car", confidence: 0.82,
            trackID: id)], at: 2.1, lane: lane(2.1)))
        XCTAssertEqual(fresh.id, id); XCTAssertTrue(fresh.isFreshDetection)
        XCTAssertTrue(fresh.hasConfirmedRearIdentity)
    }
    func testLaneGraceExpiresAndDoesNotRenewFromPredictions() throws {
        var selector = LeadSelector(requiresRearView: true)
        try confirm(&selector, id: UUID())
        for time in [0.6, 1.0, 1.5, 2.0] { _ = selector.update([], at: time, lane: lane(time)) }
        XCTAssertNil(selector.update([], at: 2.3, lane: lane(2.3)))
    }
    func testNoLaneDoesNotAuthorizeExtendedBlindLock() throws {
        var selector = LeadSelector(requiresRearView: true)
        try confirm(&selector, id: UUID())
        _ = selector.update([], at: 0.8)
        XCTAssertNil(selector.update([], at: 1.4))
    }
    func testOverlappingNewIdCannotInheritLaneGraceOrRearEvidence() throws {
        var selector = LeadSelector(requiresRearView: true)
        try confirm(&selector, id: UUID())
        XCTAssertNil(selector.update([LeadVehicle(box: box, label: "car", confidence: 0.90,
            trackID: UUID())], at: 1.5, lane: lane(1.5)))
    }
    func testVehicleTrackerCanBridgeCropShrinkOnlyForTheVisuallySupportedLead() throws {
        let shrunk = CGRect(x: box.midX - box.width * 0.61 / 2, y: box.midY - box.height * 0.61 / 2,
                            width: box.width * 0.61, height: box.height * 0.61)
        for supported in [false, true] {
            var tracks = VehicleHeadingTracks()
            let id = try XCTUnwrap(tracks.update([LeadVehicle(box: box, label: "car", confidence: 0.9)], at: 0).first?.trackID)
            let next = try XCTUnwrap(tracks.update([LeadVehicle(box: shrunk, label: "car", confidence: 0.9)],
                at: 0.2, preferredID: supported ? id : nil, trackedBox: box).first)
            if supported { XCTAssertEqual(next.trackID, id) }
            else { XCTAssertNotEqual(next.trackID, id) }
        }
    }
    func testConflictingGlareCropsStillInvalidateAssociation() throws {
        var tracks = VehicleHeadingTracks()
        let id = try XCTUnwrap(tracks.update([LeadVehicle(box: box, label: "car", confidence: 0.9)], at: 0).first?.trackID)
        let result = tracks.update([-0.015, 0.015].map {
            LeadVehicle(box: box.offsetBy(dx: $0, dy: 0), label: "car", confidence: 0.9)
        }, at: 0.2, preferredID: id, trackedBox: box)
        XCTAssertTrue(result.allSatisfy { $0.trackID != id && $0.viewpoint == nil })
    }
    private func sampledRisk() -> LeadRiskContinuity {
        var estimator = LeadRiskContinuity()
        for i in 0...5 {
            let time = Double(i) * 0.2
            _ = estimator.update(time: time, distance: 20 - time * 2, egoSpeed: 10, gpsAge: 0, confidence: 0.9)
        }
        return estimator
    }
    func testShortGapHidesRiskAndResumesWithoutCollectingAllSamplesAgain() throws {
        var estimator = sampledRisk()
        let paused = estimator.pause(at: 1.2, reason: "Detection gap")
        XCTAssertNil(paused.score); XCTAssertNil(paused.leadSpeed); XCTAssertNil(paused.timeToCollision)
        let resumed = estimator.update(time: 1.4, distance: 17.2, egoSpeed: 10, gpsAge: 0, confidence: 0.9)
        XCTAssertEqual(try XCTUnwrap(resumed.leadSpeed), 8, accuracy: 0.0001)
    }
    func testWeakFrameDoesNotBecomeSpeedOrEraseGoodSamples() throws {
        var estimator = sampledRisk()
        let weak = estimator.update(time: 1.2, distance: 17.6, egoSpeed: 10, gpsAge: 0, confidence: 0.50)
        XCTAssertNil(weak.leadSpeed); XCTAssertNil(weak.score)
        let resumed = estimator.update(time: 1.4, distance: 17.2, egoSpeed: 10, gpsAge: 0, confidence: 0.9)
        XCTAssertEqual(try XCTUnwrap(resumed.leadSpeed), 8, accuracy: 0.0001)
    }
    func testRepeatedCoastingCannotRenewReliableHistory() {
        var estimator = sampledRisk()
        for time in [1.2, 1.4, 1.8] { XCTAssertNil(estimator.pause(at: time, reason: "Gap").score) }
        XCTAssertNil(estimator.update(time: 2, distance: 16, egoSpeed: 10, gpsAge: 0, confidence: 0.9).leadSpeed)
    }
    func testResetForNewTargetClearsRetainedRiskHistory() {
        var estimator = sampledRisk(); estimator.reset()
        XCTAssertNil(estimator.update(time: 1.2, distance: 40, egoSpeed: 10, gpsAge: 0, confidence: 0.9).leadSpeed)
    }
    func testOutOfOrderAndInvalidConfidenceCannotResumeRisk() {
        var estimator = sampledRisk()
        XCTAssertNil(estimator.update(time: 0.9, distance: 18.2, egoSpeed: 10, gpsAge: 0, confidence: 0.9).leadSpeed)
        estimator = sampledRisk()
        XCTAssertNil(estimator.update(time: 1.2, distance: 17.6, egoSpeed: 10, gpsAge: 0, confidence: .infinity).leadSpeed)
    }
    func testNightAndHeatBudgetsLimitAuxiliaryWork() {
        let day = VisionWorkPolicy(night: false, heat: .nominal)
        let night = VisionWorkPolicy(night: true, heat: .nominal)
        XCTAssertEqual(day.vehicleInterval, 0.10)
        XCTAssertEqual(night.vehicleInterval, 0.20)
        XCTAssertEqual(night.laneInterval, 1.0 / 3)
        for heat in [VisionHeat.fair, .serious, .critical] {
            let policy = VisionWorkPolicy(night: true, heat: heat)
            XCTAssertGreaterThanOrEqual(policy.vehicleInterval, night.vehicleInterval)
            XCTAssertGreaterThanOrEqual(policy.laneInterval, night.laneInterval)
            XCTAssertGreaterThanOrEqual(policy.viewpointInterval, night.viewpointInterval)
        }
    }
    func testSlowInferenceTrimsAuxiliaryCadence() {
        let policy = VisionWorkPolicy(night: false, heat: .nominal, vehicleLatency: 0.25)
        XCTAssertEqual(policy.vehicleInterval, 0.10)
        XCTAssertEqual(policy.laneInterval, 0.40); XCTAssertEqual(policy.viewpointInterval, 0.50)
    }
}
