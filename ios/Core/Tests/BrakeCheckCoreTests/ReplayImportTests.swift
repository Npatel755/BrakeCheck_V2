import Foundation
import XCTest
@testable import BrakeCheckCore

final class ReplayImportTests: XCTestCase {
    private func withFolder(_ work: (URL) throws -> Void) throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        try work(root)
    }

    func testIdenticalNamesKeepBothImportsAndOriginals() throws {
        try withFolder { root in
            let first = root.appendingPathComponent("first/drive.mp4")
            let second = root.appendingPathComponent("second/drive.mp4")
            for url in [first, second] {
                try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            }
            try Data("first clip".utf8).write(to: first)
            try Data("second clip".utf8).write(to: second)
            let destination = root.appendingPathComponent("imports")
            let copiedFirst = try ReplayImportStore.copy(first, to: destination)
            let copiedSecond = try ReplayImportStore.copy(second, to: destination)
            XCTAssertNotEqual(copiedFirst, copiedSecond)
            XCTAssertEqual(copiedFirst.lastPathComponent, "drive.mp4")
            XCTAssertEqual(try Data(contentsOf: copiedFirst), try Data(contentsOf: first))
            XCTAssertEqual(try Data(contentsOf: copiedSecond), try Data(contentsOf: second))
        }
    }

    func testFailedImportPreservesExistingClipAndDoesNotCreateAnotherFolder() throws {
        try withFolder { root in
            let source = root.appendingPathComponent("drive.mp4")
            try Data("original".utf8).write(to: source)
            let destination = root.appendingPathComponent("imports")
            let clip = try ReplayImportStore.copy(source, to: destination)
            try FileManager.default.removeItem(at: source)
            XCTAssertThrowsError(try ReplayImportStore.copy(source, to: destination))
            XCTAssertEqual(try Data(contentsOf: clip), Data("original".utf8))
            XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: destination.path).count, 1)
            XCTAssertThrowsError(try ReplayImportStore.copy(root, to: destination))
            XCTAssertEqual(try Data(contentsOf: clip), Data("original".utf8))
        }
    }
}
