import Foundation
import XCTest
@testable import BrakeCheckCore

final class TripMetricsStoreTests: XCTestCase {
    private func trip(_ start: Double, id: UUID = UUID(), speed: Double? = 55) -> TripMetrics {
        TripMetrics(id: id, startedAt: Date(timeIntervalSince1970: start),
                    endedAt: Date(timeIntervalSince1970: start + 120), maxSpeedMPH: speed,
                    estimatedLeadHardBrakes: 2, estimatedCloseCalls: 3)
    }
    private func withStore(_ work: (TripMetricsStore) throws -> Void) throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        try work(TripMetricsStore(url: folder.appendingPathComponent("TripMetrics/history.json")))
    }
    func testCompletedTripsSurviveReloadAndAreNewestFirst() throws {
        try withStore { store in
            XCTAssertTrue(try store.load().isEmpty)
            let first = trip(100), second = trip(500, speed: nil)
            _ = try store.save(second); _ = try store.save(first)
            let restored = try TripMetricsStore(url: store.url).load()
            XCTAssertEqual(restored.map(\.id), [second.id, first.id])
            XCTAssertNil(restored[0].maxSpeedMPH)
            XCTAssertEqual(restored[1].maxSpeedMPH, 55)
            XCTAssertEqual(restored[1].durationSeconds, 120)
            XCTAssertEqual(restored[1].estimatedLeadHardBrakes, 2)
            XCTAssertEqual(restored[1].estimatedCloseCalls, 3)
        }
    }
    func testRetryDoesNotDuplicateTrip() throws {
        try withStore { store in
            let saved = trip(100)
            _ = try store.save(saved)
            XCTAssertEqual(try store.save(saved).count, 1)
            XCTAssertEqual(try store.load().first?.id, saved.id)
        }
    }
    func testInvalidMetricsCannotOverwriteExistingHistory() throws {
        try withStore { store in
            _ = try store.save(trip(100))
            let original = try Data(contentsOf: store.url)
            XCTAssertThrowsError(try store.save(trip(500, speed: -1)))
            XCTAssertThrowsError(try store.save(trip(500, speed: .infinity)))
            let reversed = TripMetrics(startedAt: Date(timeIntervalSince1970: 500),
                endedAt: Date(timeIntervalSince1970: 100), maxSpeedMPH: nil,
                estimatedLeadHardBrakes: 0, estimatedCloseCalls: 0)
            XCTAssertThrowsError(try store.save(reversed))
            XCTAssertEqual(try Data(contentsOf: store.url), original)
        }
    }
    func testCorruptHistoryIsReportedAndPreserved() throws {
        try withStore { store in
            _ = try store.save(trip(100))
            let corrupt = Data("invalid json".utf8)
            try corrupt.write(to: store.url)
            XCTAssertThrowsError(try store.load())
            XCTAssertThrowsError(try store.save(trip(500)))
            XCTAssertEqual(try Data(contentsOf: store.url), corrupt)
        }
    }
}
