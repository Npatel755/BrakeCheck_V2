import Foundation
import XCTest
@testable import BrakeCheckCore

final class LeadStabilityTests: XCTestCase {
    private let box = CGRect(x: 0.35, y: 0.25, width: 0.30, height: 0.22)
    private func car(_ box: CGRect, id: UUID? = nil, confidence: Double = 0.9) -> LeadVehicle {
        LeadVehicle(box: box, label: "car", confidence: confidence, trackID: id)
    }
    private func rear(_ time: Double) -> VehicleViewpoint {
        VehicleViewpoint(scores: [0.02, 0.02, 0.02, 0.30, 0.30, 0.30, 0.02, 0.02], capturedAt: time)!
    }
    private func lane(_ time: Double) -> LanePath {
        LanePath(bands: (0..<13).map { LaneBand(y: 0.1 + Double($0) * 0.04, left: 0.35, right: 0.65) },
                 confidence: 0.9, capturedAt: time)
    }
    func testResolvedAmbiguityStopsRecyclingIDsAndCanConfirmAgain() throws {
        var tracks = VehicleHeadingTracks()
        let old = try XCTUnwrap(tracks.update([car(box)], at: 0).first?.trackID)
        XCTAssertTrue(tracks.record(rear(0), for: old, at: 0))
        _ = tracks.update([car(box.offsetBy(dx: -0.01, dy: 0)), car(box.offsetBy(dx: 0.01, dy: 0))], at: 0.1)
        _ = tracks.update([car(box)], at: 0.2)
        let resolved = try XCTUnwrap(tracks.update([car(box)], at: 0.3).first?.trackID)
        XCTAssertNotEqual(old, resolved)
        XCTAssertFalse(tracks.record(rear(0), for: old, at: 0.3))
        XCTAssertTrue(tracks.record(rear(0.3), for: resolved, at: 0.3))
        for time in [0.4, 0.5, 0.6, 0.7] {
            XCTAssertEqual(tracks.update([car(box)], at: time).first?.trackID, resolved)
        }
        XCTAssertTrue(tracks.record(rear(0.7), for: resolved, at: 0.7))
        XCTAssertTrue(tracks.viewpoint(for: resolved, at: 0.7)?.hasConfirmedRearView(at: 0.7) == true)
    }
    func testOngoingCrossingCannotRetainRearEvidence() throws {
        var tracks = VehicleHeadingTracks()
        let old = try XCTUnwrap(tracks.update([car(box)], at: 0).first?.trackID)
        _ = tracks.record(rear(0), for: old, at: 0)
        for time in [0.2, 0.4, 0.6] {
            let crossed = tracks.update([car(box.offsetBy(dx: -0.01, dy: 0)), car(box.offsetBy(dx: 0.01, dy: 0))], at: time)
            XCTAssertTrue(crossed.allSatisfy { $0.trackID != old && $0.viewpoint == nil })
        }
    }
    func testTwoHertzCameraHistoryCanProduceClosingSpeed() throws {
        var estimator = CameraRangeEstimator()
        let id = UUID(); var result = CameraRangeSnapshot()
        for i in 0...8 {
            let time = Double(i) * 0.5
            result = estimator.update(targetID: id, time: time, distance: 30 - 2 * time, confidence: 0.9, fresh: true)
        }
        XCTAssertEqual(try XCTUnwrap(result.closingSpeed), 2, accuracy: 0.0001)
        XCTAssertNil(estimator.update(targetID: id, time: 4.1, distance: 22, confidence: 0.9, fresh: false).closingSpeed)
        XCTAssertNil(estimator.update(targetID: id, time: 4.7, distance: 20.6, confidence: 0.9, fresh: true).closingSpeed)
    }
    private func movingCar(_ time: Double, id: UUID, confidence: Double = 0.9) -> LeadVehicle {
        let width = 1.8 / (2 * tan(60.0 * .pi / 360) * (30 - 2 * time))
        return car(CGRect(x: 0.5 - width / 2, y: 0.3, width: width, height: width), id: id, confidence: confidence)
    }
    func testTwoHertzMotionCanAuthorizeFreshApproach() throws {
        var motion = VehicleMotionGuard(); let id = UUID(); var result: LeadVehicle?
        for i in 0...6 {
            let time = Double(i) * 0.5
            result = motion.update([movingCar(time, id: id)], at: time, horizontalFOV: 60, ego: nil).first
        }
        XCTAssertTrue(try XCTUnwrap(result?.travel).supportsCollisionApproach(at: 3))
        XCTAssertTrue(motion.update([], at: 3.1, horizontalFOV: 60, ego: nil).isEmpty)
    }
    func testWeakMotionFramePausesWithoutErasingReliableSamples() throws {
        var motion = VehicleMotionGuard(); let id = UUID()
        for time in [0.0, 0.2, 0.4] { _ = motion.update([movingCar(time, id: id)], at: time, horizontalFOV: 60, ego: nil) }
        XCTAssertNil(motion.update([movingCar(0.6, id: id, confidence: 0.3)], at: 0.6, horizontalFOV: 60, ego: nil).first?.travel)
        let result = motion.update([movingCar(0.8, id: id)], at: 0.8, horizontalFOV: 60, ego: nil).first
        XCTAssertTrue(try XCTUnwrap(result?.travel).supportsCollisionApproach(at: 0.8))
    }
    func testOneBadRangeDoesNotEraseMotionOrPublishTheSpike() throws {
        var estimator = CameraRangeEstimator(); let id = UUID()
        for i in 0...12 {
            let t = Double(i) * 0.1
            _ = estimator.update(targetID: id, time: t, distance: 20 - 2 * t, confidence: 0.9, fresh: true)
        }
        let spike = estimator.update(targetID: id, time: 1.3, distance: 40, confidence: 0.9, fresh: true)
        XCTAssertNil(spike.distance); XCTAssertNil(spike.closingSpeed)
        let recovered = estimator.update(targetID: id, time: 1.4, distance: 17.2, confidence: 0.9, fresh: true)
        XCTAssertEqual(try XCTUnwrap(recovered.closingSpeed), 2, accuracy: 0.0001)
    }
    func testSustainedRangeChangeStartsNewSamples() {
        var estimator = CameraRangeEstimator(); let id = UUID()
        for i in 0...12 { _ = estimator.update(targetID: id, time: Double(i) * 0.1, distance: 20, confidence: 0.9, fresh: true) }
        _ = estimator.update(targetID: id, time: 1.3, distance: 40, confidence: 0.9, fresh: true)
        let changed = estimator.update(targetID: id, time: 1.5, distance: 40, confidence: 0.9, fresh: true)
        XCTAssertEqual(changed.distance, 40); XCTAssertNil(changed.closingSpeed)
    }
    func testSlowRearSlotsDoNotStarveOtherVehicles() throws {
        let ids = [UUID(), UUID()]; var scheduler = VehicleViewpointScheduler(); var chosen: Set<UUID> = []
        for i in 0...8 {
            let t = Double(i) * 0.75
            if let id = scheduler.choose(ids.map { car(box, id: $0) }, at: t, preferredID: ids[0], lane: nil)?.trackID { chosen.insert(id) }
        }
        XCTAssertEqual(chosen, Set(ids))
    }
    func testSlowRearClassifierCompletesTwoCapturesBeforeTheyExpire() {
        let ids = [UUID(), UUID()]; var scheduler = VehicleViewpointScheduler()
        var histories = Dictionary(uniqueKeysWithValues: ids.map { ($0, VehicleViewpointHistory()) })
        var confirmed: Set<UUID> = []
        for i in 0...12 {
            let time = Double(i) * 0.75
            let vehicles = ids.map { LeadVehicle(box: box, label: "car", confidence: 0.9,
                viewpoint: histories[$0]?.latest(at: time), trackID: $0) }
            if let id = scheduler.choose(vehicles, at: time, preferredID: ids[0], lane: nil)?.trackID {
                histories[id]?.append(rear(time))
                // Model output arrives a quarter second after its capture.
                if histories[id]?.latest(at: time + 0.25)?.hasConfirmedRearView(at: time + 0.25) == true { confirmed.insert(id) }
            }
        }
        XCTAssertEqual(confirmed, Set(ids))
    }
    func testAdjacentCarDoesNotPreventSearchingAnEmptyMeasuredLane() {
        var recovery = VehicleDetectionRecovery()
        let adjacent = car(CGRect(x: 0.05, y: 0.25, width: 0.15, height: 0.15))
        XCTAssertNotNil(recovery.propose([adjacent], at: 1, preferredBox: nil, lastDetectedAt: nil,
            trackedBox: nil, lane: lane(1), imageAspect: 16.0 / 9, interval: 0.75, captureAge: 0.02, vehicleLatency: 0.02))
    }
    func testUnreliableApproachDoesNotDiscardExistingLeadMeasurements() {
        let lead = RiskSnapshot(distance: 20, leadSpeed: 8, reason: "Sampling")
        let combined = CollisionApproachRules.combine(lead: lead, approach: RiskSnapshot())
        XCTAssertEqual(combined.distance, 20); XCTAssertEqual(combined.leadSpeed, 8)
    }
    func testSlowRearClassifierLeavesCapacityForVehicleAndLaneModels() {
        var budget = VehicleViewpointBudget()
        XCTAssertEqual(budget.interval(minimum: 0.25), 0.25)
        XCTAssertEqual(budget.laneInterval(minimum: 0.2), 0.2)
        budget.record(predictionSeconds: 0.242)
        XCTAssertEqual(budget.interval(minimum: 0.25), 0.726, accuracy: 0.0001)
        XCTAssertEqual(budget.laneInterval(minimum: 0.2), 1.0 / 3, accuracy: 0.0001)
        XCTAssertEqual(budget.laneInterval(minimum: 0.5), 0.5)
        budget.record(predictionSeconds: .nan)
        XCTAssertEqual(budget.interval(minimum: 0.50), 0.726, accuracy: 0.0001)
        budget.record(predictionSeconds: 0.1)
        XCTAssertGreaterThan(budget.interval(minimum: 0.25), 0.50)
        budget.reset(); XCTAssertEqual(budget.interval(minimum: 0.5), 0.5)
    }
    func testCameraClosingRiskOnConfirmedLeadDoesNotRequireGPS() {
        let camera = CameraRangeSnapshot(distance: 10, closingSpeed: 5)
        let result = CollisionApproachRules.forConfirmedLead(lead: RiskSnapshot(), camera: camera,
            confidence: 0.9, fresh: true, pathSupported: true, rearConfirmed: true)
        XCTAssertEqual(result.timeToCollision, 2); XCTAssertEqual(result.score, 78)
        XCTAssertNil(result.leadSpeed); XCTAssertNil(result.leadAcceleration)
    }
    func testCameraLeadRiskStillNeedsFreshPathRearAndConfidentRange() {
        for flags in [(false, true, true), (true, false, true), (true, true, false)] {
            XCTAssertNil(CollisionApproachRules.forConfirmedLead(lead: RiskSnapshot(),
                camera: CameraRangeSnapshot(distance: 10, closingSpeed: 5), confidence: 0.9,
                fresh: flags.0, pathSupported: flags.1, rearConfirmed: flags.2).score)
        }
        for confidence in [0.5, Double.nan] {
            XCTAssertNil(CollisionApproachRules.forConfirmedLead(lead: RiskSnapshot(),
                camera: CameraRangeSnapshot(distance: 10, closingSpeed: 5), confidence: confidence,
                fresh: true, pathSupported: true, rearConfirmed: true).score)
        }
        XCTAssertNil(CollisionApproachRules.forConfirmedLead(lead: RiskSnapshot(), camera: CameraRangeSnapshot(),
            confidence: 0.9, fresh: true, pathSupported: true, rearConfirmed: true).score)
    }
    func testValidGPSLeadRiskIsNotReducedByCameraTTC() {
        let lead = RiskSnapshot(level: .critical, score: 95, distance: 10, leadSpeed: 8, leadAcceleration: -6)
        let result = CollisionApproachRules.forConfirmedLead(lead: lead,
            camera: CameraRangeSnapshot(distance: 10, closingSpeed: 1), confidence: 0.9,
            fresh: true, pathSupported: true, rearConfirmed: true)
        XCTAssertEqual(result.score, 95); XCTAssertEqual(result.leadSpeed, 8); XCTAssertEqual(result.leadAcceleration, -6)
    }
    func testWeakMotionCannotKeepHistoryAliveIndefinitely() {
        var motion = VehicleMotionGuard(); let id = UUID()
        for time in [0.0, 0.2, 0.4] { _ = motion.update([movingCar(time, id: id)], at: time, horizontalFOV: 60, ego: nil) }
        for time in [0.6, 0.8, 1.0, 1.2] {
            XCTAssertNil(motion.update([movingCar(time, id: id, confidence: 0.3)], at: time, horizontalFOV: 60, ego: nil).first?.travel)
        }
        XCTAssertNil(motion.update([movingCar(1.4, id: id)], at: 1.4, horizontalFOV: 60, ego: nil).first?.travel)
    }
    func testRecoveryStillRequiresFreshLaneBeforeIgnoringAdjacentCars() {
        let adjacent = car(CGRect(x: 0.05, y: 0.25, width: 0.15, height: 0.15))
        for path in [nil, lane(0)] {
            var recovery = VehicleDetectionRecovery()
            XCTAssertNil(recovery.propose([adjacent], at: 2, preferredBox: nil, lastDetectedAt: nil,
                trackedBox: nil, lane: path, imageAspect: 16.0 / 9, interval: 0.75, captureAge: 0.02, vehicleLatency: 0.02))
        }
    }
}
