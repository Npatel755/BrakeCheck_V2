import Foundation

/// Keeps work on its intended time grid when camera offers arrive with jitter.
/// A missed slot is skipped, never replayed. The half-interval spacing floor
/// prevents a late offer from producing an immediate catch-up burst.
public struct FrameCadence {
    private var nextDeadline: TimeInterval?
    private var lastAccepted: TimeInterval?
    private var configuredInterval: TimeInterval?

    public init() {}
    public mutating func reset() {
        nextDeadline = nil; lastAccepted = nil; configuredInterval = nil
    }

    public mutating func shouldRun(at time: TimeInterval, interval: TimeInterval) -> Bool {
        guard time.isFinite, interval.isFinite, interval > 0 else { return false }
        if let lastAccepted, time < lastAccepted { return false }
        if configuredInterval != interval {
            configuredInterval = interval
            nextDeadline = lastAccepted.map { $0 + interval }
        }
        let deadline = nextDeadline ?? time
        let tolerance = min(0.001, interval * 0.01)
        guard time + tolerance >= deadline,
              lastAccepted.map({ time - $0 >= interval * 0.5 - 1e-9 }) ?? true else { return false }
        let skipped = floor(max(0, time - deadline) / interval)
        nextDeadline = max(deadline + (skipped + 1) * interval, time + interval * 0.5)
        lastAccepted = time
        return true
    }
}
