import Foundation
import XCTest
@testable import BrakeCheckCore

final class LeadLaneEvidenceTests: XCTestCase {
    private func observation(_ time: Double, shift: Double = 0, confidence: Double = 0.9,
                             slots: [Int] = [1, 2], rows: [Double]? = nil) -> LanePredictionObservation {
        let rows = rows ?? (0..<14).map { 0.12 + Double($0) * 0.02 }
        return .init(capturedAt: time, boundaries: slots.map { slot in
            .init(laneSlot: slot, confidence: confidence, points: rows.map { y in
                let center = 0.50 + shift + y * y * 0.20
                let half = 0.29 - y * 0.42
                return .init(x: center + (slot == 1 ? -half : half), y: y)
            })
        })
    }
    private func confirmed(_ estimator: inout LeadLaneEvidenceEstimator, start: Double = 10) throws -> LanePath {
        XCTAssertNil(estimator.update(observation(start), at: start))
        XCTAssertNil(estimator.update(observation(start + 0.2), at: start + 0.2))
        return try XCTUnwrap(estimator.update(observation(start + 0.4), at: start + 0.4))
    }

    func testShortObservedEgoPairConfirmsOnlyAfterIndependentConsistentCaptures() throws {
        var estimator = LeadLaneEvidenceEstimator()
        let path = try confirmed(&estimator)
        XCTAssertEqual(path.capturedAt, 10.4)
        XCTAssertEqual(path.bands.first?.y, 0.12)
        XCTAssertEqual(path.bands.last?.y ?? 0, 0.38, accuracy: 1e-9)
        XCTAssertEqual(path.confidence, 0.9)
        XCTAssertEqual(estimator.status, "confirmed observed lane pair")
        XCTAssertTrue(path.isUsable(at: 10.5))
        XCTAssertNil(path.bounds(at: 0.4)) // no extrapolation toward a farther vehicle
    }

    func testHighFrameRateCannotBypassMinimumConfirmationSpan() {
        var estimator = LeadLaneEvidenceEstimator()
        for t in [10.0, 10.01, 10.02, 10.03] {
            XCTAssertNil(estimator.update(observation(t), at: t))
        }
    }

    func testDuplicateCaptureCannotBeCountedAsNewEvidence() {
        var estimator = LeadLaneEvidenceEstimator()
        for _ in 0..<4 { XCTAssertNil(estimator.update(observation(10), at: 10)) }
    }

    func testMissingWeakSingleSideAndOuterSlotsCannotAuthorizeLeadCorridor() {
        for input in [observation(10, confidence: 0.81), observation(10, slots: [1]),
                      observation(10, slots: [0, 3]), observation(10, rows: [0.1, 0.12, 0.14]) ] {
            var estimator = LeadLaneEvidenceEstimator()
            XCTAssertNil(estimator.update(input, at: 10))
            XCTAssertEqual(estimator.status, "waiting for lane pair")
        }
    }

    func testNegativeObservationAndClockDiscontinuityEndConfirmedEvidence() throws {
        var estimator = LeadLaneEvidenceEstimator()
        _ = try confirmed(&estimator)
        XCTAssertNil(estimator.update(nil, at: 10.5))
        XCTAssertNil(estimator.update(observation(10.6), at: 10.6))
        XCTAssertNil(estimator.update(observation(9), at: 9))
        XCTAssertNil(estimator.update(observation(12), at: 12))
        XCTAssertNil(estimator.update(observation(.nan), at: .nan))
    }

    func testChangedRoadMustReconfirmInsteadOfInheritingPriorVotes() throws {
        var estimator = LeadLaneEvidenceEstimator()
        _ = try confirmed(&estimator)
        XCTAssertNil(estimator.update(observation(10.6, shift: 0.12), at: 10.6))
        XCTAssertNil(estimator.update(observation(10.8, shift: 0.12), at: 10.8))
        XCTAssertNotNil(estimator.update(observation(11, shift: 0.12), at: 11))
    }

    func testVehicleOcclusionCannotTurnTwoDisconnectedShortPiecesIntoOneLongPair() {
        var estimator = LeadLaneEvidenceEstimator()
        let rows = [0.1, 0.12, 0.14, 0.16, 0.30, 0.32, 0.34, 0.36]
        for t in [10.0, 10.2, 10.4] {
            XCTAssertNil(estimator.update(observation(t, rows: rows), at: t))
        }
    }

    func testFutureNegativeDoesNotEraseHistoricalVehicleFrameButClearsCurrent() throws {
        var estimator = LeadLaneEvidenceEstimator(), buffer = LeadLaneEvidenceBuffer()
        let path = try confirmed(&estimator)
        buffer.record(path, at: 10.4)
        buffer.record(nil, at: 10.6)
        XCTAssertEqual(buffer.latest(at: 10.5)?.capturedAt, 10.4)
        XCTAssertNil(buffer.latest(at: 10.6))
        XCTAssertNil(buffer.latest(at: 10.3))
        XCTAssertNil(buffer.latest(at: .nan))
    }

