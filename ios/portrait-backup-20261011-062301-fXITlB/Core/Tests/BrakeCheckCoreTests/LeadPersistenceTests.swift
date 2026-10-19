import Foundation
import XCTest
@testable import BrakeCheckCore

final class LeadPersistenceTests: XCTestCase {
    private let box = CGRect(x: 0.35, y: 0.25, width: 0.30, height: 0.22)
    private func rear(_ time: Double) -> VehicleViewpoint {
        VehicleViewpoint(scores: [0.02, 0.02, 0.02, 0.30, 0.30, 0.30, 0.02, 0.02], capturedAt: time)!
    }
    private func front(_ time: Double) -> VehicleViewpoint {
        VehicleViewpoint(scores: [0.30, 0.30, 0.02, 0.02, 0.02, 0.02, 0.02, 0.30], capturedAt: time)!
    }
    private func uncertain(_ time: Double) -> VehicleViewpoint {
        VehicleViewpoint(scores: Array(repeating: 0.125, count: 8), capturedAt: time)!
    }
    private func side(_ time: Double) -> VehicleViewpoint {
        VehicleViewpoint(scores: [0.01, 0.01, 0.47, 0.01, 0.01, 0.01, 0.47, 0.01], capturedAt: time)!
    }
    private func lane(_ time: Double, left: Double = 0.30, right: Double = 0.70) -> LanePath {
        LanePath(bands: (0..<13).map { LaneBand(y: 0.1 + Double($0) * 0.04, left: left, right: right) },
                 confidence: 0.9, capturedAt: time)
    }
    private func vehicle(_ id: UUID, view: VehicleViewpoint?, box: CGRect? = nil) -> LeadVehicle {
        LeadVehicle(box: box ?? self.box, label: "car", confidence: 0.9, viewpoint: view, trackID: id)
    }
    private func confirm(_ selector: inout LeadSelector, id: UUID) throws -> VehicleViewpoint {
        var history = VehicleViewpointHistory()
        history.append(rear(0)); history.append(rear(0.2))
        let view = try XCTUnwrap(history.latest(at: 0.2))
        var selected: LeadSelection?
        for time in [0.2, 0.3, 0.4] {
            selected = selector.update([vehicle(id, view: view)], at: time, lane: lane(time))
        }
        XCTAssertEqual(try XCTUnwrap(selected).id, id)
        return view
    }

    func testSameVehicleKeepsLeadDuringUncertainAndMissingRearChecks() throws {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID()
        _ = try confirm(&selector, id: id)
        for frame in 0..<150 {
            let time = 0.5 + Double(frame) * 0.1
            let view: VehicleViewpoint? = frame.isMultiple(of: 2) ? uncertain(time) : nil
            let lead = try XCTUnwrap(selector.update([vehicle(id, view: view)], at: time, lane: lane(time)))
            XCTAssertEqual(lead.id, id)
            XCTAssertTrue(lead.isFreshDetection)
            XCTAssertTrue(lead.isPathSupported)
            XCTAssertTrue(lead.hasConfirmedRearIdentity)
            XCTAssertTrue(lead.isRearConfirmationRetained)
        }
    }

    func testAnUnconfirmedVehicleCannotAcquireWithOneRearCapture() {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID(), view = rear(0)
        for time in [0.0, 0.1, 0.2, 0.3, 0.4] {
            XCTAssertNil(selector.update([vehicle(id, view: view)], at: time, lane: lane(time)))
        }
    }

    func testFreshFrontEvidenceReleasesRememberedLeadImmediately() throws {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID()
        _ = try confirm(&selector, id: id)
        XCTAssertNil(selector.update([vehicle(id, view: front(0.5))], at: 0.5, lane: lane(0.5)))
    }

    func testOneSideCaptureDoesNotClearButTwoIndependentSideViewsDo() throws {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID()
        _ = try confirm(&selector, id: id)
        let first = side(0.5)
        XCTAssertNotNil(selector.update([vehicle(id, view: first)], at: 0.5, lane: lane(0.5)))
        XCTAssertNotNil(selector.update([vehicle(id, view: first)], at: 0.6, lane: lane(0.6)))
        XCTAssertNil(selector.update([vehicle(id, view: side(0.7))], at: 0.7, lane: lane(0.7)))
    }

    func testConfirmationExpiresWithoutRearOrPathEvidence() throws {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID()
        _ = try confirm(&selector, id: id)
        var result: LeadSelection?
        for frame in 0...30 {
            let time = 0.5 + Double(frame) * 0.2
            result = selector.update([vehicle(id, view: nil)], at: time)
        }
        XCTAssertNil(result)
    }

    func testRepeatedOldRearCaptureDoesNotRefreshTheConfirmationDeadline() throws {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID(), original = try confirm(&selector, id: id)
        var result: LeadSelection?
        for frame in 0...30 {
            let time = 0.5 + Double(frame) * 0.2
            result = selector.update([vehicle(id, view: original)], at: time)
        }
        XCTAssertNil(result)
    }

    func testFreshSingleRearCheckRefreshesAnAlreadyConfirmedIdentity() throws {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID()
        _ = try confirm(&selector, id: id)
        _ = selector.update([vehicle(id, view: uncertain(0.5))], at: 0.5)
        _ = selector.update([vehicle(id, view: rear(0.6))], at: 0.6)
        var result: LeadSelection?
        for frame in 0...28 {
            let time = 0.7 + Double(frame) * 0.2
            result = selector.update([vehicle(id, view: nil)], at: time)
        }
        XCTAssertEqual(try XCTUnwrap(result).id, id) // 6.3 s: older 0.2 s evidence would have expired.
    }

