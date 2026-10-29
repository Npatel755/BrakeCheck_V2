import Foundation
import XCTest
@testable import BrakeCheckCore

final class DetectionContinuityTests: XCTestCase {
    private let box = CGRect(x: 0.35, y: 0.25, width: 0.30, height: 0.22)
    private func car(_ box: CGRect? = nil, confidence: Double = 0.9, id: UUID? = nil) -> LeadVehicle {
        LeadVehicle(box: box ?? self.box, label: "car", confidence: confidence, trackID: id)
    }
    private func rear(_ time: Double) -> VehicleViewpoint {
        VehicleViewpoint(scores: [0.02, 0.02, 0.02, 0.30, 0.30, 0.30, 0.02, 0.02], capturedAt: time)!
    }
    private func lane(_ time: Double) -> LanePath {
        LanePath(bands: (0..<13).map { LaneBand(y: 0.1 + Double($0) * 0.04, left: 0.30, right: 0.70) },
                 confidence: 0.9, capturedAt: time)
    }

    func testRearCropSurvivesAMissedNextDetectorFrame() throws {
        var tracks = VehicleHeadingTracks()
        let id = try XCTUnwrap(tracks.update([car()], at: 0).first?.trackID)
        XCTAssertTrue(tracks.record(rear(0), for: id, at: 0))
        _ = tracks.update([car()], at: 0.25)
        XCTAssertTrue(tracks.update([], at: 0.5).isEmpty)
        XCTAssertTrue(tracks.record(rear(0.25), for: id, at: 0.5))
        var selector = LeadSelector(requiresRearView: true)
        XCTAssertNil(selector.update([], at: 0.5, lane: lane(0.5)))
        var lead: LeadSelection?
        for time in [0.6, 0.7, 0.8] {
            let found = tracks.update([car()], at: time)
            XCTAssertEqual(found.first?.trackID, id)
            XCTAssertTrue(found.first?.viewpoint?.hasConfirmedRearView(at: time) == true)
            lead = selector.update(found, at: time, lane: lane(time))
        }
        XCTAssertEqual(try XCTUnwrap(lead).id, id)
        XCTAssertTrue(lead?.isFreshDetection == true)
    }
    func testRearCropCannotRenewEvidenceAcrossLongMiss() throws {
        var tracks = VehicleHeadingTracks()
        let id = try XCTUnwrap(tracks.update([car()], at: 0).first?.trackID)
        _ = tracks.update([], at: 0.46)
        XCTAssertFalse(tracks.record(rear(0), for: id, at: 0.46))
        XCTAssertNil(tracks.viewpoint(for: id, at: 0.46))
    }
    func testCropNotCapturedFromTheRetainedDetectionIsRejected() throws {
        var tracks = VehicleHeadingTracks()
        let id = try XCTUnwrap(tracks.update([car()], at: 0).first?.trackID)
        _ = tracks.update([], at: 0.2)
        XCTAssertFalse(tracks.record(rear(0.1), for: id, at: 0.2))
        XCTAssertFalse(tracks.record(rear(0), for: UUID(), at: 0.2))
    }
    func testCancelledClassifierSlotCanRetryOnANewerFrame() {
        let id = UUID(); var scheduler = VehicleViewpointScheduler()
        XCTAssertNotNil(scheduler.choose([car(id: id)], at: 0, preferredID: nil, lane: nil))
        scheduler.cancel(for: id, capturedAt: 0)
        XCTAssertNotNil(scheduler.choose([car(id: id)], at: 0.1, preferredID: nil, lane: nil))
    }
    func testOldCancellationCannotReleaseANewerClassifierSlot() {
        let id = UUID(); var scheduler = VehicleViewpointScheduler()
        _ = scheduler.choose([car(id: id)], at: 0, preferredID: nil, lane: nil)
        XCTAssertNotNil(scheduler.choose([car(id: id)], at: 0.3, preferredID: nil, lane: nil))
        scheduler.cancel(for: id, capturedAt: 0)
        XCTAssertNil(scheduler.choose([car(id: id)], at: 0.4, preferredID: nil, lane: nil))
    }
    func testSmallButResolvedRearCropIsEligibleBeforeTheOld32PixelGate() {
        let small = car(CGRect(x: 0.4, y: 0.4, width: 0.025, height: 0.03))
        XCTAssertTrue(VehicleViewpointEligibility.canClassify(small, sourceWidth: 960, sourceHeight: 640))
        XCTAssertFalse(VehicleViewpointEligibility.canClassify(small, sourceWidth: 640, sourceHeight: 384))
    }
    func testClassifierRejectsUnresolvedWeakUnsupportedAndInvalidCrops() {
        XCTAssertFalse(VehicleViewpointEligibility.canClassify(car(confidence: 0.39), sourceWidth: 1280, sourceHeight: 720))
        XCTAssertFalse(VehicleViewpointEligibility.canClassify(car(), sourceWidth: .nan, sourceHeight: 720))
        XCTAssertFalse(VehicleViewpointEligibility.canClassify(car(), sourceWidth: 1280, sourceHeight: 0))
        XCTAssertFalse(VehicleViewpointEligibility.canClassify(LeadVehicle(box: box, label: "bus", confidence: 1), sourceWidth: 1280, sourceHeight: 720))
    }

