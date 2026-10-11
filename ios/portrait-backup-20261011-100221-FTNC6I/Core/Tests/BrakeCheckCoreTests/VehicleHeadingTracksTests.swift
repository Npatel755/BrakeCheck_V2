import XCTest
@testable import BrakeCheckCore

final class VehicleHeadingTracksTests: XCTestCase {
    func testNightCarBusAliasRetainsTheOriginalIDAndRangeScale() throws {
        var tracks = VehicleHeadingTracks()
        let first = try XCTUnwrap(tracks.update([vehicle("car")], at: 0).first)
        let next = try XCTUnwrap(tracks.update([vehicle("bus")], at: 0.2, allowNightClassAliases: true).first)
        XCTAssertEqual(next.trackID, first.trackID)
        XCTAssertEqual(next.label, "car")
        XCTAssertEqual(next.assumedWidthMeters, first.assumedWidthMeters)
    }

    func testNewNightBusCannotAcquireRearEvidenceByChangingLabel() throws {
        var tracks = VehicleHeadingTracks()
        let first = try XCTUnwrap(tracks.update([vehicle("bus")], at: 0).first)
        let next = try XCTUnwrap(tracks.update([vehicle("car")], at: 0.2, allowNightClassAliases: true).first)
        XCTAssertEqual(next.trackID, first.trackID)
        XCTAssertEqual(next.label, "bus")
        XCTAssertNil(next.viewpoint)
    }

    func testCropCapturedBeforeAnAmbiguousCrossingCannotRestoreEvidence() throws {
        var tracks = VehicleHeadingTracks()
        let left = CGRect(x: 0.30, y: 0.25, width: 0.30, height: 0.22)
        let right = left.offsetBy(dx: 0.10, dy: 0)
        let initial = tracks.update([vehicle("car", box: left), vehicle("car", box: right)], at: 0)
        let id = try XCTUnwrap(initial.first?.trackID)
        _ = tracks.update([vehicle("car", box: left.offsetBy(dx: 0.05, dy: 0))], at: 0.2)
        let resolved = tracks.update([vehicle("car", box: left)], at: 0.3)
        XCTAssertEqual(resolved.first?.trackID, id)
        XCTAssertFalse(tracks.record(rear(0.1), for: id, at: 0.3))
        XCTAssertNil(tracks.viewpoint(for: id, at: 0.3))
    }

    private let box = CGRect(x: 0.35, y: 0.25, width: 0.30, height: 0.22)
    private func vehicle(_ label: String, box: CGRect? = nil) -> LeadVehicle {
        LeadVehicle(box: box ?? self.box, label: label, confidence: 0.9)
    }
    private func rear(_ time: Double) -> VehicleViewpoint {
        VehicleViewpoint(scores: [0.02, 0.02, 0.02, 0.30, 0.30, 0.30, 0.02, 0.02], capturedAt: time)!
    }
    private func front(_ time: Double) -> VehicleViewpoint {
        VehicleViewpoint(scores: [0.30, 0.30, 0.02, 0.02, 0.02, 0.02, 0.02, 0.30], capturedAt: time)!
    }

    func testCarTruckSwitchKeepsIdentityEvidenceAndRangeScale() throws {
        var tracks = VehicleHeadingTracks()
        let first = try XCTUnwrap(tracks.update([vehicle("car")], at: 0).first)
        let id = try XCTUnwrap(first.trackID)
        XCTAssertTrue(tracks.record(rear(0), for: id, at: 0))
        let next = try XCTUnwrap(tracks.update([vehicle("truck")], at: 0.3).first)
        XCTAssertEqual(next.trackID, id)
        XCTAssertEqual(next.assumedWidthMeters, first.assumedWidthMeters)
        XCTAssertTrue(tracks.record(rear(0.3), for: id, at: 0.3))
        XCTAssertTrue(try XCTUnwrap(tracks.viewpoint(for: id, at: 0.3)).hasConfirmedRearView(at: 0.3))
    }

    func testTruckCarSwitchKeepsTheOriginalRangeScale() throws {
        var tracks = VehicleHeadingTracks()
        let first = try XCTUnwrap(tracks.update([vehicle("truck")], at: 0).first)
        let next = try XCTUnwrap(tracks.update([vehicle("car")], at: 0.2).first)
        XCTAssertEqual(next.trackID, first.trackID)
        XCTAssertEqual(next.assumedWidthMeters, first.assumedWidthMeters)
    }