    func testOneBadLaneFramePausesMetricsAndKeepsTheSameLeadIdentity() throws {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID()
        _ = try confirm(&selector, id: id)
        let excluded = try XCTUnwrap(selector.update([vehicle(id, view: nil)], at: 0.5,
            lane: lane(0.5, left: 0.75, right: 0.95)))
        XCTAssertEqual(excluded.id, id)
        XCTAssertFalse(excluded.isPathSupported)
        let recovered = try XCTUnwrap(selector.update([vehicle(id, view: nil)], at: 0.6, lane: lane(0.6)))
        XCTAssertEqual(recovered.id, id)
        XCTAssertTrue(recovered.isPathSupported)
    }

    func testSustainedMeasuredLaneExitReleasesTheLead() throws {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID()
        _ = try confirm(&selector, id: id)
        var result: LeadSelection?
        for time in [0.5, 0.6, 0.7, 0.8] {
            result = selector.update([vehicle(id, view: nil)], at: time,
                lane: lane(time, left: 0.75, right: 0.95))
            if time < 0.8 { XCTAssertFalse(try XCTUnwrap(result).isPathSupported) }
        }
        XCTAssertNil(result)
    }

    func testLongHeldLeadStillReleasesAfterMeasuredLaneExit() throws {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID()
        _ = try confirm(&selector, id: id)
        for frame in 0..<80 {
            let time = 0.5 + Double(frame) * 0.1
            XCTAssertEqual(selector.update([vehicle(id, view: uncertain(time))], at: time,
                lane: lane(time))?.id, id)
        }
        var result: LeadSelection?
        for time in [8.5, 8.6, 8.7, 8.8] {
            result = selector.update([vehicle(id, view: uncertain(time))], at: time,
                lane: lane(time, left: 0.75, right: 0.95))
            if time < 8.8 { XCTAssertFalse(try XCTUnwrap(result).isPathSupported) }
        }
        XCTAssertNil(result)
    }

    func testStaleLaneCannotKeepAnOldRearConfirmationAlive() throws {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID()
        _ = try confirm(&selector, id: id)
        for frame in 0..<80 {
            let time = 0.5 + Double(frame) * 0.1
            _ = selector.update([vehicle(id, view: uncertain(time))], at: time, lane: lane(time))
        }
        let stale = lane(8.4)
        var result: LeadSelection?
        for frame in 0..<25 {
            let time = 8.5 + Double(frame) * 0.1
            result = selector.update([vehicle(id, view: uncertain(time))], at: time, lane: stale)
        }
        XCTAssertNil(result)
    }

    func testAnotherVehicleIdCannotInheritTheRememberedLeadConfirmation() throws {
        var selector = LeadSelector(requiresRearView: true)
        let original = UUID(), other = UUID()
        _ = try confirm(&selector, id: original)
        var result: LeadSelection?
        for time in [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4] {
            result = selector.update([vehicle(other, view: nil)], at: time, lane: lane(time))
            if let result {
                XCTAssertEqual(result.id, original)
                XCTAssertFalse(result.isFreshDetection)
            }
        }
        XCTAssertNil(result)
    }

    func testProlongedDetectionLossReleasesLeadDespiteAVisibleVisualTrack() throws {
        var selector = LeadSelector(requiresRearView: true)
        let id = UUID()
        _ = try confirm(&selector, id: id)
        var result: LeadSelection?
        for time in [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4] {
            result = selector.update([], at: time, lane: lane(time), trackedBox: box)
        }
        XCTAssertNil(result)
    }

    func testBriefDetectionGapKeepsIdAndPausesMeasurementsUntilFreshDetection() throws {
        var tracks = VehicleHeadingTracks(), selector = LeadSelector(requiresRearView: true)
        let raw = LeadVehicle(box: box, label: "car", confidence: 0.9)
        let initial = try XCTUnwrap(tracks.update([raw], at: 0).first)
        let id = try XCTUnwrap(initial.trackID)
        XCTAssertTrue(tracks.record(rear(0), for: id, at: 0))
        _ = tracks.update([raw], at: 0.2)
        XCTAssertTrue(tracks.record(rear(0.2), for: id, at: 0.2))
        for time in [0.2, 0.3, 0.4] {
            let values = time == 0.2 ? [vehicle(id, view: tracks.viewpoint(for: id, at: time))]
                : tracks.update([raw], at: time)
            _ = selector.update(values, at: time, lane: lane(time))
        }
        let gap = try XCTUnwrap(selector.update(tracks.update([], at: 0.7), at: 0.7,
            lane: lane(0.7), trackedBox: box))
        XCTAssertEqual(gap.id, id); XCTAssertFalse(gap.isFreshDetection)
        let recovered = tracks.update([raw], at: 1.1)
        XCTAssertEqual(recovered.first?.trackID, id)
        let lead = try XCTUnwrap(selector.update(recovered, at: 1.1, lane: lane(1.1)))
        XCTAssertEqual(lead.id, id); XCTAssertTrue(lead.isFreshDetection)
    }

    func testChangedSilhouetteStartsNewIdentityAndRejectsOldCropResult() throws {
        var tracks = VehicleHeadingTracks()
        let raw = LeadVehicle(box: box, label: "car", confidence: 0.9)
        let first = try XCTUnwrap(tracks.update([raw], at: 0).first)
        let id = try XCTUnwrap(first.trackID)
        XCTAssertTrue(tracks.record(rear(0), for: id, at: 0))
        let changed = LeadVehicle(box: CGRect(x: box.minX, y: box.minY,
            width: box.width * 1.5, height: box.height), label: "car", confidence: 0.9)
        let next = try XCTUnwrap(tracks.update([changed], at: 0.2).first)
        XCTAssertNotEqual(next.trackID, id)
        XCTAssertNil(next.viewpoint)
        XCTAssertFalse(tracks.record(rear(0.1), for: id, at: 0.2))
    }
}
