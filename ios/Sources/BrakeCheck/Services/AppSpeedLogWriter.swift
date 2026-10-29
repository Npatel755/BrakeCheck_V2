import BrakeCheckCore
import Foundation

/// GPS callbacks only admit bounded work. File creation, CSV formatting and disk
/// writes run on Core's private I/O queue, including the final trip flush.
final class AppSpeedLogWriter {
    private let writer = QueuedSpeedLogWriter(directory: AppSpeedLogWriter.directory)
    var url: URL? { writer.url }
    var lastError: String? { writer.lastError }
    var droppedSamples: Int { writer.droppedSamples }
    var pendingSamples: Int { writer.pendingSamples }

    static var directory: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("SpeedLogs", isDirectory: true)
    }

    func start(at date: Date = Date()) {
        writer.start(at: date)
    }

    func append(speed: Double, accuracy: Double, at date: Date) {
        writer.append(speed: speed, accuracy: accuracy, at: date)
    }

    /// Completion returns to the main queue after accepted samples are flushed
    /// and closed, before the caller presents a trip summary or replay picker.
    func stop(completion: @escaping () -> Void = {}) {
        writer.stop { DispatchQueue.main.async(execute: completion) }
    }
}
