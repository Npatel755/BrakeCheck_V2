import Foundation
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

/// The writer calls its factory and every sink operation on its private I/O queue.
/// `close` must flush pending data before returning, and may report a disk error.
public protocol SpeedLogSink: AnyObject {
    func write(_ data: Data) throws
    func close() throws
}

/// Ordered CSV persistence that never waits for filesystem work on a GPS callback.
/// Admission retains at most `maxPendingSamples` samples, including the sample
/// currently being written. Saturation drops a sample rather than delaying GPS.
public final class QueuedSpeedLogWriter {
    public typealias SinkFactory = (URL) throws -> SpeedLogSink

    private final class Session {
        let startedAt: Date
        // These three properties are protected by admissionLock.
        var lastAcceptedTime: TimeInterval?
        var publishedURL: URL?
        var failed = false
        // Only the private serial I/O queue accesses the sink.
        var sink: SpeedLogSink?

        init(startedAt: Date) { self.startedAt = startedAt }
    }

    public let directory: URL
    public let maxPendingSamples: Int
    private let admissionLock = NSLock()
    private let ioQueue = DispatchQueue(label: "BrakeCheck.speedLog", qos: .utility,
                                        autoreleaseFrequency: .workItem)
    private let sinkFactory: SinkFactory
    private var activeSession: Session?
    private var pendingCount = 0
    private var droppedCount = 0
    private var errorMessage: String?

    /// The active file is published only after its header was written. A queued
    /// stop or replacement start clears it immediately, before older work closes.
    public var url: URL? {
        admissionLock.lock(); defer { admissionLock.unlock() }
        return activeSession?.publishedURL
    }

    public var pendingSamples: Int {
        admissionLock.lock(); defer { admissionLock.unlock() }
        return pendingCount
    }

    /// Lifetime total of invalid, saturated, or failed samples. Normal 5 Hz
    /// decimation and callbacks outside a trip do not count as dropped samples.
    public var droppedSamples: Int {
        admissionLock.lock(); defer { admissionLock.unlock() }
        return droppedCount
    }

    /// The latest persistence or invalid-start error; retained across new trips
    /// so a quick restart cannot conceal an earlier disk failure.
    public var lastError: String? {
        admissionLock.lock(); defer { admissionLock.unlock() }
        return errorMessage
    }

    public init(directory: URL, maxPendingSamples: Int = 64,
                sinkFactory: SinkFactory? = nil) {
        precondition(maxPendingSamples > 0)
        self.directory = directory
        self.maxPendingSamples = maxPendingSamples
        self.sinkFactory = sinkFactory ?? { try ExclusiveSpeedLogFile(url: $0) }
    }

    /// Ends the preceding session after its accepted rows, then creates a new
    /// file on the worker. Even same-second starts cannot overwrite a trip file.
    public func start(at date: Date = Date()) {
        admissionLock.lock()
        let previous = activeSession
        let timestamp = date.timeIntervalSince1970
        let next = timestamp.isFinite && timestamp >= 0 ? Session(startedAt: date) : nil
        activeSession = next
        if next == nil { errorMessage = "Cannot start speed log with an invalid timestamp." }
        // Enqueue while holding the admission lock: callers on different queues
        // cannot reorder a lifecycle command around an admitted sample.
        ioQueue.async {
            self.close(previous)
            if let next { self.open(next) }
        }
        admissionLock.unlock()
    }

