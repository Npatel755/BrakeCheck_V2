import Foundation

/// Commands retain their serial order, while slow encoder/filesystem work can
/// retain only a bounded number of camera samples. Used from the capture queue.
public final class BoundedRecordingQueue {
    public let queue: DispatchQueue
    private let lock = NSLock()
    private let maximumFrames: Int
    private var retainedFrames = 0

    public init(maximumFrames: Int = 2) {
        precondition(maximumFrames > 0)
        self.maximumFrames = maximumFrames
        queue = DispatchQueue(label: "BrakeCheck.recording", qos: .userInitiated,
                              autoreleaseFrequency: .workItem)
    }

    /// The submitted closure owns its sample until execution finishes. A
    /// rejected closure is released immediately, without entering the queue.
    @discardableResult
    public func submitFrame(_ operation: @escaping () -> Void) -> Bool {
        lock.lock()
        guard retainedFrames < maximumFrames else { lock.unlock(); return false }
        retainedFrames += 1
        lock.unlock()
        queue.async {
            defer {
                self.lock.lock(); self.retainedFrames -= 1; self.lock.unlock()
            }
            operation()
        }
        return true
    }

    /// Lifecycle/orientation/event commands are never discarded under load.
    public func submitCommand(_ operation: @escaping () -> Void) {
        queue.async(execute: operation)
    }
}