    func testBufferCannotRefreshTimestampReuseStaleOrAcceptMismatchedPath() throws {
        var estimator = LeadLaneEvidenceEstimator(), buffer = LeadLaneEvidenceBuffer()
        let path = try confirmed(&estimator)
        buffer.record(path, at: 10.4)
        buffer.record(path, at: 10.8)
        XCTAssertEqual(buffer.latest(at: 10.85)?.capturedAt, 10.4)
        XCTAssertNil(buffer.latest(at: 10.91))
        buffer.reset()
        XCTAssertNil(buffer.latest(at: 10.4))
    }

    private func vehicle(_ id: UUID, x: Double, time: Double, rear: Bool = false) -> LeadVehicle {
        let view = rear ? VehicleViewpoint(scores: [0.02, 0.02, 0.02, 0.3, 0.3, 0.3, 0.02, 0.02], capturedAt: time)! : nil
        let confirmed = view.map { VehicleViewpoint(latest: $0, rearCount: 2, rearSpan: 0.2) }
        return LeadVehicle(box: CGRect(x: x - 0.05, y: 0.25, width: 0.10, height: 0.10),
                           label: "car", confidence: 0.9, viewpoint: confirmed, trackID: id)
    }

    func testMeasuredBendSelectsRearConfirmedLeadOutsideBlindCameraFootprint() throws {
        let id = UUID(), adjacent = UUID()
        var estimator = LeadLaneEvidenceEstimator()
        var selector = LeadSelector(requiresRearView: true)
        var noLane = LeadSelector(requiresRearView: true)
        var result: LeadSelection?, baseline: LeadSelection?
        for i in 0..<8 {
            let t = 10 + Double(i) * 0.2
            let path = estimator.update(observation(t, shift: 0.18), at: t)
            let cars = [vehicle(adjacent, x: 0.40, time: t, rear: true), vehicle(id, x: 0.69, time: t, rear: true)]
            result = selector.update(cars, at: t, lane: path)
            baseline = noLane.update(cars, at: t)
        }
        XCTAssertEqual(result?.id, id)
        XCTAssertTrue(try XCTUnwrap(result).isPathSupported)
        XCTAssertNotEqual(baseline?.id, id)
    }

    func testLanePairNeverInventsRearOrientationForUnknownVehicle() {
        var estimator = LeadLaneEvidenceEstimator(), selector = LeadSelector(requiresRearView: true)
        let id = UUID()
        for i in 0..<8 {
            let t = 10 + Double(i) * 0.2
            let lane = estimator.update(observation(t), at: t)
            XCTAssertNil(selector.update([vehicle(id, x: 0.5, time: t)], at: t, lane: lane))
        }
    }

    func testLaneAwareViewpointWorkPrefersInLaneUnknownOverAdjacentPendingRear() throws {
        var estimator = LeadLaneEvidenceEstimator()
        let path = try confirmed(&estimator)
        let lead = vehicle(UUID(), x: 0.51, time: 10.4)
        let pendingView = VehicleViewpoint(scores: [0.02, 0.02, 0.02, 0.3, 0.3, 0.3, 0.02, 0.02], capturedAt: 10.4)!
        let beside = LeadVehicle(box: CGRect(x: 0.82, y: 0.25, width: 0.12, height: 0.15),
                                label: "car", confidence: 0.95, viewpoint: pendingView, trackID: UUID())
        var scheduler = VehicleViewpointScheduler()
        XCTAssertEqual(scheduler.choose([beside, lead], at: 10.4, preferredID: nil, lane: path)?.trackID, lead.trackID)
    }

    func testRecoverySearchesEgoLaneInsteadOfContinuingToZoomAdjacentOldTarget() throws {
        var estimator = LeadLaneEvidenceEstimator(), recovery = VehicleDetectionRecovery()
        let path = try confirmed(&estimator)
        let beside = LeadVehicle(box: CGRect(x: 0.84, y: 0.25, width: 0.1, height: 0.14), label: "car", confidence: 0.95)
        let crop = try XCTUnwrap(recovery.propose([beside], at: 10.4, preferredBox: beside.box,
            lastDetectedAt: 10.3, trackedBox: beside.box, lane: path, imageAspect: 16.0/9.0,
            interval: 1, captureAge: 0.05, vehicleLatency: 0.08))
        XCTAssertEqual(crop.region.width, 0.64, accuracy: 1e-9)
        XCTAssertLessThan(crop.region.midX, 0.65)
    }

