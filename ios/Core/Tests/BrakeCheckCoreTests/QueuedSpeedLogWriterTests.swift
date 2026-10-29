import XCTest
@testable import BrakeCheckCore

final class QueuedSpeedLogWriterTests: XCTestCase {
    private let epoch = Date(timeIntervalSince1970: 1_800_000_000)

    private func temporaryDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("BrakeCheckSpeedLog-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }
        return directory
    }

    private func finish(_ writer: QueuedSpeedLogWriter) {
        let finished = expectation(description: "CSV closed before completion")
        writer.stop { finished.fulfill() }
        wait(for: [finished], timeout: 5)
    }

    private func logs(in directory: URL) throws -> [String] {
        try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "csv" }
            .map { try String(contentsOf: $0, encoding: .utf8) }
    }

    func testRealFileHeaderCanonicalDecimalsAndAcceptedRowOrder() throws {
        let directory = try temporaryDirectory()
        let writer = QueuedSpeedLogWriter(directory: directory)
        writer.start(at: epoch)
        XCTAssertTrue(writer.append(speed: 0, accuracy: -1, at: epoch))
        XCTAssertTrue(writer.append(speed: 3.125, accuracy: 0.25, at: epoch.addingTimeInterval(0.2)))
        XCTAssertTrue(writer.append(speed: 5.25, accuracy: 1.125, at: epoch.addingTimeInterval(0.4)))
        finish(writer)

        XCTAssertEqual(try logs(in: directory), [
            "unix_time,speed_mps,accuracy_mps\n"
                + "1800000000.000,0.000,-1.000\n"
                + "1800000000.200,3.125,0.250\n"
                + "1800000000.400,5.250,1.125\n"
        ])
        XCTAssertEqual(writer.pendingSamples, 0)
        XCTAssertEqual(writer.droppedSamples, 0)
        XCTAssertNil(writer.lastError)
        XCTAssertNil(writer.url)
    }

    func testCadenceAcceptsNominalPointTwoButRejectsDuplicatesAndBackwardFixes() throws {
        let directory = try temporaryDirectory()
        let writer = QueuedSpeedLogWriter(directory: directory)
        writer.start(at: epoch)
        XCTAssertTrue(writer.append(speed: 1, accuracy: 1, at: epoch))
        XCTAssertFalse(writer.append(speed: 99, accuracy: 1, at: epoch))
        XCTAssertFalse(writer.append(speed: 98, accuracy: 1, at: epoch.addingTimeInterval(-1)))
        XCTAssertFalse(writer.append(speed: 97, accuracy: 1, at: epoch.addingTimeInterval(0.199)))
        XCTAssertTrue(writer.append(speed: 2, accuracy: 1, at: epoch.addingTimeInterval(0.2)))
        XCTAssertTrue(writer.append(speed: 3, accuracy: 1, at: epoch.addingTimeInterval(0.4)))
        finish(writer)

        let text = try XCTUnwrap(logs(in: directory).first)
        XCTAssertEqual(text.split(separator: "\n").count, 4)
        XCTAssertFalse(text.contains("97.000"))
        XCTAssertFalse(text.contains("98.000"))
        XCTAssertFalse(text.contains("99.000"))
        XCTAssertEqual(writer.droppedSamples, 0, "Normal cadence filtering is not a disk backlog drop")
    }

    func testInvalidSamplesNeverEnterCSVOrAdvanceTheCadence() throws {
        let directory = try temporaryDirectory()
        let writer = QueuedSpeedLogWriter(directory: directory)
        writer.start(at: epoch)
        let invalid: [(Double, Double, Date)] = [
            (.nan, 1, epoch), (.infinity, 1, epoch), (-1, 1, epoch),
            (1, .nan, epoch), (1, .infinity, epoch), (1, -2, epoch),
            (1, 1, Date(timeIntervalSince1970: .nan)),
            (1, 1, Date(timeIntervalSince1970: -1))
        ]
        for (speed, accuracy, date) in invalid {
            XCTAssertFalse(writer.append(speed: speed, accuracy: accuracy, at: date))
        }
        XCTAssertTrue(writer.append(speed: 2, accuracy: -1, at: epoch))
        finish(writer)
        XCTAssertEqual(writer.droppedSamples, invalid.count)
        let text = try XCTUnwrap(logs(in: directory).first)
        XCTAssertEqual(text, "unix_time,speed_mps,accuracy_mps\n1800000000.000,2.000,-1.000\n")
    }

    func testRapidSameSecondRestartsAreDistinctAndCannotMixTripsOrOverwriteExistingFile() throws {
        let directory = try temporaryDirectory()
        let existing = directory.appendingPathComponent("trip_2027-01-15_08-00-00.csv")
        try Data("preserved\n".utf8).write(to: existing)
        let writer = QueuedSpeedLogWriter(directory: directory)
        for trip in 1...12 {
            writer.start(at: epoch)
            XCTAssertTrue(writer.append(speed: Double(trip), accuracy: 1, at: epoch))
            XCTAssertTrue(writer.append(speed: Double(trip), accuracy: 1, at: epoch.addingTimeInterval(0.2)))
        }
        finish(writer)

        XCTAssertEqual(try String(contentsOf: existing, encoding: .utf8), "preserved\n")
        let files = try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
        XCTAssertEqual(files.count, 13)
        XCTAssertEqual(Set(files.map(\.lastPathComponent)).count, 13)
        var observedSpeeds: Set<Int> = []
        for file in files where file != existing {
            let rows = try String(contentsOf: file, encoding: .utf8).split(separator: "\n")
            XCTAssertEqual(rows.count, 3)
            XCTAssertEqual(rows.first, "unix_time,speed_mps,accuracy_mps")
            let speeds = rows.dropFirst().map { $0.split(separator: ",")[1] }
            XCTAssertEqual(speeds[0], speeds[1], "A trip file must not contain another trip's rows")
            observedSpeeds.insert(Int(Double(speeds[0])!))
        }
        XCTAssertEqual(observedSpeeds, Set(1...12))
        XCTAssertEqual(writer.droppedSamples, 0)
    }

    func testSlowFactoryDoesNotBlockCallerAndBacklogIsBoundedIncludingExecutingSample() throws {
        let directory = try temporaryDirectory()
        let entered = expectation(description: "I/O factory blocked")
        let admitted = expectation(description: "GPS callbacks finished while I/O stayed blocked")
        let closed = expectation(description: "Stop command cannot be dropped behind full backlog")
        let release = DispatchSemaphore(value: 0)
        let probe = SpeedLogProbe()
        let writer = QueuedSpeedLogWriter(directory: directory, maxPendingSamples: 3) { url in
            entered.fulfill()
            guard release.wait(timeout: .now() + 5) == .success else { throw SpeedLogTestError.timeout }
            return try ProbedSpeedLogFile(url: url, probe: probe)
        }
        defer { release.signal() }
        writer.start(at: epoch)
        wait(for: [entered], timeout: 5)
        DispatchQueue.global().async {
            for index in 0..<3 {
                XCTAssertTrue(writer.append(speed: Double(index), accuracy: 1,
                                             at: self.epoch.addingTimeInterval(Double(index))))
            }
            XCTAssertFalse(writer.append(speed: 8, accuracy: 1, at: self.epoch.addingTimeInterval(8)))
            XCTAssertFalse(writer.append(speed: 9, accuracy: 1, at: self.epoch.addingTimeInterval(9)))
            XCTAssertEqual(writer.pendingSamples, 3)
            writer.stop { closed.fulfill() }
            XCTAssertFalse(writer.append(speed: 10, accuracy: 1, at: self.epoch.addingTimeInterval(10)))
            admitted.fulfill()
        }
        wait(for: [admitted], timeout: 2)
        XCTAssertEqual(writer.droppedSamples, 2)
        release.signal()
        wait(for: [closed], timeout: 5)
        XCTAssertEqual(writer.pendingSamples, 0)
        XCTAssertEqual(try logs(in: directory).first?.split(separator: "\n").count, 4)
        XCTAssertEqual(probe.snapshot().filter { $0 == "close" }.count, 1)
    }

    func testExecutingWriteCountsTowardBoundAndRecoveryKeepsOriginalMeasurementTimes() throws {
        let directory = try temporaryDirectory()
        let writing = expectation(description: "First data write blocked")
        let release = DispatchSemaphore(value: 0)
        let probe = SpeedLogProbe()
        let writer = QueuedSpeedLogWriter(directory: directory, maxPendingSamples: 1) { url in
            try ProbedSpeedLogFile(url: url, probe: probe, beforeData: {
                writing.fulfill()
                guard release.wait(timeout: .now() + 5) == .success else { throw SpeedLogTestError.timeout }
            })
        }
        defer { release.signal() }
        writer.start(at: epoch)
        XCTAssertTrue(writer.append(speed: 2, accuracy: 1, at: epoch))
        wait(for: [writing], timeout: 5)
        XCTAssertEqual(writer.pendingSamples, 1)
        XCTAssertFalse(writer.append(speed: 3, accuracy: 1, at: epoch.addingTimeInterval(1)))
        writer.stop()
        let reset = expectation(description: "Prior write closed before new trip opened")
        writer.stop { reset.fulfill() }
        release.signal()
        wait(for: [reset], timeout: 5)
        XCTAssertEqual(writer.pendingSamples, 0)
        XCTAssertEqual(writer.droppedSamples, 1)
        let text = try XCTUnwrap(logs(in: directory).first)
        XCTAssertTrue(text.contains("1800000000.000,2.000,1.000\n"))
        XCTAssertFalse(text.contains("1800000001.000"), "Dropped rows cannot be backfilled with guessed speed")
    }

    func testStopCompletionsAreFIFOAndRunOnlyAfterTheirOwnFilesClose() throws {
        let directory = try temporaryDirectory()
        let probe = SpeedLogProbe()
        let done = expectation(description: "Both ordered stop completions")
        done.expectedFulfillmentCount = 2
        let writer = QueuedSpeedLogWriter(directory: directory) { url in
            try ProbedSpeedLogFile(url: url, probe: probe)
        }
        writer.start(at: epoch)
        writer.append(speed: 1, accuracy: 1, at: epoch)
        writer.stop {
            XCTAssertEqual(probe.snapshot().last, "close")
            XCTAssertFalse(Thread.isMainThread)
            probe.record("completion1")
            done.fulfill()
        }
        writer.start(at: epoch)
        writer.append(speed: 2, accuracy: 1, at: epoch)
        writer.stop {
            XCTAssertEqual(probe.snapshot().last, "close")
            probe.record("completion2")
            done.fulfill()
        }
        wait(for: [done], timeout: 5)
        XCTAssertEqual(probe.snapshot(), ["factory", "header", "row", "close", "completion1",
                                         "factory", "header", "row", "close", "completion2"])
        XCTAssertFalse(probe.didPerformIOOnMain)
    }

    func testFactoryFailureReportsErrorDropsAcceptedRowsAndNextTripCanRecover() throws {
        let directory = try temporaryDirectory()
        let entered = expectation(description: "First factory entered")
        let release = DispatchSemaphore(value: 0)
        let probe = SpeedLogProbe()
        var calls = 0 // Factory is confined to the serial I/O queue.
        let writer = QueuedSpeedLogWriter(directory: directory) { url in
            calls += 1
            if calls == 1 {
                entered.fulfill()
                guard release.wait(timeout: .now() + 5) == .success else { throw SpeedLogTestError.timeout }
                throw SpeedLogTestError.factory
            }
            return try ProbedSpeedLogFile(url: url, probe: probe)
        }
        defer { release.signal() }
        writer.start(at: epoch)
        wait(for: [entered], timeout: 5)
        XCTAssertTrue(writer.append(speed: 1, accuracy: 1, at: epoch))
        let failed = expectation(description: "Failed start still completes stop")
        writer.stop { failed.fulfill() }
        release.signal()
        wait(for: [failed], timeout: 5)
        XCTAssertEqual(writer.lastError, "Factory unavailable")
        XCTAssertEqual(writer.droppedSamples, 1)
        XCTAssertNil(writer.url)
        writer.start(at: epoch)
        XCTAssertTrue(writer.append(speed: 2, accuracy: 1, at: epoch))
        finish(writer)
        XCTAssertEqual(try logs(in: directory).count, 1)
        XCTAssertEqual(writer.pendingSamples, 0)
        XCTAssertEqual(writer.lastError, "Factory unavailable", "A restart cannot hide the previous disk failure")
    }

    func testRealDirectoryErrorIsReportedAndDoesNotPreventCompletion() throws {
        let directory = try temporaryDirectory()
        let notDirectory = directory.appendingPathComponent("occupied")
        try Data("keep".utf8).write(to: notDirectory)
        let writer = QueuedSpeedLogWriter(directory: notDirectory)
        writer.start(at: epoch)
        writer.append(speed: 1, accuracy: 1, at: epoch)
        finish(writer)
        XCTAssertNotNil(writer.lastError)
        XCTAssertNil(writer.url)
        XCTAssertEqual(writer.droppedSamples, 1)
        XCTAssertEqual(try String(contentsOf: notDirectory, encoding: .utf8), "keep")
    }

    func testRowFailureClosesFileOnceAndDoesNotPretendPendingRowsWereWritten() throws {
        let directory = try temporaryDirectory()
        let entered = expectation(description: "First row entered")
        let release = DispatchSemaphore(value: 0)
        let probe = SpeedLogProbe()
        let writer = QueuedSpeedLogWriter(directory: directory) { url in
            try ProbedSpeedLogFile(url: url, probe: probe, beforeData: {
                entered.fulfill()
                guard release.wait(timeout: .now() + 5) == .success else { throw SpeedLogTestError.timeout }
                throw SpeedLogTestError.write
            })
        }
        defer { release.signal() }
        writer.start(at: epoch)
        writer.append(speed: 1, accuracy: 1, at: epoch)
        wait(for: [entered], timeout: 5)
        writer.append(speed: 2, accuracy: 1, at: epoch.addingTimeInterval(1))
        let stopped = expectation(description: "Close after row error")
        writer.stop { stopped.fulfill() }
        release.signal()
        wait(for: [stopped], timeout: 5)
        XCTAssertEqual(writer.lastError, "Write unavailable")
        XCTAssertEqual(writer.droppedSamples, 2)
        XCTAssertEqual(writer.pendingSamples, 0)
        XCTAssertEqual(probe.snapshot().filter { $0 == "close" }.count, 1)
        XCTAssertEqual(try logs(in: directory), ["unix_time,speed_mps,accuracy_mps\n"])
    }

    func testCloseFailureStillCompletesAndPublishesError() throws {
        let directory = try temporaryDirectory()
        let probe = SpeedLogProbe()
        let writer = QueuedSpeedLogWriter(directory: directory) { url in
            try ProbedSpeedLogFile(url: url, probe: probe, failClose: true)
        }
        writer.start(at: epoch)
        writer.append(speed: 1, accuracy: 1, at: epoch)
        finish(writer)
        XCTAssertEqual(writer.lastError, "Close unavailable")
        XCTAssertEqual(writer.pendingSamples, 0)
        XCTAssertEqual(probe.snapshot().last, "close")
    }

    func testReleaseDuringAcceptedWriteEventuallyClosesWithoutWaitingOnCaller() throws {
        let directory = try temporaryDirectory()
        let entered = expectation(description: "Row worker blocked")
        let closed = expectation(description: "Deinit closes its outstanding trip off main")
        let release = DispatchSemaphore(value: 0)
        let probe = SpeedLogProbe()
        var writer: QueuedSpeedLogWriter? = QueuedSpeedLogWriter(directory: directory) { url in
            try ProbedSpeedLogFile(url: url, probe: probe, beforeData: {
                entered.fulfill()
                guard release.wait(timeout: .now() + 5) == .success else { throw SpeedLogTestError.timeout }
            }, afterClose: { closed.fulfill() })
        }
        defer { release.signal() }
        writer?.start(at: epoch)
        writer?.append(speed: 2, accuracy: 1, at: epoch)
        wait(for: [entered], timeout: 5)
        weak var weakWriter = writer
        writer = nil
        XCTAssertNotNil(weakWriter, "Accepted work retains the writer until it finishes")
        release.signal()
        wait(for: [closed], timeout: 5)
        XCTAssertNil(weakWriter)
        XCTAssertFalse(probe.didPerformIOOnMain)
        XCTAssertEqual(try logs(in: directory).first?.split(separator: "\n").count, 2)
    }

    func testInvalidStartEndsPriorTripAndInactiveStopStillCompletes() throws {
        let directory = try temporaryDirectory()
        let writer = QueuedSpeedLogWriter(directory: directory)
        XCTAssertEqual(writer.maxPendingSamples, 64)
        XCTAssertFalse(writer.append(speed: 1, accuracy: 1, at: epoch))
        writer.start(at: epoch)
        writer.append(speed: 2, accuracy: 1, at: epoch)
        writer.start(at: Date(timeIntervalSince1970: -1))
        XCTAssertFalse(writer.append(speed: 3, accuracy: 1, at: epoch.addingTimeInterval(1)))
        finish(writer)
        XCTAssertEqual(try logs(in: directory).count, 1)
        XCTAssertNotNil(writer.lastError)
        XCTAssertNil(writer.url)
    }
}

