import Foundation
import XCTest
@testable import BrakeCheckCore

final class AppRecoveryStorageTests: XCTestCase {
    func testRetriesBackOffAndResetOnRecovery() {
        var recovery = NavigationRecovery()
        XCTAssertTrue(recovery.canAttempt(at: 0))
        recovery.failed(at: 100)
        XCTAssertFalse(recovery.canAttempt(at: 104))
        XCTAssertTrue(recovery.canAttempt(at: 105))
        recovery.failed(at: 105)
        XCTAssertEqual(recovery.retryAt, 115)
        for _ in 0..<10 { recovery.failed(at: 200) }
        XCTAssertEqual(recovery.retryAt, 260)
        recovery.reset()
        recovery.failed(at: 300)
        XCTAssertEqual(recovery.retryAt, 305)
        XCTAssertFalse(recovery.canAttempt(at: .nan))
    }
    func testCleanupIsOptInAndProtectsActiveOrCollectingFootage() {
        let now = Date(timeIntervalSince1970: 10_000_000)
        var event = IncidentManifest(at: now.addingTimeInterval(-31 * 86_400), reason: "Saved")
        event.state = "Saved available footage"
        XCTAssertFalse(RecordingStoragePolicy.shouldDelete(event, now: now, retentionDays: 0, protectedIDs: []))
        XCTAssertTrue(RecordingStoragePolicy.shouldDelete(event, now: now, retentionDays: 30, protectedIDs: []))
        XCTAssertFalse(RecordingStoragePolicy.shouldDelete(event, now: now, retentionDays: 30, protectedIDs: [event.id]))
        event.state = "Collecting available footage"
        XCTAssertFalse(RecordingStoragePolicy.shouldDelete(event, now: now, retentionDays: 7, protectedIDs: []))
    }
    func testCleanupRetainsRecentFutureAndUnsupportedPreferences() {
        let now = Date(timeIntervalSince1970: 10_000_000)
        var recent = IncidentManifest(at: now.addingTimeInterval(-6 * 86_400), reason: "Recent")
        recent.state = "Saved available footage"
        XCTAssertFalse(RecordingStoragePolicy.shouldDelete(recent, now: now, retentionDays: 7, protectedIDs: []))
        XCTAssertFalse(RecordingStoragePolicy.shouldDelete(recent, now: now, retentionDays: -1, protectedIDs: []))
        var future = IncidentManifest(at: now, reason: "Pending")
        future.state = "Saved available footage; trip ended"
        XCTAssertFalse(RecordingStoragePolicy.shouldDelete(future, now: now, retentionDays: 7, protectedIDs: []))
        XCTAssertNil(RecordingStoragePolicy.limitBytes(gigabytes: 0))
        XCTAssertNil(RecordingStoragePolicy.limitBytes(gigabytes: -1))
        XCTAssertEqual(RecordingStoragePolicy.limitBytes(gigabytes: 2), 2_000_000_000)
    }
    func testCSVPreservesUnavailableSpeedAndLabelsEstimates() {
        let missing = TripMetrics(startedAt: Date(timeIntervalSince1970: 100), endedAt: Date(timeIntervalSince1970: 220),
            maxSpeedMPH: nil, estimatedLeadHardBrakes: 2, estimatedCloseCalls: 3)
        let zero = TripMetrics(startedAt: missing.startedAt, endedAt: missing.endedAt, maxSpeedMPH: 0,
            estimatedLeadHardBrakes: 0, estimatedCloseCalls: 0)
        let rows = TripMetricsCSV.encode([missing, zero]).components(separatedBy: "\r\n")
        XCTAssertTrue(rows[0].contains("estimated_lead_hard_brakes"))
        XCTAssertEqual(rows[1].components(separatedBy: ",")[4], "")
        XCTAssertEqual(rows[2].components(separatedBy: ",")[4], "0.00")
        XCTAssertEqual(rows[1].components(separatedBy: ",")[3], "120.00")
    }
    func testManeuverSymbolsDistinguishTurnsAndUTurns() {
        XCTAssertEqual(NavigationManeuver.symbol(for: "Turn left onto Main Street"), "arrow.turn.up.left")
        XCTAssertEqual(NavigationManeuver.symbol(for: "Make a U-turn to the right"), "arrow.uturn.left")
        XCTAssertEqual(NavigationManeuver.symbol(for: "Continue straight"), "arrow.up")
        XCTAssertEqual(NavigationManeuver.symbol(for: "Continue onto Right Street"), "arrow.up")
    }
}