    private func modelBox(_ full: CGRect, crop: VehicleSearchCrop) -> CGRect {
        CGRect(x: crop.content.minX + (full.minX - crop.region.minX) / crop.region.width * crop.content.width,
               y: crop.content.minY + (full.minY - crop.region.minY) / crop.region.height * crop.content.height,
               width: full.width / crop.region.width * crop.content.width,
               height: full.height / crop.region.height * crop.content.height)
    }
    func testRecoveryCoordinatesPreserveWholeImageRangeInLandscapeAndPortrait() throws {
        let full = CGRect(x: 0.4, y: 0.35, width: 0.10, height: 0.12)
        for aspect in [16.0 / 9, 9.0 / 16, 2.2] {
            let crop = try XCTUnwrap(VehicleSearchCrop(region: CGRect(x: 0.18, y: 0.08, width: 0.64, height: 0.72), imageAspect: aspect))
            let found = try XCTUnwrap(crop.fullImageBox(from: modelBox(full, crop: crop)))
            XCTAssertEqual(found.minX, full.minX, accuracy: 1e-8)
            XCTAssertEqual(found.minY, full.minY, accuracy: 1e-8)
            XCTAssertEqual(found.width, full.width, accuracy: 1e-8)
            XCTAssertEqual(found.height, full.height, accuracy: 1e-8)
        }
    }
    func testCropEdgesAndPaddingCannotProduceWholeCarMeasurements() throws {
        let crop = try XCTUnwrap(VehicleSearchCrop(region: CGRect(x: 0.18, y: 0.08, width: 0.64, height: 0.72), imageAspect: 2))
        XCTAssertNil(crop.fullImageBox(from: CGRect(x: 0.3, y: 0, width: 0.2, height: 0.2)))
        XCTAssertNil(crop.fullImageBox(from: CGRect(x: crop.content.minX, y: 0.4, width: 0.2, height: 0.2)))
        XCTAssertNil(VehicleSearchCrop(region: CGRect(x: -0.1, y: 0.2, width: 0.5, height: 0.5), imageAspect: 2))
        XCTAssertNil(VehicleSearchCrop(region: box, imageAspect: .infinity))
    }
    private func propose(_ recovery: inout VehicleDetectionRecovery, _ vehicles: [LeadVehicle] = [],
                         at time: Double = 0, preferred: CGRect? = nil, seen: Double? = nil,
                         tracked: CGRect? = nil, interval: Double? = 0.75,
                         age: Double = 0.1, latency: Double = 0.08) -> VehicleSearchCrop? {
        recovery.propose(vehicles, at: time, preferredBox: preferred, lastDetectedAt: seen,
                         trackedBox: tracked, lane: nil, imageAspect: 16.0 / 9,
                         interval: interval, captureAge: age, vehicleLatency: latency)
    }
    func testMissedLeadRecoveryIsTargetedAndRateLimited() throws {
        var recovery = VehicleDetectionRecovery()
        let target = CGRect(x: 0.62, y: 0.35, width: 0.10, height: 0.08)
        let crop = try XCTUnwrap(propose(&recovery, preferred: target, seen: 0))
        XCTAssertLessThan(crop.region.minX, target.minX)
        XCTAssertGreaterThan(crop.region.maxX, target.maxX)
        XCTAssertLessThan(crop.region.minY, target.minY)
        XCTAssertGreaterThan(crop.region.maxY, target.maxY)
        XCTAssertGreaterThan(crop.region.midX, 0.6)
        XCTAssertNil(propose(&recovery, at: 0.4, tracked: target))
        XCTAssertNotNil(propose(&recovery, at: 0.75, tracked: target))
    }
    func testStrongFullFrameTargetSkipsRecoveryButAnotherCarCannotBlockIt() {
        var recovery = VehicleDetectionRecovery()
        XCTAssertNil(propose(&recovery, [car()], preferred: box, seen: 0))
        XCTAssertNotNil(propose(&recovery, [car(CGRect(x: 0.02, y: 0.3, width: 0.1, height: 0.1))],
            at: 0.1, preferred: box, seen: 0))
    }
    func testStrongUnconfirmedFullFrameCarDoesNotTriggerExtraInference() {
        var recovery = VehicleDetectionRecovery()
        XCTAssertNil(propose(&recovery, [car()]))
        XCTAssertNotNil(propose(&recovery, [car(confidence: 0.35)], at: 0.1))
    }
    func testHotSlowOldAndFutureFramesDisableRecovery() {
        for input in [(nil as Double?, 0.1, 0.08), (0.75, 0.1, 0.21), (0.75, 0.26, 0.08), (0.75, -0.1, 0.08)] {
            var recovery = VehicleDetectionRecovery()
            XCTAssertNil(propose(&recovery, interval: input.0, age: input.1, latency: input.2))
        }
        XCTAssertNil(VisionWorkPolicy(night: true, heat: .fair).recoveryInterval)
        XCTAssertNil(VisionWorkPolicy(night: false, heat: .serious).recoveryInterval)
        XCTAssertNil(VisionWorkPolicy(night: false, heat: .critical).recoveryInterval)
        XCTAssertNil(VisionWorkPolicy(night: false, heat: .nominal, vehicleLatency: 0.21).recoveryInterval)
    }
    func testExpiredLastPositionCannotKeepTargetedSearchAlive() throws {
        var recovery = VehicleDetectionRecovery()
        let old = CGRect(x: 0.02, y: 0.2, width: 0.10, height: 0.10)
        let crop = try XCTUnwrap(propose(&recovery, at: 2, preferred: old, seen: 0))
        XCTAssertEqual(crop.region.midX, 0.5, accuracy: 1e-8)
    }
    func testRecoveryResetAllowsImmediateSearchInNewTrip() {
        var recovery = VehicleDetectionRecovery()
        XCTAssertNotNil(propose(&recovery))
        recovery.reset()
        XCTAssertNotNil(propose(&recovery, at: 0.1))
        XCTAssertNil(propose(&recovery, at: 0.1))
    }
    func testRecoveryDuplicateKeepsStrongFullFrameGeometryAndSeparateCars() {
        let other = CGRect(x: 0.76, y: 0.3, width: 0.12, height: 0.12)
        let duplicate = LeadVehicle(box: box.offsetBy(dx: 0.005, dy: 0), label: "truck", confidence: 0.98)
        let merged = VehicleDetectionRecovery.merge(fullFrame: [car()], recovery: [duplicate, car(other)], imageAspect: 16.0 / 9)
        XCTAssertEqual(merged.count, 2)
        XCTAssertTrue(merged.contains { $0.box == box && $0.label == "car" })
        XCTAssertTrue(merged.contains { $0.box == other })
    }
    func testBetterWholeCarRecoveryCanReplaceAWeakFullFrameBox() {
        let improved = car(box.offsetBy(dx: 0.004, dy: 0), confidence: 0.9)
        let merged = VehicleDetectionRecovery.merge(fullFrame: [car(confidence: 0.35)], recovery: [improved], imageAspect: 16.0 / 9)
        XCTAssertEqual(merged.count, 1); XCTAssertEqual(merged.first?.confidence, 0.9)
    }
    func testRecoveryStillNeedsRearProofAndCannotAcquireOnConfidenceAlone() {
        var selector = LeadSelector(requiresRearView: true)
        let found = car(id: UUID())
        for time in [0.0, 0.2, 0.4, 0.6] {
            XCTAssertNil(selector.update([found], at: time, lane: lane(time)))
        }
    }

