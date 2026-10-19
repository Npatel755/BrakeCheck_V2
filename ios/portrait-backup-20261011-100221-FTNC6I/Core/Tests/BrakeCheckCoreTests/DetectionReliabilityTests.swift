import Foundation
import XCTest
@testable import BrakeCheckCore

final class DetectionReliabilityTests: XCTestCase {
    private let box = CGRect(x: 0.35, y: 0.25, width: 0.30, height: 0.22)
    private func vehicle(_ label: String = "car", box: CGRect? = nil, confidence: Double = 0.9,
                         id: UUID? = nil, view: VehicleViewpoint? = nil) -> LeadVehicle {
        LeadVehicle(box: box ?? self.box, label: label, confidence: confidence, viewpoint: view, trackID: id)
    }
    private func rear(_ time: Double) -> VehicleViewpoint {
        VehicleViewpoint(scores: [0.02, 0.02, 0.02, 0.30, 0.30, 0.30, 0.02, 0.02], capturedAt: time)!
    }

    func testDuplicateNightCarBusBoxesProduceOneIdentity() throws {
        let filtered = VehicleDetectionFilter.filter([
            vehicle(), vehicle("bus", box: box.offsetBy(dx: 0.005, dy: 0), confidence: 0.8)
        ], imageAspect: 16.0 / 9.0)
        XCTAssertEqual(filtered.count, 1)
        var tracks = VehicleHeadingTracks()
        let id = try XCTUnwrap(tracks.update(filtered, at: 0).first?.trackID)
        for frame in 1...8 {
            let next = VehicleDetectionFilter.filter([
                vehicle("bus", confidence: 0.95), vehicle(confidence: 0.85)
            ], imageAspect: 16.0 / 9.0)
            let linked = tracks.update(next, at: Double(frame) * 0.1, allowNightClassAliases: true)
            XCTAssertEqual(linked.first?.trackID, id)
            XCTAssertEqual(linked.first?.assumedWidthMeters, 1.8)
        }
    }

    func testOverlappingSeparateCarsAreNotMerged() {
        let other = box.offsetBy(dx: 0.09, dy: 0)
        XCTAssertEqual(VehicleDetectionFilter.filter([vehicle(), vehicle(box: other)],
            imageAspect: 16.0 / 9.0).count, 2)
    }

    func testMotorcycleIsNotSuppressedByOverlappingCar() {
        XCTAssertEqual(VehicleDetectionFilter.filter([vehicle(), vehicle("motorbike", confidence: 0.8)],
            imageAspect: 1).count, 2)
    }

    func testTinyEdgeRoundingKeepsClippedIdentityButCannotSupplyRange() throws {
        let edge = CGRect(x: -0.00001, y: 0.25, width: 0.30, height: 0.22)
        let normalized = try XCTUnwrap(VehicleDetectionFilter.normalizedBox(edge))
        XCTAssertEqual(normalized.minX, 0)
        XCTAssertTrue(LeadVehicle.validBox(normalized))
        XCTAssertLessThanOrEqual(normalized.minX, 0.005) // Range's full-silhouette gate rejects it.
    }

    func testCorruptBoxesConfidenceAndImplausibleShapesAreDiscarded() {
        let bad = [vehicle(box: CGRect(x: -0.5, y: 0.2, width: 0.6, height: 0.3)),
            vehicle(confidence: .nan), vehicle(confidence: 1.2),
            vehicle(box: CGRect(x: 0.1, y: 0.2, width: 0.8, height: 0.08)),
            vehicle("person")]
        XCTAssertTrue(VehicleDetectionFilter.filter(bad, imageAspect: 16.0 / 9.0).isEmpty)
        XCTAssertTrue(VehicleDetectionFilter.filter([vehicle()], imageAspect: .nan).isEmpty)
    }

