import XCTest
@testable import BrakeCheckCore

final class LeadSelectorTests: XCTestCase {
    private func car(x: Double = 0.5, y: Double = 0.30, width: Double = 0.12,
                     height: Double = 0.11, confidence: Double = 0.9, label: String = "car") -> LeadVehicle {
        LeadVehicle(box: CGRect(x: x - width / 2, y: y, width: width, height: height),
                    label: label, confidence: confidence)
    }
    private func lane(at time: Double, bend: Double = 0) -> LanePath {
        LanePath(bands: (0..<20).map { i in
            let y = 0.06 + Double(i) * 0.025
            let center = 0.5 + bend * pow(y / 0.55, 2)
            let half = 0.30 - 0.44 * y
            return LaneBand(y: y, left: center - half, right: center + half)
        }, confidence: 0.9, capturedAt: time)
    }
    private func confirm(_ selector: inout LeadSelector, _ cars: [LeadVehicle],
                         from start: Double = 0, bend: Double? = nil) -> LeadSelection? {
        var result: LeadSelection?
        for i in 0...3 {
            let time = start + Double(i) * 0.1
            result = selector.update(cars, at: time, lane: bend.map { lane(at: time, bend: $0) })
        }
        return result
    }
    func testThreeObservationsAndPointTwoSecondsConfirm() throws {
        var s = LeadSelector()
        XCTAssertNil(s.update([car()], at: 0))
        XCTAssertNil(s.update([car()], at: 0.1))
        XCTAssertNotNil(s.update([car()], at: 0.2))
    }
    func testElapsedTimeAloneDoesNotConfirm() {
        var s = LeadSelector()
        XCTAssertNil(s.update([car()], at: 0))
        XCTAssertNil(s.update([car()], at: 0.3))
    }
    func testLowerConfidenceAcquiresAndEvenLowerConfidenceRetains() throws {
        var s = LeadSelector()
        let first = try XCTUnwrap(confirm(&s, [car(confidence: 0.46)]))
        let next = try XCTUnwrap(s.update([car(confidence: 0.30)], at: 0.4))
        XCTAssertEqual(next.id, first.id)
        XCTAssertTrue(next.isFreshDetection)
    }
    func testLowConfidenceCannotCreateNewTrack() {
        var s = LeadSelector()
        XCTAssertNil(confirm(&s, [car(confidence: 0.30)]))
    }
    func testOneMissRetainsIdentityButDoesNotBecomeMeasurement() throws {
        var s = LeadSelector()
        let first = try XCTUnwrap(confirm(&s, [car()]))
        let held = try XCTUnwrap(s.update([], at: 0.4))
        XCTAssertEqual(held.id, first.id)
        XCTAssertFalse(held.isFreshDetection)
        let resumed = try XCTUnwrap(s.update([car()], at: 0.5))
        XCTAssertEqual(resumed.id, first.id)
        XCTAssertTrue(resumed.isFreshDetection)
    }
    func testVisualTrackerCanFollowDuringGap() throws {
        var s = LeadSelector()
        let first = try XCTUnwrap(confirm(&s, [car()]))
        let moved = car(x: 0.54).box
        let held = try XCTUnwrap(s.update([], at: 0.4, trackedBox: moved))
        XCTAssertEqual(held.id, first.id)
        XCTAssertEqual(held.vehicle.box, moved)
        XCTAssertFalse(held.isFreshDetection)
    }
    func testGapExpiresAndNewTrackHasNewIdentity() throws {
        var s = LeadSelector()
        let first = try XCTUnwrap(confirm(&s, [car()]))
        let held = try XCTUnwrap(s.update([], at: 0.9))
        XCTAssertEqual(held.id, first.id)
        XCTAssertFalse(held.isFreshDetection)
        XCTAssertNil(s.update([], at: 1.3))
        let second = try XCTUnwrap(confirm(&s, [car()], from: 1.4))
        XCTAssertNotEqual(first.id, second.id)
    }
    func testVehicleOnCurveOutsideStraightGuideCanAcquire() throws {
        var s = LeadSelector()
        let y = 0.46, bend = 0.26
        let x = 0.5 + bend * pow(y / 0.55, 2)
        XCTAssertFalse(LeadCorridor().contains(car(x: x, y: y, width: 0.08, height: 0.07).box))
        let lead = try XCTUnwrap(confirm(&s, [car(x: x, y: y, width: 0.08, height: 0.07)], bend: bend))
        XCTAssertEqual(lead.vehicle.box.midX, x, accuracy: 0.0001)
    }
    func testGradualCurveMotionKeepsIdentity() throws {
        var s = LeadSelector()
        let y = 0.45
        let original = try XCTUnwrap(confirm(&s, [car(y: y)], bend: 0))
        for i in 4...18 {
            let time = Double(i) * 0.1, bend = Double(i - 3) * 0.018
            let x = 0.5 + bend * pow(y / 0.55, 2)
            let reading = try XCTUnwrap(s.update([car(x: x, y: y)], at: time, lane: lane(at: time, bend: bend)))
            XCTAssertEqual(reading.id, original.id)
            XCTAssertTrue(reading.isFreshDetection)
        }
    }
    func testKnownLaneExcludesOncomingLaneAndRoadsideCandidates() {
        var s = LeadSelector()
        let offLane = [car(x: 0.34, y: 0.48, width: 0.08, height: 0.07),
                       car(x: 0.83, y: 0.18, width: 0.18, height: 0.16)]
        XCTAssertNil(confirm(&s, offLane, bend: 0))
    }
    func testStoppedVehicleInLaneRemainsEligible() throws {
        var s = LeadSelector()
        let original = try XCTUnwrap(confirm(&s, [car()], bend: 0))
        for i in 4...12 {
            let time = Double(i) * 0.1
            XCTAssertEqual(try XCTUnwrap(s.update([car()], at: time, lane: lane(at: time))).id, original.id)
        }
    }
    func testNearerCentralVehicleWins() throws {
        var s = LeadSelector()
        let near = car(y: 0.16, width: 0.24, height: 0.2)
        let far = car(y: 0.44, width: 0.07, height: 0.07, confidence: 0.99)
        XCTAssertEqual(try XCTUnwrap(confirm(&s, [far, near], bend: 0)).vehicle.box, near.box)
    }
    func testCutInConfirmsThenChangesIdentityWithoutMixingMeasurements() throws {
        var s = LeadSelector()
        let far = car(y: 0.44, width: 0.08, height: 0.08)
        let near = car(y: 0.18, width: 0.22, height: 0.18)
        let original = try XCTUnwrap(confirm(&s, [far], bend: 0))
        let pending = try XCTUnwrap(s.update([far, near], at: 0.4, lane: lane(at: 0.4)))
        XCTAssertEqual(pending.id, original.id)
        XCTAssertFalse(pending.isFreshDetection)
        _ = s.update([far, near], at: 0.5, lane: lane(at: 0.5))
        let changed = try XCTUnwrap(s.update([far, near], at: 0.6, lane: lane(at: 0.6)))
        XCTAssertNotEqual(changed.id, original.id)
        XCTAssertEqual(changed.vehicle.box, near.box)
        XCTAssertTrue(changed.isFreshDetection)
    }
    func testClearLaneExitPausesMetricsThenReleasesAfterBoundaryGrace() throws {
        var s = LeadSelector()
        let original = try XCTUnwrap(confirm(&s, [car(y: 0.52, width: 0.20, height: 0.13)], bend: 0))
        let outside = car(x: 0.62, y: 0.52, width: 0.20, height: 0.13)
        for time in [0.4, 0.5, 0.6] {
            let held = try XCTUnwrap(s.update([outside], at: time, lane: lane(at: time)))
            XCTAssertEqual(held.id, original.id)
            XCTAssertFalse(held.isFreshDetection)
            XCTAssertFalse(held.isPathSupported)
        }
        XCTAssertNil(s.update([outside], at: 0.7, lane: lane(at: 0.7)))
    }
    func testAmbiguousTwoCarsDoNotShareRangeHistory() throws {
        var s = LeadSelector()
        _ = try XCTUnwrap(confirm(&s, [car()]))
        XCTAssertNil(s.update([car(x: 0.48), car(x: 0.52)], at: 0.4))
    }
    func testClassChangeStartsFreshIdentity() throws {
        var s = LeadSelector()
        let original = try XCTUnwrap(confirm(&s, [car()]))
        XCTAssertNil(s.update([car(label: "truck")], at: 0.4))
        _ = s.update([car(label: "truck")], at: 0.5)
        let changed = try XCTUnwrap(s.update([car(label: "truck")], at: 0.6))
        XCTAssertNotEqual(changed.id, original.id)
        XCTAssertEqual(changed.vehicle.assumedWidthMeters, 2.5)
    }
    func testLargeSizeJumpStartsFreshIdentity() throws {
        var s = LeadSelector()
        let original = try XCTUnwrap(confirm(&s, [car()]))
        let bigger = car(width: 0.30, height: 0.26)
        XCTAssertNil(s.update([bigger], at: 0.4))
        let changed = try XCTUnwrap(confirm(&s, [bigger], from: 0.5))
        XCTAssertNotEqual(changed.id, original.id)
    }
    func testInvalidGeometryClassesAndTimesReset() throws {
        let invalid = [car(y: 0.66), car(x: 0.96, width: 0.2), car(width: 0.001),
                       car(confidence: .nan), car(confidence: 1.1), car(label: "person")]
        for v in invalid { var s = LeadSelector(); XCTAssertNil(confirm(&s, [v])) }
        for time in [0.3, 0.2, .nan, .infinity] {
            var s = LeadSelector()
            _ = try XCTUnwrap(confirm(&s, [car()]))
            XCTAssertNil(s.update([car()], at: time))
        }
    }
    func testStaleLaneCannotPullSelectionTowardOldCurve() {
        var s = LeadSelector()
        let stale = lane(at: 0, bend: 0.3)
        for i in 0...3 { _ = s.update([car(x: 0.7, y: 0.48, width: 0.08, height: 0.07)], at: 1 + Double(i) * 0.1, lane: stale) }
        XCTAssertNil(s.update([car(x: 0.7, y: 0.48, width: 0.08, height: 0.07)], at: 1.4, lane: stale))
    }
    func testResetClearsConfirmationAndIdentity() throws {
        var s = LeadSelector()
        let original = try XCTUnwrap(confirm(&s, [car()]))
        s.reset()
        XCTAssertNil(s.update([car()], at: 0.4))
        XCTAssertNotEqual(try XCTUnwrap(confirm(&s, [car()], from: 0.5)).id, original.id)
    }
}