    func testARepeatedClassifierCaptureCannotConfirmRear() throws {
        var tracks = VehicleHeadingTracks()
        let first = try XCTUnwrap(tracks.update([vehicle("car")], at: 0).first)
        let id = try XCTUnwrap(first.trackID)
        XCTAssertTrue(tracks.record(rear(0), for: id, at: 0))
        _ = tracks.update([vehicle("truck")], at: 0.3)
        XCTAssertFalse(tracks.record(rear(0), for: id, at: 0.3))
        XCTAssertFalse(try XCTUnwrap(tracks.viewpoint(for: id, at: 0.3)).hasConfirmedRearView(at: 0.3))
    }

    func testFrontFacingEvidenceClearsAnExistingLead() throws {
        var history = VehicleViewpointHistory()
        history.append(rear(0)); history.append(rear(0.2))
        let id = UUID()
        var selector = LeadSelector(requiresRearView: true)
        var selection: LeadSelection?
        for time in [0.2, 0.3, 0.4] {
            selection = selector.update([LeadVehicle(box: box, label: "truck", confidence: 0.9,
                viewpoint: history.latest(at: time), trackID: id)], at: time)
        }
        XCTAssertNotNil(selection)
        history.append(front(0.5))
        XCTAssertNil(selector.update([LeadVehicle(box: box, label: "truck", confidence: 0.9,
            viewpoint: history.latest(at: 0.5), trackID: id)], at: 0.5, trackedBox: box))
        XCTAssertFalse(try XCTUnwrap(history.latest(at: 0.5)).hasConfirmedRearView(at: 0.5))
    }

    func testAnotherTruckCannotInheritTheCarsViewpoint() throws {
        var tracks = VehicleHeadingTracks()
        let first = try XCTUnwrap(tracks.update([vehicle("car")], at: 0).first)
        XCTAssertTrue(tracks.record(rear(0), for: try XCTUnwrap(first.trackID), at: 0))
        let other = CGRect(x: 0.76, y: 0.25, width: 0.15, height: 0.16)
        let result = tracks.update([vehicle("car"), vehicle("truck", box: other)], at: 0.2)
        XCTAssertEqual(result.count, 2)
        XCTAssertNotEqual(result[0].trackID, result[1].trackID)
        XCTAssertNil(result[1].viewpoint)
    }

    func testAmbiguousCrossingAndUnsupportedClassLoseEvidence() throws {
        var tracks = VehicleHeadingTracks()
        let initial = try XCTUnwrap(tracks.update([vehicle("car")], at: 0).first)
        XCTAssertTrue(tracks.record(rear(0), for: try XCTUnwrap(initial.trackID), at: 0))
        let crossing = tracks.update([vehicle("car", box: box.offsetBy(dx: -0.01, dy: 0)),
            vehicle("truck", box: box.offsetBy(dx: 0.01, dy: 0))], at: 0.2)
        XCTAssertEqual(crossing.count, 2)
        XCTAssertTrue(crossing.allSatisfy { $0.viewpoint == nil })
        tracks.reset()
        let car = try XCTUnwrap(tracks.update([vehicle("car")], at: 1).first)
        XCTAssertTrue(tracks.record(rear(1), for: try XCTUnwrap(car.trackID), at: 1))
        let bus = try XCTUnwrap(tracks.update([vehicle("bus")], at: 1.2).first)
        XCTAssertNotEqual(car.trackID, bus.trackID)
        XCTAssertNil(bus.viewpoint)
    }

    func testRearConfirmationDoesNotAuthorizeSpeedWithoutPathSupport() throws {
        var history = VehicleViewpointHistory()
        history.append(rear(0)); history.append(rear(0.2))
        let id = UUID()
        var selector = LeadSelector(requiresRearView: true)
        var result: LeadSelection?
        for time in [0.2, 0.3, 0.4] {
            result = selector.update([LeadVehicle(box: box, label: "car", confidence: 0.9,
                viewpoint: history.latest(at: time), trackID: id)], at: time)
        }
        let candidate = try XCTUnwrap(result)
        XCTAssertEqual(candidate.id, id)
        XCTAssertTrue(candidate.isFreshDetection)
        XCTAssertFalse(candidate.isPathSupported)
    }

    func testCoastingCandidateHasNoFreshRangeMeasurement() throws {
        var history = VehicleViewpointHistory()
        history.append(rear(0)); history.append(rear(0.2))
        let id = UUID()
        var selector = LeadSelector(requiresRearView: true)
        for time in [0.2, 0.3, 0.4] {
            _ = selector.update([LeadVehicle(box: box, label: "car", confidence: 0.9,
                viewpoint: history.latest(at: time), trackID: id)], at: time)
        }
        let coast = try XCTUnwrap(selector.update([], at: 0.5, trackedBox: box))
        XCTAssertEqual(coast.id, id)
        XCTAssertFalse(coast.isFreshDetection)
        XCTAssertFalse(coast.isPathSupported)
    }
}
