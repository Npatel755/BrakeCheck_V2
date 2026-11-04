import Foundation
import XCTest
@testable import BrakeCheckCore

final class RecordingRecoveryTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 100_000)

    private func withLibrary(_ work: (URL, URL) throws -> Void) throws {
        // Foundation's resolvingSymlinksInPath keeps the /var alias on macOS while
        // contentsOfDirectory reports /private/var, so resolve with realpath(3).
        let temporary = FileManager.default.temporaryDirectory.path
        let resolved = temporary.withCString { realpath($0, nil) }.map { pointer -> String in
            defer { free(pointer) }
            return String(cString: pointer)
        } ?? temporary
        let root = URL(fileURLWithPath: resolved, isDirectory: true).appendingPathComponent(UUID().uuidString)
        let ring = root.appendingPathComponent("ring"), incidents = root.appendingPathComponent("incidents")
        try FileManager.default.createDirectory(at: ring, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: incidents, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        try work(ring, incidents)
    }

    private func write<T: Encodable>(_ value: T, to url: URL) throws {
        let encoder = JSONEncoder(); encoder.dateEncodingStrategy = .iso8601
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try encoder.encode(value).write(to: url)
    }

    private func clip(_ filename: String = "test.mp4") -> VideoSegment {
        VideoSegment(filename: filename, start: now, end: now.addingTimeInterval(10))
    }

    func testValidLibraryIsRestoredWithoutChangingFiles() throws {
        try withLibrary { ring, incidents in
            let segment = clip()
            let movie = ring.appendingPathComponent(segment.filename)
            let metadata = movie.appendingPathExtension("json")
            try Data("completed video".utf8).write(to: movie)
            try write(segment, to: metadata)
            var event = IncidentManifest(at: now, reason: "Manual")
            event.segments = [segment]
            let folder = incidents.appendingPathComponent(event.id.uuidString)
            let manifest = folder.appendingPathComponent("manifest.json")
            try write(event, to: manifest)
            try FileManager.default.copyItem(at: movie, to: folder.appendingPathComponent(segment.filename))
            let before = try Data(contentsOf: manifest)
            let restored = try RecordingLibraryRecovery.load(ringDirectory: ring, incidentsDirectory: incidents)
            XCTAssertEqual(restored.ring, [segment])
            XCTAssertEqual(restored.incidents.map(\.id), [event.id])
            XCTAssertTrue(restored.warnings.isEmpty)
            XCTAssertTrue(restored.unindexed.isEmpty)
            XCTAssertFalse(restored.preserveRing)
            XCTAssertEqual(try Data(contentsOf: manifest), before)
            XCTAssertEqual(try Data(contentsOf: movie), Data("completed video".utf8))
        }
    }

    func testFractionalMetadataIsRecoveredWithoutLosingClipTiming() throws {
        try withLibrary { ring, incidents in
            let start = now.addingTimeInterval(0.125)
            let segment = VideoSegment(filename: "fractional.mp4", start: start, end: start.addingTimeInterval(0.5))
            let movie = ring.appendingPathComponent(segment.filename)
            try Data("single completed frame".utf8).write(to: movie)
            try RecordingMetadataCodec.encoder().encode(segment).write(to: movie.appendingPathExtension("json"))
            var event = IncidentManifest(at: start, reason: "Test")
            event.segments = [segment]
            let directory = incidents.appendingPathComponent(event.id.uuidString)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            try RecordingMetadataCodec.encoder().encode(event).write(to: directory.appendingPathComponent("manifest.json"))
            try FileManager.default.copyItem(at: movie, to: directory.appendingPathComponent(segment.filename))
            let snapshot = try RecordingLibraryRecovery.load(ringDirectory: ring, incidentsDirectory: incidents)
            XCTAssertEqual(snapshot.ring, [segment])
            XCTAssertEqual(snapshot.incidents.first?.triggerTime, event.triggerTime)
            XCTAssertTrue(snapshot.warnings.isEmpty)
            XCTAssertTrue(snapshot.unindexed.isEmpty)
        }
    }

    func testDamagedOrMissingRingMetadataKeepsFootageAvailableForExport() throws {
        try withLibrary { ring, incidents in
            let first = ring.appendingPathComponent("damaged.mp4"), second = ring.appendingPathComponent("unindexed.mp4")
            for url in [first, second] { try Data("preserve".utf8).write(to: url) }
            let bad = first.appendingPathExtension("json")
            try Data("damaged metadata".utf8).write(to: bad)
            let snapshot = try RecordingLibraryRecovery.load(ringDirectory: ring, incidentsDirectory: incidents)
            XCTAssertTrue(snapshot.ring.isEmpty)
            XCTAssertEqual(Set(snapshot.unindexed.map(\.url)), [first, second])
            XCTAssertEqual(snapshot.warnings.count, 1)
            XCTAssertEqual(try Data(contentsOf: bad), Data("damaged metadata".utf8))
            for url in [first, second] { XCTAssertEqual(try Data(contentsOf: url), Data("preserve".utf8)) }
        }
    }

    func testDamagedIncidentKeepsRingAndUnindexedIncidentVideo() throws {
        try withLibrary { ring, incidents in
            let segment = clip()
            try write(segment, to: ring.appendingPathComponent("test.mp4.json"))
            try Data("ring clip".utf8).write(to: ring.appendingPathComponent("test.mp4"))
            let folder = incidents.appendingPathComponent(UUID().uuidString)
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            let manifest = folder.appendingPathComponent("manifest.json")
            try Data("broken".utf8).write(to: manifest)
            let movie = folder.appendingPathComponent("saved.mp4")
            try Data("saved clip".utf8).write(to: movie)
            let snapshot = try RecordingLibraryRecovery.load(ringDirectory: ring, incidentsDirectory: incidents)
            XCTAssertTrue(snapshot.preserveRing)
            XCTAssertEqual(snapshot.ring, [segment])
            XCTAssertEqual(snapshot.unindexed.map(\.url), [movie])
            XCTAssertTrue(snapshot.incidents.isEmpty)
            XCTAssertEqual(try Data(contentsOf: manifest), Data("broken".utf8))
        }
    }

    func testMissingArchivedClipProtectsIncidentWithoutDroppingItsTiming() throws {
        try withLibrary { ring, incidents in
            var event = IncidentManifest(at: now, reason: "Saved")
            event.state = "Saved available footage"
            event.segments = [clip("missing.mp4")]
            let manifest = incidents.appendingPathComponent(event.id.uuidString).appendingPathComponent("manifest.json")
            try write(event, to: manifest)
            let snapshot = try RecordingLibraryRecovery.load(ringDirectory: ring, incidentsDirectory: incidents)
            XCTAssertEqual(snapshot.incidents.first?.segments, event.segments)
            XCTAssertEqual(snapshot.protectedIncidentIDs, [event.id])
            XCTAssertFalse(RecordingStoragePolicy.shouldDelete(event, now: now.addingTimeInterval(100 * 86400),
                retentionDays: 7, protectedIDs: snapshot.protectedIncidentIDs))
        }
    }

    func testUnindexedFileInsideValidIncidentProtectsItFromAutomaticDeletion() throws {
        try withLibrary { ring, incidents in
            let event = IncidentManifest(at: now, reason: "Saved")
            let folder = incidents.appendingPathComponent(event.id.uuidString)
            try write(event, to: folder.appendingPathComponent("manifest.json"))
            let movie = folder.appendingPathComponent("extra.mp4")
            try Data("extra".utf8).write(to: movie)
            let snapshot = try RecordingLibraryRecovery.load(ringDirectory: ring, incidentsDirectory: incidents)
            XCTAssertEqual(snapshot.unindexed.map(\.url), [movie])
            XCTAssertEqual(snapshot.protectedIncidentIDs, [event.id])
        }
    }

    func testMismatchedNamesAndManifestIDsAreNeverTrustedOrRewritten() throws {
        try withLibrary { ring, incidents in
            try write(clip("different.mp4"), to: ring.appendingPathComponent("test.mp4.json"))
            try Data("movie".utf8).write(to: ring.appendingPathComponent("test.mp4"))
            let event = IncidentManifest(at: now, reason: "Saved")
            try write(event, to: incidents.appendingPathComponent(UUID().uuidString).appendingPathComponent("manifest.json"))
            let snapshot = try RecordingLibraryRecovery.load(ringDirectory: ring, incidentsDirectory: incidents)
            XCTAssertTrue(snapshot.incidents.isEmpty)
            XCTAssertTrue(snapshot.ring.isEmpty)
            XCTAssertTrue(snapshot.preserveRing)
            XCTAssertEqual(snapshot.warnings.count, 2)
        }
    }

    func testInvalidSegmentsCannotAuthorizeCleanupOrPathTraversal() {
        for filename in ["../other.mp4", "sub/file.mp4", "sub\\file.mp4", "/root.mp4", "file\n.mp4", "manifest.json", ""] {
            let segment = clip(filename)
            XCTAssertFalse(segment.valid, filename)
            XCTAssertFalse(RetentionPolicy.shouldDeleteFromRing(segment, now: now.addingTimeInterval(10000)))
            XCTAssertFalse(RetentionPolicy.overlaps(segment, IncidentManifest(at: now, reason: "Test")))
        }
        XCTAssertFalse(VideoSegment(filename: "test.mp4", start: now, end: now.addingTimeInterval(-1)).valid)
        XCTAssertFalse(VideoSegment(filename: "test.mp4", start: Date(timeIntervalSince1970: .nan), end: now).valid)
        var event = IncidentManifest(at: now, reason: "Test")
        event.segments = [clip(), clip()]
        XCTAssertFalse(event.valid)
        XCTAssertFalse(RecordingStoragePolicy.shouldDelete(event, now: now.addingTimeInterval(100 * 86400),
            retentionDays: 7, protectedIDs: []))
    }

    func testSymlinkMoviesAreNotPublishedAsRecoverableMedia() throws {
        try withLibrary { ring, incidents in
            let target = incidents.appendingPathComponent("outside.txt")
            try Data("not video".utf8).write(to: target)
            let link = ring.appendingPathComponent("link.mp4")
            try FileManager.default.createSymbolicLink(at: link, withDestinationURL: target)
            try write(clip("link.mp4"), to: link.appendingPathExtension("json"))
            let snapshot = try RecordingLibraryRecovery.load(ringDirectory: ring, incidentsDirectory: incidents)
            XCTAssertTrue(snapshot.ring.isEmpty)
            XCTAssertTrue(snapshot.unindexed.isEmpty)
            XCTAssertEqual(try Data(contentsOf: target), Data("not video".utf8))
        }
    }
}
