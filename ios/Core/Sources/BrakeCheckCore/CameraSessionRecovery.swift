import Foundation

/// Capture intent outlives a transient camera reset; paused/stopped trips never
/// restart from a queued retry. State is owned by the serial capture queue.
public struct CameraSessionRecovery {
    public struct Retry: Equatable {
        public let id: UUID
        public let at: TimeInterval
    }
    public private(set) var wantsRunning = false
    public private(set) var interrupted = false
    public private(set) var pendingRetry: Retry?
    private var awaitingRestart = true
    private var attempts = 0
    private var healthySince: TimeInterval?
    private var startedAt: TimeInterval?
    private var lastFrame: TimeInterval?
    public init() {}

    public var acceptsFrames: Bool { wantsRunning && !interrupted && !awaitingRestart }

    public mutating func start() {
        wantsRunning = true; interrupted = false; pendingRetry = nil
        awaitingRestart = true; attempts = 0; healthySince = nil; startedAt = nil; lastFrame = nil
    }
    public mutating func stop() {
        wantsRunning = false; interrupted = false; pendingRetry = nil
        awaitingRestart = true; healthySince = nil; startedAt = nil; lastFrame = nil
    }
    public mutating func beginInterruption() {
        interrupted = true; pendingRetry = nil; awaitingRestart = true; healthySince = nil
    }
    public mutating func endInterruption() -> Bool {
        interrupted = false
        return wantsRunning
    }
    public mutating func sessionStarted(at time: TimeInterval) {
        guard wantsRunning, !interrupted, pendingRetry == nil, time.isFinite, time >= 0 else { return }
        awaitingRestart = false
        startedAt = time; lastFrame = nil; healthySince = nil
    }

    public mutating func failed(at time: TimeInterval) -> Retry? {
        awaitingRestart = true; healthySince = nil
        guard wantsRunning, !interrupted, time.isFinite, time >= 0 else { return nil }
        if let pendingRetry { return pendingRetry }
        guard attempts < 3 else { return nil }
        let retry = Retry(id: UUID(), at: time + [0.5, 1.0, 2.0][attempts])
        attempts += 1; pendingRetry = retry
        return retry
    }
    public mutating func take(_ retry: Retry, at time: TimeInterval) -> Bool {
        guard wantsRunning, !interrupted, pendingRetry == retry,
              time.isFinite, time >= retry.at else { return false }
        pendingRetry = nil
        return true
    }

    public mutating func frameReceived(at time: TimeInterval) {
        guard acceptsFrames, time.isFinite, let startedAt, time >= startedAt,
              lastFrame.map({ time > $0 }) ?? true else { return }
        if healthySince == nil || lastFrame.map({ time - $0 > 1 }) == true { healthySince = time }
        lastFrame = time
        // A single frame between repeated failures must not create an endless retry loop.
        if time - (healthySince ?? time) >= 5 { attempts = 0 }
    }

    public func needsRestart(at time: TimeInterval) -> Bool {
        guard acceptsFrames, time.isFinite, let startedAt, time >= startedAt else { return false }
        if let lastFrame { return time - lastFrame > 3 }
        return time - startedAt > 5
    }
}
