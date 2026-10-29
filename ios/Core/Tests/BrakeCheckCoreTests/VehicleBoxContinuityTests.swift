import Foundation
import XCTest
@testable import BrakeCheckCore

final class VehicleBoxContinuityTests: XCTestCase {
    private let box = CGRect(x: 0.35, y: 0.25, width: 0.30, height: 0.22)
    private func car(_ box: CGRect? = nil, id: UUID? = nil, confidence: Double = 0.9) -> LeadVehicle {
        LeadVehicle(box: box ?? self.box, label: "car", confidence: confidence, trackID: id)
    }

    func testDisplayReducesJitterWithoutChangingDetectorMeasurements() throws {
        let id = UUID(); var display = VehicleDetectionPresentation()
        _ = display.update([car(id: id)], at: 0)
        let shifted = box.offsetBy(dx: 0.012, dy: 0.004)
        let measurement = car(shifted, id: id)
        let entry = try XCTUnwrap(display.update([measurement], at: 0.1).first)
        XCTAssertGreaterThan(entry.vehicle.box.minX, box.minX)
        XCTAssertLessThan(entry.vehicle.box.minX, shifted.minX)
        XCTAssertEqual(measurement.box, shifted)
        XCTAssertEqual(entry.vehicle.trackID, id)
        XCTAssertEqual(entry.capturedAt, 0.1)
        XCTAssertTrue(entry.isFreshDetection)
        XCTAssertEqual(display.box(for: id), entry.vehicle.box)
    }

    func testDisplayFollowsMovingCarWithoutBuildingLag() throws {
        let id = UUID(); var display = VehicleDetectionPresentation()
        for frame in 0...15 {
            let detected = box.offsetBy(dx: Double(frame) * 0.008, dy: 0)
            let entry = try XCTUnwrap(display.update([car(detected, id: id)], at: Double(frame) * 0.1).first)
            XCTAssertLessThan(abs(entry.vehicle.box.midX - detected.midX), 0.009)
        }
    }

    func testChangedCropOrReturnAfterGapUsesCurrentGeometryImmediately() throws {
        let id = UUID(); var display = VehicleDetectionPresentation()
        _ = display.update([car(id: id)], at: 0)
        let changed = CGRect(x: 0.4, y: 0.26, width: 0.18, height: 0.22)
        XCTAssertEqual(display.update([car(changed, id: id)], at: 0.1).first?.vehicle.box, changed)
        _ = display.update([], at: 0.2)
        let returned = changed.offsetBy(dx: 0.015, dy: 0)
        XCTAssertEqual(display.update([car(returned, id: id)], at: 0.45).first?.vehicle.box, returned)
    }

    func testReplacedDisplayIdentityCannotReappearDuringNextMiss() throws {
        let old = UUID(), replacement = UUID(); var display = VehicleDetectionPresentation()
        _ = display.update([car(id: old)], at: 0)
        _ = display.update([car(id: replacement)], at: 0.1)
        let held = display.update([], at: 0.2)
        XCTAssertEqual(held.count, 1)
        XCTAssertEqual(held.first?.vehicle.trackID, replacement)
        XCTAssertNil(display.box(for: old))
    }

    func testVisualGapFollowsOnlySupportedPreferredTrackWithoutRenewingCapture() throws {
        let id = UUID(), otherID = UUID(); var display = VehicleDetectionPresentation()
        let other = box.offsetBy(dx: 0.3, dy: 0)
        _ = display.update([car(id: id), car(other, id: otherID)], at: 0)
        let moved = box.offsetBy(dx: 0.03, dy: -0.005)
        let held = display.update([], at: 0.2, preferredID: id, trackedBox: moved)
        let lead = try XCTUnwrap(held.first { $0.vehicle.trackID == id })
        XCTAssertEqual(lead.vehicle.box, moved)
        XCTAssertEqual(lead.capturedAt, 0)
        XCTAssertFalse(lead.isFreshDetection)
        XCTAssertEqual(held.first { $0.vehicle.trackID == otherID }?.vehicle.box, other)
        XCTAssertTrue(display.update([], at: 0.46, preferredID: id, trackedBox: moved).isEmpty)
        XCTAssertNil(display.box(for: id))
    }

    func testVisualTrackerCannotMoveDisplayOntoAnotherCar() throws {
        let id = UUID(); var display = VehicleDetectionPresentation()
        _ = display.update([car(id: id)], at: 0)
        let drifted = box.offsetBy(dx: 0.25, dy: 0)
        XCTAssertEqual(display.update([], at: 0.2, preferredID: id, trackedBox: drifted).first?.vehicle.box, box)
        XCTAssertFalse(VehicleTrackSupport.supportsVisualBox(CGRect(x: 0.42, y: 0.25, width: 0.12, height: 0.22), relativeTo: box))
    }

    func testDuplicateDisplayIDsProduceOneStrongestBox() throws {
        let id = UUID(); var display = VehicleDetectionPresentation()
        let result = display.update([car(id: id, confidence: 0.4), car(id: id, confidence: 0.9)], at: 0)
        XCTAssertEqual(result.count, 1)
        XCTAssertEqual(result.first?.vehicle.confidence, 0.9)
    }

    func testMovingSmallCarKeepsItsIdentityAcrossBriefDetectorMiss() throws {
        let initial = CGRect(x: 0.4, y: 0.35, width: 0.04, height: 0.05)
        var tracks = VehicleHeadingTracks()
        let id = try XCTUnwrap(tracks.update([car(initial)], at: 0).first?.trackID)
        for frame in 1...4 {
            let found = tracks.update([car(initial.offsetBy(dx: Double(frame) * 0.005, dy: 0))], at: Double(frame) * 0.1)
            XCTAssertEqual(found.first?.trackID, id)
        }
        XCTAssertTrue(tracks.update([], at: 0.6).isEmpty)
        let actual = initial.offsetBy(dx: 0.045, dy: 0)
        let returned = try XCTUnwrap(tracks.update([car(actual)], at: 0.9).first)
        XCTAssertEqual(returned.trackID, id)
        XCTAssertEqual(returned.box, actual)
    }

    func testSupersededCropIdentityIsRetiredInsteadOfCompetingAgain() throws {
        var tracks = VehicleHeadingTracks()
        let old = try XCTUnwrap(tracks.update([car()], at: 0).first?.trackID)
        let changed = CGRect(x: 0.41, y: 0.25, width: 0.18, height: 0.22)
        let replacement = try XCTUnwrap(tracks.update([car(changed)], at: 0.1).first?.trackID)
        XCTAssertNotEqual(replacement, old)
        let rear = try XCTUnwrap(VehicleViewpoint(scores: [0.02, 0.02, 0.02, 0.30, 0.30, 0.30, 0.02, 0.02], capturedAt: 0))
        XCTAssertFalse(tracks.record(rear, for: old, at: 0.1))
        let returned = try XCTUnwrap(tracks.update([car()], at: 0.2).first?.trackID)
        XCTAssertNotEqual(returned, old)
        XCTAssertNil(tracks.viewpoint(for: old, at: 0.2))
    }
}
