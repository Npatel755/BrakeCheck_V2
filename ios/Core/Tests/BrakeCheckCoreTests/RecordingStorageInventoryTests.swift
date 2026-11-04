import Foundation
import XCTest
@testable import BrakeCheckCore

final class RecordingStorageInventoryTests: XCTestCase {
    private func root() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }
    func testMissingRootDoesNotLookLikeAnEmptyLibrary() {
        let missing = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        XCTAssertNil(RecordingStorageInventory.bytes(in: [missing]))
    }
    func testEmptyLibraryHasKnownZeroUsage() throws {
        let root = try root(); defer { try? FileManager.default.removeItem(at: root) }
        XCTAssertEqual(RecordingStorageInventory.bytes(in: [root]), 0)
    }
    func testNestedMoviesCountButMetadataAndDirectoriesDoNot() throws {
        let root = try root(); defer { try? FileManager.default.removeItem(at: root) }
        let nested = root.appendingPathComponent("incident")
        try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
        try Data(repeating: 0, count: 100).write(to: root.appendingPathComponent("one.mp4"))
        try Data(repeating: 0, count: 80).write(to: nested.appendingPathComponent("two.MP4"))
        try Data(repeating: 0, count: 300).write(to: nested.appendingPathComponent("manifest.json"))
        try FileManager.default.createDirectory(at: root.appendingPathComponent("directory.mp4"), withIntermediateDirectories: true)
        XCTAssertEqual(RecordingStorageInventory.bytes(in: [root]), 180)
    }
    func testHardLinkedIncidentAndRingCopyOccupySpaceOnce() throws {
        let root = try root(); defer { try? FileManager.default.removeItem(at: root) }
        let ring = root.appendingPathComponent("ring"), incidents = root.appendingPathComponent("incidents")
        for folder in [ring, incidents] { try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true) }
        let source = ring.appendingPathComponent("movie.mp4"), copy = incidents.appendingPathComponent("movie.mp4")
        try Data(repeating: 0, count: 128).write(to: source)
        try FileManager.default.linkItem(at: source, to: copy)
        XCTAssertEqual(RecordingStorageInventory.bytes(in: [ring, incidents]), 128)
    }
    func testOverlappingRootsDoNotDoubleCountMovieFiles() throws {
        let root = try root(); defer { try? FileManager.default.removeItem(at: root) }
        let nested = root.appendingPathComponent("nested")
        try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
        try Data(repeating: 0, count: 42).write(to: nested.appendingPathComponent("clip.mp4"))
        XCTAssertEqual(RecordingStorageInventory.bytes(in: [root, nested]), 42)
    }
    func testPartiallyMissingInventoryFailsClosedInsteadOfUndercounting() throws {
        let root = try root(); defer { try? FileManager.default.removeItem(at: root) }
        try Data(repeating: 0, count: 42).write(to: root.appendingPathComponent("clip.mp4"))
        XCTAssertNil(RecordingStorageInventory.bytes(in: [root, root.appendingPathComponent("missing")]))
    }
    func testFileCannotStandInForALibraryFolder() throws {
        let root = try root(); defer { try? FileManager.default.removeItem(at: root) }
        let file = root.appendingPathComponent("not-a-folder")
        try Data("preserve".utf8).write(to: file)
        XCTAssertNil(RecordingStorageInventory.bytes(in: [file]))
        XCTAssertEqual(try Data(contentsOf: file), Data("preserve".utf8))
    }
    func testSymbolicMovieLinkCannotCountOutsideLibrary() throws {
        let root = try root(); defer { try? FileManager.default.removeItem(at: root) }
        let library = root.appendingPathComponent("library")
        try FileManager.default.createDirectory(at: library, withIntermediateDirectories: true)
        let outside = root.appendingPathComponent("outside.mp4")
        try Data(repeating: 0, count: 500).write(to: outside)
        try FileManager.default.createSymbolicLink(at: library.appendingPathComponent("linked.mp4"), withDestinationURL: outside)
        XCTAssertEqual(RecordingStorageInventory.bytes(in: [library]), 0)
    }
}