private enum SpeedLogTestError: Error, LocalizedError {
    case factory, write, close, timeout
    var errorDescription: String? {
        switch self {
        case .factory: return "Factory unavailable"
        case .write: return "Write unavailable"
        case .close: return "Close unavailable"
        case .timeout: return "Timed out waiting for test gate"
        }
    }
}

private final class SpeedLogProbe {
    private let lock = NSLock()
    private var events: [String] = []
    private var mainIO = false
    func record(_ event: String, isIO: Bool = false) {
        lock.lock(); defer { lock.unlock() }
        events.append(event)
        if isIO && Thread.isMainThread { mainIO = true }
    }
    func snapshot() -> [String] { lock.lock(); defer { lock.unlock() }; return events }
    var didPerformIOOnMain: Bool { lock.lock(); defer { lock.unlock() }; return mainIO }
}

/// Actual temporary-file writes with hooks around slow/failing operations.
private final class ProbedSpeedLogFile: SpeedLogSink {
    private let handle: FileHandle
    private let probe: SpeedLogProbe
    private var beforeData: (() throws -> Void)?
    private let afterClose: (() -> Void)?
    private let failClose: Bool

    init(url: URL, probe: SpeedLogProbe, beforeData: (() throws -> Void)? = nil,
         failClose: Bool = false, afterClose: (() -> Void)? = nil) throws {
        self.probe = probe
        self.beforeData = beforeData
        self.failClose = failClose
        self.afterClose = afterClose
        probe.record("factory", isIO: true)
        guard FileManager.default.createFile(atPath: url.path, contents: nil) else {
            throw SpeedLogTestError.factory
        }
        handle = try FileHandle(forWritingTo: url)
    }

    func write(_ data: Data) throws {
        let isHeader = data.starts(with: Data("unix_time".utf8))
        if !isHeader, let beforeData {
            self.beforeData = nil
            try beforeData()
        }
        try handle.write(contentsOf: data)
        probe.record(isHeader ? "header" : "row", isIO: true)
    }

    func close() throws {
        try handle.synchronize()
        try handle.close()
        probe.record("close", isIO: true)
        afterClose?()
        if failClose { throw SpeedLogTestError.close }
    }
}
