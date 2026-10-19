import XCTest
@testable import BrakeCheckCore

final class RetentionTests: XCTestCase {
    private let trigger = Date(timeIntervalSince1970: 100_000)

    func testWindowIsFifteenMinutesOnEachSide() {
        let event = IncidentManifest(at: trigger, reason: "Test")
        XCTAssertEqual(trigger.timeIntervalSince(event.requestedStart), 900)
        XCTAssertEqual(event.requestedEnd.timeIntervalSince(trigger), 900)
    }

    func testSegmentStraddlingStartIsRetained() {
        let event = IncidentManifest(at: trigger, reason: "Test")
        let segment = makeSegment(start: -905, end: -895)
        XCTAssertTrue(RetentionPolicy.overlaps(segment, event))
    }

    func testPostWindowFootageIsExcluded() {
        let event = IncidentManifest(at: trigger, reason: "Test")
        XCTAssertFalse(RetentionPolicy.overlaps(makeSegment(start: 901, end: 911), event))
        XCTAssertFalse(RetentionPolicy.overlaps(makeSegment(start: -920, end: -910), event))
    }

    func testOldRingSegmentExpiresButBoundaryIsPreserved() {
        XCTAssertTrue(RetentionPolicy.shouldDeleteFromRing(makeSegment(start: -920, end: -910), now: trigger))
        XCTAssertFalse(RetentionPolicy.shouldDeleteFromRing(makeSegment(start: -905, end: -895), now: trigger))
    }

    func testOneSegmentCanBelongToTwoNearbyEvents() {
        let first = IncidentManifest(at: trigger, reason: "First")
        let second = IncidentManifest(at: trigger.addingTimeInterval(60), reason: "Second")
        let shared = makeSegment(start: 0, end: 10)
        XCTAssertTrue(RetentionPolicy.overlaps(shared, first))
        XCTAssertTrue(RetentionPolicy.overlaps(shared, second))
        XCTAssertNotEqual(first.id, second.id)
    }

    func testManifestRoundTripPreservesRequestedWindowAndGaps() throws {
        var event = IncidentManifest(at: trigger, reason: "Manual")
        event.segments = [makeSegment(start: -20, end: -10), makeSegment(start: 20, end: 30)]
        event.notes = ["Background interruption"]
        let data = try JSONEncoder().encode(event)
        let restored = try JSONDecoder().decode(IncidentManifest.self, from: data)
        XCTAssertEqual(restored.id, event.id)
        XCTAssertEqual(restored.segments, event.segments)
        XCTAssertEqual(restored.notes, event.notes)
    }

    private func makeSegment(start: Double, end: Double) -> VideoSegment {
        VideoSegment(filename: "test.mp4", start: trigger.addingTimeInterval(start), end: trigger.addingTimeInterval(end))
    }
}