    func testSingleRightBoundaryExcludesAdjacentSUVWithoutInventingTheLeftSide() {
        let input = observation(10, slots: [2])
        let beside = CGRect(x: 0.82, y: 0.25, width: 0.12, height: 0.13)
        let ahead = CGRect(x: 0.45, y: 0.25, width: 0.10, height: 0.10)
        XCTAssertTrue(LeadLaneRelevance.excludes(beside, boundaries: input, at: 10))
        XCTAssertFalse(LeadLaneRelevance.excludes(ahead, boundaries: input, at: 10))
        var estimator = LeadLaneEvidenceEstimator()
        XCTAssertNil(estimator.update(input, at: 10))
    }

    func testWeakStaleFutureAndOutOfSpanBoundaryCannotExcludeVehicle() {
        let box = CGRect(x: 0.82, y: 0.25, width: 0.12, height: 0.13)
        XCTAssertFalse(LeadLaneRelevance.excludes(box, boundaries: observation(10, confidence: 0.79, slots: [2]), at: 10))
        for time in [9.9, 10.501, .nan] {
            XCTAssertFalse(LeadLaneRelevance.excludes(box, boundaries: observation(10, slots: [2]), at: time))
        }
        XCTAssertFalse(LeadLaneRelevance.excludes(box.offsetBy(dx: 0, dy: 0.4), boundaries: observation(10, slots: [2]), at: 10))
    }

    func testConflictingSameSlotBranchesDoNotExcludeArbitrarily() {
        let a = observation(10, slots: [2]), b = observation(10, shift: 0.10, slots: [2])
        let conflict = LanePredictionObservation(capturedAt: 10, boundaries: a.boundaries + b.boundaries)
        XCTAssertFalse(LeadLaneRelevance.excludes(CGRect(x: 0.82, y: 0.25, width: 0.12, height: 0.13), boundaries: conflict, at: 10))
    }

    func testOneSidedEvidenceDirectsRearCheckBeforeAdjacentPendingCrop() {
        let id = UUID(), besideID = UUID()
        let lead = vehicle(id, x: 0.50, time: 10)
        let rear = VehicleViewpoint(scores: [0.02, 0.02, 0.02, 0.3, 0.3, 0.3, 0.02, 0.02], capturedAt: 10)!
        let beside = LeadVehicle(box: CGRect(x: 0.82, y: 0.25, width: 0.12, height: 0.13), label: "car",
                                 confidence: 0.95, viewpoint: rear, trackID: besideID)
        var old = VehicleViewpointScheduler(), updated = VehicleViewpointScheduler()
        XCTAssertEqual(old.choose([beside, lead], at: 10, preferredID: nil, lane: nil)?.trackID, besideID)
        XCTAssertEqual(updated.choose([beside, lead], at: 10, preferredID: nil, lane: nil,
                                      boundaries: observation(10, slots: [2]))?.trackID, id)
    }

    func testLaneConstraintReleasesAdjacencyWithoutReusingRearIdentityForDifferentCar() throws {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID()
        var selected: LeadSelection?
        for i in 0..<4 {
            let t = 10 + Double(i) * 0.1
            selected = selector.update([vehicle(id, x: 0.5, time: t, rear: true)], at: t)
        }
        XCTAssertEqual(try XCTUnwrap(selected).id, id)
        let other = UUID()
        for i in 4..<10 {
            let t = 10 + Double(i) * 0.1
            let beside = vehicle(id, x: 0.55, time: t, rear: true)
            let constraint = observation(t, shift: -0.25, slots: [2])
            selected = selector.update([beside, vehicle(other, x: 0.32, time: t)], at: t, boundaries: constraint)
            XCTAssertFalse(selected?.isPathSupported == true)
            XCTAssertNotEqual(selected?.id, other)
        }
        XCTAssertNil(selected)
    }

    func testBoundaryBufferNegativeAndNewTripCannotResurrectPriorConstraint() {
        var buffer = LeadLaneEvidenceBuffer()
        buffer.record(nil, boundaries: observation(10, slots: [2]), at: 10)
        buffer.record(nil, at: 10.2)
        XCTAssertNotNil(buffer.latestBoundaries(at: 10.1))
        XCTAssertNil(buffer.latestBoundaries(at: 10.2))
        buffer.record(nil, boundaries: observation(10.3, slots: [2]), at: 10.3)
        buffer.reset()
        XCTAssertNil(buffer.latestBoundaries(at: 10.3))
    }

    func testPreviouslyPreferredAdjacentCarCannotMonopolizeTheNextRearCheck() {
        let besideID = UUID(), leadID = UUID()
        let lead = vehicle(leadID, x: 0.50, time: 10)
        let beside = vehicle(besideID, x: 0.88, time: 10, rear: true)
        var scheduler = VehicleViewpointScheduler()
        XCTAssertEqual(scheduler.choose([beside, lead], at: 10, preferredID: besideID, lane: nil,
                                        boundaries: observation(10, slots: [2]))?.trackID, leadID)
    }
}