    /// Accepts finite nonnegative speed/time and nonnegative accuracy, or the
    /// existing -1 accuracy sentinel for an unknown GPS speed uncertainty.
    @discardableResult
    public func append(speed: Double, accuracy: Double, at date: Date) -> Bool {
        let timestamp = date.timeIntervalSince1970
        admissionLock.lock()
        guard let session = activeSession else { admissionLock.unlock(); return false }
        guard speed.isFinite, speed >= 0, timestamp.isFinite, timestamp >= 0,
              accuracy.isFinite, accuracy >= 0 || accuracy == -1 else {
            droppedCount += 1
            admissionLock.unlock()
            return false
        }
        // Date's floating representation can make a nominal 0.2 s delta a few
        // tenths of a microsecond short at contemporary Unix timestamps.
        if let previous = session.lastAcceptedTime, timestamp - previous < 0.2 - 0.000001 {
            admissionLock.unlock()
            return false
        }
        guard !session.failed, pendingCount < maxPendingSamples else {
            droppedCount += 1
            admissionLock.unlock()
            return false
        }
        session.lastAcceptedTime = timestamp
        pendingCount += 1
        ioQueue.async {
            defer {
                self.admissionLock.lock()
                self.pendingCount -= 1
                self.admissionLock.unlock()
            }
            guard let sink = session.sink else { self.recordDroppedSample(); return }
            let line = String(format: "%.3f,%.3f,%.3f\n", locale: Locale(identifier: "en_US_POSIX"),
                              timestamp, speed, accuracy)
            do {
                try sink.write(Data(line.utf8))
            } catch {
                self.recordDroppedSample()
                self.record(error: error, for: session)
                self.close(session)
            }
        }
        admissionLock.unlock()
        return true
    }

    /// Stops admission immediately. Completion runs on the serial I/O queue only
    /// after all previously accepted rows have completed and the file has closed.
    /// It also runs after a failed or inactive trip; inspect `lastError` for errors.
    public func stop(completion: @escaping () -> Void = {}) {
        admissionLock.lock()
        let previous = activeSession
        activeSession = nil
        ioQueue.async {
            self.close(previous)
            completion()
        }
        admissionLock.unlock()
    }

    private func open(_ session: Session) {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd_HH-mm-ss"
        let name = "trip_\(formatter.string(from: session.startedAt))_\(UUID().uuidString).csv"
        let fileURL = directory.appendingPathComponent(name)
        do {
            let sink = try sinkFactory(fileURL)
            session.sink = sink
            try sink.write(Data("unix_time,speed_mps,accuracy_mps\n".utf8))
            admissionLock.lock()
            session.publishedURL = fileURL
            admissionLock.unlock()
        } catch {
            record(error: error, for: session)
            close(session)
        }
    }

    private func close(_ session: Session?) {
        guard let session, let sink = session.sink else { return }
        session.sink = nil
        do { try sink.close() }
        catch { record(error: error, for: session) }
    }

    private func record(error: Error, for session: Session) {
        let message = error.localizedDescription
        admissionLock.lock()
        session.failed = true
        session.publishedURL = nil
        errorMessage = message
        admissionLock.unlock()
    }

    private func recordDroppedSample() {
        admissionLock.lock(); droppedCount += 1; admissionLock.unlock()
    }

    deinit {
        // Queued commands retain the writer until they finish. An idle active
        // file may remain when its last caller releases it on the main thread;
        // transfer that final close to the same I/O queue without waiting on it.
        guard let session = activeSession else { return }
        ioQueue.async {
            let sink = session.sink
            session.sink = nil
            try? sink?.close()
        }
    }
}

private final class ExclusiveSpeedLogFile: SpeedLogSink {
    private var handle: FileHandle?

    init(url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        // Exclusive creation guarantees that an existing trip is never truncated,
        // even if a filename collision or another process creates it first.
        let descriptor = url.path.withCString { open($0, O_WRONLY | O_CREAT | O_EXCL, mode_t(0o600)) }
        guard descriptor >= 0 else {
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno),
                          userInfo: [NSFilePathErrorKey: url.path])
        }
        handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
    }

    func write(_ data: Data) throws {
        guard let handle else { throw CocoaError(.fileWriteUnknown) }
        try handle.write(contentsOf: data)
    }

    func close() throws {
        guard let handle else { return }
        self.handle = nil
        // Always attempt close after a flush failure, without concealing it.
        var flushError: Error?
        do { try handle.synchronize() } catch { flushError = error }
        do { try handle.close() } catch { if flushError == nil { flushError = error } }
        if let flushError { throw flushError }
    }
}