    func testHeldDisplayBoxKeepsCaptureTimeAndExpiresWithoutRenewal() throws {
        var display = VehicleDetectionPresentation(); let id = UUID()
        _ = display.update([car(id: id)], at: 0)
        for time in [0.2, 0.4, 0.45] {
            let held = try XCTUnwrap(display.update([], at: time).first)
            XCTAssertEqual(held.capturedAt, 0); XCTAssertEqual(held.vehicle.trackID, id)
            XCTAssertFalse(held.isFreshDetection)
        }
        XCTAssertTrue(display.update([], at: 0.46).isEmpty)
    }
    func testNewOverlappingIdentityDoesNotLeaveGhostBoxOrInheritRearView() {
        var display = VehicleDetectionPresentation(); let old = UUID(), next = UUID()
        _ = display.update([car(id: old)], at: 0)
        let found = display.update([car(id: next)], at: 0.2)
        XCTAssertEqual(found.count, 1); XCTAssertEqual(found.first?.vehicle.trackID, next)
        XCTAssertTrue(found.first?.isFreshDetection == true); XCTAssertNil(found.first?.vehicle.viewpoint)
    }
    func testDisplayResetDoesNotCarryBoxesIntoNewTrip() {
        var display = VehicleDetectionPresentation()
        _ = display.update([car(id: UUID())], at: 0); display.reset()
        XCTAssertTrue(display.update([], at: 0.1).isEmpty)
    }
    func testShortNightDetectionGapKeepsRealMotionSamples() throws {
        var motion = VehicleMotionGuard(); let id = UUID(); var found: LeadVehicle?
        for time in [0.0, 0.2, 0.4, 0.9] {
            let distance = 30 - time * 2
            let width = 1.8 / (2 * tan(60 * Double.pi / 360) * distance)
            found = motion.update([car(CGRect(x: 0.5 - width / 2, y: 0.3, width: width, height: width * 16 / 9 / 1.5), id: id)],
                at: time, horizontalFOV: 60, ego: nil).first
        }
        let travel = try XCTUnwrap(found?.travel)
        XCTAssertTrue(travel.isReliableApproach); XCTAssertEqual(travel.closingSpeed, 2, accuracy: 1e-6)
    }
    func testLongNightGapStillDiscardsMotionHistory() {
        var motion = VehicleMotionGuard(); let id = UUID(); var found: LeadVehicle?
        for time in [0.0, 0.2, 0.4, 1.05] {
            let width = 1.8 / (2 * tan(60 * Double.pi / 360) * (30 - time * 2))
            found = motion.update([car(CGRect(x: 0.5 - width / 2, y: 0.3, width: width, height: width * 16 / 9 / 1.5), id: id)],
                at: time, horizontalFOV: 60, ego: nil).first
        }
        XCTAssertNil(found?.travel)
    }
    func testReplayRGBAndCaptureYUVFindTheSameWhiteAndYellowMarkings() {
        let width = 640, height = 384
        func rgb(_ x: Int) -> (UInt8, UInt8, UInt8) {
            if abs(x - 192) <= 3 { return (255, 255, 255) }
            if abs(x - 448) <= 3 { return (255, 255, 0) }
            return (50, 50, 50)
        }
        let replay = LaneMarkingScanner.scan(width: width, height: height, excluding: [],
            luma: { x, _ in let p = rgb(x); return FramePixelMath.luma(red: p.0, green: p.1, blue: p.2) },
            chroma: { x, _ in let p = rgb(x); return FramePixelMath.chroma(red: p.0, green: p.1, blue: p.2) })
        let capture = LaneMarkingScanner.scan(width: width, height: height, excluding: [],
            luma: { x, _ in abs(x - 192) <= 3 ? 255 : abs(x - 448) <= 3 ? 226 : 50 },
            chroma: { x, _ in abs(x - 448) <= 3 ? (1, 149) : (128, 128) })
        XCTAssertEqual(replay.count, 24)
        XCTAssertTrue(replay.allSatisfy { $0.peaks.count == 2 })
        for (a, b) in zip(replay, capture) {
            XCTAssertEqual(a.peaks.count, b.peaks.count)
            for (x, y) in zip(a.peaks, b.peaks) { XCTAssertEqual(x.x, y.x, accuracy: 1e-8) }
        }
    }
    func testVideoRangeNormalizesBlackWhiteAndNeutralChroma() {
        XCTAssertEqual(FramePixelMath.fullRangeLuma(16), 0)
        XCTAssertEqual(FramePixelMath.fullRangeLuma(235), 255)
        XCTAssertEqual(FramePixelMath.fullRangeLuma(0), 0)
        XCTAssertEqual(FramePixelMath.fullRangeLuma(255), 255)
        XCTAssertEqual(FramePixelMath.fullRangeChroma(128), 128)
        XCTAssertEqual(FramePixelMath.fullRangeChroma(240), 255)
        XCTAssertEqual(FramePixelMath.luma(red: 255, green: 255, blue: 255), 255)
        let neutral = FramePixelMath.chroma(red: 100, green: 100, blue: 100)
        XCTAssertEqual(neutral.0, 128); XCTAssertEqual(neutral.1, 128)
    }
}