    func testBothCarsCompleteRearConfirmationWhileLeadStaysFresh() throws {
        let lead = UUID(), other = UUID()
        var histories = [lead: VehicleViewpointHistory(), other: VehicleViewpointHistory()]
        var scheduler = VehicleViewpointScheduler()
        var selected: [UUID] = []
        for frame in 0...12 {
            let time = Double(frame) * 0.25
            let vehicles = [vehicle(id: lead, view: histories[lead]?.latest(at: time)),
                vehicle(box: box.offsetBy(dx: 0.08, dy: -0.05), id: other,
                        view: histories[other]?.latest(at: time))]
            if let chosen = scheduler.choose(vehicles, at: time, preferredID: lead, lane: nil),
               let id = chosen.trackID {
                selected.append(id); histories[id]?.append(rear(time))
            }
            if time >= 0.5 { XCTAssertTrue(histories[lead]?.latest(at: time)?.hasConfirmedRearView(at: time) == true) }
        }
        XCTAssertTrue(selected.contains(other))
        XCTAssertTrue(histories[other]?.latest(at: 3)?.hasConfirmedRearView(at: 3) == true)
    }

    func testAThirdCutInAlsoGetsConfirmedWithoutStarvingTheLead() {
        let ids = [UUID(), UUID(), UUID()]
        var histories = Dictionary(uniqueKeysWithValues: ids.map { ($0, VehicleViewpointHistory()) })
        var scheduler = VehicleViewpointScheduler()
        var confirmed: Set<UUID> = []
        for frame in 0...16 {
            let time = Double(frame) * 0.25
            let vehicles = ids.map { vehicle(id: $0, view: histories[$0]?.latest(at: time)) }
            if let chosen = scheduler.choose(vehicles, at: time, preferredID: ids[0], lane: nil),
               let id = chosen.trackID { histories[id]?.append(rear(time)) }
            for id in ids where histories[id]?.latest(at: time)?.hasConfirmedRearView(at: time) == true {
                confirmed.insert(id)
            }
            if time >= 0.5 { XCTAssertNotNil(histories[ids[0]]?.latest(at: time)) }
        }
        XCTAssertEqual(confirmed, Set(ids))
    }

    func testOneConfirmedLeadUsesOnlyTheNeededRefreshSlots() throws {
        let id = UUID()
        var history = VehicleViewpointHistory()
        history.append(rear(0)); history.append(rear(0.2))
        var scheduler = VehicleViewpointScheduler()
        XCTAssertNotNil(scheduler.choose([vehicle(id: id, view: history.latest(at: 0.2))],
            at: 0.2, preferredID: id, lane: nil))
        XCTAssertNil(scheduler.choose([vehicle(id: id, view: history.latest(at: 0.45))],
            at: 0.45, preferredID: id, lane: nil))
        XCTAssertNotNil(scheduler.choose([vehicle(id: id, view: history.latest(at: 0.7))],
            at: 0.7, preferredID: id, lane: nil))
    }

    func testSameFrameCannotBecomeTwoClassifierCapturesAndResetStartsFresh() {
        var scheduler = VehicleViewpointScheduler()
        let car = vehicle(id: UUID())
        XCTAssertNotNil(scheduler.choose([car], at: 1, preferredID: nil, lane: nil))
        XCTAssertNil(scheduler.choose([car], at: 1, preferredID: nil, lane: nil))
        scheduler.reset()
        XCTAssertNotNil(scheduler.choose([car], at: 1.1, preferredID: nil, lane: nil))
    }

    func testMeasurementExpiryDoesNotExpireIdentityAndNeitherAcceptsFutureTime() {
        XCTAssertTrue(TrackingFreshness.measurementIsFresh(capturedAt: 10, now: 10.6))
        XCTAssertFalse(TrackingFreshness.measurementIsFresh(capturedAt: 10, now: 10.61))
        XCTAssertTrue(TrackingFreshness.identityIsFresh(capturedAt: 10, now: 11.5))
        XCTAssertFalse(TrackingFreshness.identityIsFresh(capturedAt: 10, now: 13.1))
        XCTAssertFalse(TrackingFreshness.identityIsFresh(capturedAt: 10, now: 9.9))
        XCTAssertFalse(TrackingFreshness.measurementIsFresh(capturedAt: .nan, now: 10))
    }
}
