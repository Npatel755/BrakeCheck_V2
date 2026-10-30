import Foundation
import XCTest
@testable import BrakeCheckCore

final class TripArchiveTests: XCTestCase {
    private func fixture() -> (URL, URL, URL, URL) {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        return (root, root.appendingPathComponent("history/trips.json"),
                root.appendingPathComponent("routes"), root.appendingPathComponent("pending"))
    }
    private func trip(id: UUID = UUID()) -> TripMetrics {
        TripMetrics(id: id, startedAt: Date(timeIntervalSince1970: 1_000.123),
            endedAt: Date(timeIntervalSince1970: 1_120.789), maxSpeedMPH: 50,
            estimatedLeadHardBrakes: 1, estimatedCloseCalls: 2)
    }
    private func route() -> TripRoute {
        TripRoute(destination: "Destination", points: [
            .init(latitude: 33, longitude: -84, time: Date(timeIntervalSince1970: 1_000.123)),
            .init(latitude: 33.001, longitude: -84, time: Date(timeIntervalSince1970: 1_005.456))])
    }
    func testTripAndRouteSaveReloadAndRetryWithoutDuplicates() async throws {
        let (root, history, routes, pending) = fixture()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = TripArchiveStore(historyURL: history, routesFolder: routes, pendingFolder: pending)
        let trip = trip(), route = route()
        let saved = try await store.save(trip, route: route)
        XCTAssertEqual(saved.trips.map(\.id), [trip.id]); XCTAssertEqual(saved.pendingCount, 0)
        let retry = try await store.save(trip, route: route)
        XCTAssertEqual(retry.trips.count, 1)
        let restarted = TripArchiveStore(historyURL: history, routesFolder: routes, pendingFolder: pending)
        let snapshot = try await restarted.load()
        XCTAssertEqual(snapshot.trips[0].startedAt.timeIntervalSince1970, 1_000.123, accuracy: 0.001)
        let restored = try await restarted.loadRoute(trip.id)
        XCTAssertEqual(restored?.destination, "Destination")
        XCTAssertEqual(try XCTUnwrap(restored?.points.last).time.timeIntervalSince1970, 1_005.456, accuracy: 0.001)
    }
    func testInterruptedRouteWriteIsRecoveredByNewStore() async throws {
        let (root, history, routes, pending) = fixture()
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try Data("blocked folder".utf8).write(to: routes)
        let store = TripArchiveStore(historyURL: history, routesFolder: routes, pendingFolder: pending)
        let trip = trip()
        do { _ = try await store.save(trip, route: route()); XCTFail("Expected blocked route write") } catch { }
        let journals = try FileManager.default.contentsOfDirectory(at: pending, includingPropertiesForKeys: nil)
        XCTAssertEqual(journals.count, 1)
        try FileManager.default.removeItem(at: routes)
        let restarted = TripArchiveStore(historyURL: history, routesFolder: routes, pendingFolder: pending)
        let snapshot = try await restarted.load()
        XCTAssertEqual(snapshot.trips.map(\.id), [trip.id]); XCTAssertEqual(snapshot.pendingCount, 0)
        XCTAssertTrue(snapshot.warnings.isEmpty)
        let restored = try await restarted.loadRoute(trip.id)
        XCTAssertEqual(restored?.points.count, 2)
    }
    func testCorruptHistoryPreservesJournalAndRecoversAfterHistoryRepair() async throws {
        let (root, history, routes, pending) = fixture()
        defer { try? FileManager.default.removeItem(at: root) }
        let metrics = TripMetricsStore(url: history)
        let originalTrip = trip(); _ = try metrics.save(originalTrip)
        let original = try Data(contentsOf: history), corrupt = Data("damaged history".utf8)
        try corrupt.write(to: history)
        let store = TripArchiveStore(historyURL: history, routesFolder: routes, pendingFolder: pending)
        let next = trip()
        do { _ = try await store.save(next, route: route()); XCTFail("Corrupt history must not be replaced") } catch { }
        XCTAssertEqual(try Data(contentsOf: history), corrupt)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(at: pending, includingPropertiesForKeys: nil).count, 1)
        try original.write(to: history)
        let result = try await store.load()
        XCTAssertEqual(Set(result.trips.map(\.id)), [next.id, originalTrip.id])
        XCTAssertEqual(result.pendingCount, 0)
    }
    func testConflictingRouteCannotBeOverwrittenAndBlocksPrivacyDeletion() async throws {
        let (root, history, routes, pending) = fixture()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = TripArchiveStore(historyURL: history, routesFolder: routes, pendingFolder: pending)
        let trip = trip(); _ = try await store.save(trip, route: route())
        let path = routes.appendingPathComponent(trip.id.uuidString + ".json"), original = try Data(contentsOf: path)
        var changed = route(); changed.destination = "Different"
        do { _ = try await store.save(trip, route: changed); XCTFail("Must preserve original route") } catch { }
        XCTAssertEqual(try Data(contentsOf: path), original)
        let result = try await store.load()
        XCTAssertEqual(result.pendingCount, 1); XCTAssertEqual(result.warnings.count, 1)
        do { try await store.deleteRoutes(); XCTFail("Pending GPS data must not silently reappear after deletion") } catch { }
        XCTAssertEqual(try Data(contentsOf: path), original)
    }
    func testMalformedJournalAndRouteAreKeptAndReported() async throws {
        let (root, history, routes, pending) = fixture()
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: pending, withIntermediateDirectories: true)
        let malformed = pending.appendingPathComponent("broken.json"), bytes = Data("broken".utf8)
        try bytes.write(to: malformed)
        let store = TripArchiveStore(historyURL: history, routesFolder: routes, pendingFolder: pending)
        let snapshot = try await store.load()
        XCTAssertEqual(snapshot.pendingCount, 1); XCTAssertEqual(snapshot.warnings.count, 1)
        XCTAssertEqual(try Data(contentsOf: malformed), bytes)
        try FileManager.default.createDirectory(at: routes, withIntermediateDirectories: true)
        let id = UUID(), path = routes.appendingPathComponent(id.uuidString + ".json")
        try bytes.write(to: path)
        do { _ = try await store.loadRoute(id); XCTFail("Damaged route must be reported") } catch { }
        XCTAssertEqual(try Data(contentsOf: path), bytes)
    }
    func testDeleteRoutesKeepsMetricsAndDoesNotRecreateThemOnReload() async throws {
        let (root, history, routes, pending) = fixture()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = TripArchiveStore(historyURL: history, routesFolder: routes, pendingFolder: pending)
        let trip = trip(); _ = try await store.save(trip, route: route())
        try await store.deleteRoutes()
        let snapshot = try await store.load(), removed = try await store.loadRoute(trip.id)
        XCTAssertEqual(snapshot.trips.map(\.id), [trip.id]); XCTAssertNil(removed)
    }
    func testJournalWithMismatchedIdentityIsPreserved() async throws {
        let (root, history, routes, pending) = fixture()
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try Data("blocked".utf8).write(to: routes)
        let store = TripArchiveStore(historyURL: history, routesFolder: routes, pendingFolder: pending)
        let trip = trip()
        do { _ = try await store.save(trip, route: route()) } catch { }
        let original = pending.appendingPathComponent(trip.id.uuidString + ".json")
        let moved = pending.appendingPathComponent(UUID().uuidString + ".json")
        try FileManager.default.moveItem(at: original, to: moved)
        let bytes = try Data(contentsOf: moved)
        try FileManager.default.removeItem(at: routes)
        let result = try await store.load()
        XCTAssertTrue(result.trips.isEmpty); XCTAssertEqual(result.pendingCount, 1)
        XCTAssertEqual(try Data(contentsOf: moved), bytes)
        XCTAssertFalse(FileManager.default.fileExists(atPath: routes.path))
    }
    func testConflictingTotalsCannotBeReplacedByRecoveredJournal() async throws {
        let (root, history, routes, pending) = fixture()
        defer { try? FileManager.default.removeItem(at: root) }
        let metrics = TripMetricsStore(url: history), id = UUID()
        let old = TripMetrics(id: id, startedAt: Date(timeIntervalSince1970: 1_000.123),
            endedAt: Date(timeIntervalSince1970: 1_120.789), maxSpeedMPH: 40,
            estimatedLeadHardBrakes: 0, estimatedCloseCalls: 0)
        _ = try metrics.save(old)
        let original = try Data(contentsOf: history)
        let store = TripArchiveStore(historyURL: history, routesFolder: routes, pendingFolder: pending)
        do { _ = try await store.save(trip(id: id), route: route()); XCTFail("Different totals cannot replace completed trip") } catch { }
        let result = try await store.load()
        XCTAssertEqual(result.trips[0].maxSpeedMPH, 40)
        XCTAssertEqual(result.pendingCount, 1)
        XCTAssertEqual(try Data(contentsOf: history), original)
        XCTAssertFalse(FileManager.default.fileExists(atPath: routes.path))
    }
    func testDamagedPendingTripDoesNotPreventAnotherTripFromSaving() async throws {
        let (root, history, routes, pending) = fixture()
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: pending, withIntermediateDirectories: true)
        let corrupt = pending.appendingPathComponent("damaged.json")
        try Data("preserve".utf8).write(to: corrupt)
        let store = TripArchiveStore(historyURL: history, routesFolder: routes, pendingFolder: pending)
        let trip = trip(), result = try await store.save(trip, route: route())
        XCTAssertEqual(result.trips.map(\.id), [trip.id])
        XCTAssertEqual(result.pendingCount, 1); XCTAssertEqual(result.warnings.count, 1)
        XCTAssertEqual(try Data(contentsOf: corrupt), Data("preserve".utf8))
    }
    func testConcurrentSavesAreSerializedWithoutLosingTrips() async throws {
        let (root, history, routes, pending) = fixture()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = TripArchiveStore(historyURL: history, routesFolder: routes, pendingFolder: pending)
        let first = trip(), second = trip()
        async let a = store.save(first, route: route())
        async let b = store.save(second, route: route())
        _ = try await (a, b)
        let result = try await store.load()
        XCTAssertEqual(Set(result.trips.map(\.id)), [first.id, second.id]); XCTAssertEqual(result.pendingCount, 0)
    }

}
