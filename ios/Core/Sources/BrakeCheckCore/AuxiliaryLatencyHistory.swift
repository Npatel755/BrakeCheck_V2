import Foundation

/// Core ML's initial predictions include accelerator setup. Do not permanently
/// suspend a model based on its cold start rather than its steady-state cost.
public struct AuxiliaryLatencyHistory {
    private var observations = 0
    private var measuredAt: TimeInterval?
    public private(set) var seconds: TimeInterval = 0
    public init() {}
    public func seconds(at time: TimeInterval) -> TimeInterval {
        guard let measuredAt, time.isFinite, time >= measuredAt, time - measuredAt < 5 else { return 0 }
        return seconds
    }
    public mutating func observe(_ duration: TimeInterval, at time: TimeInterval = ProcessInfo.processInfo.systemUptime) {
        guard duration.isFinite, duration > 0, time.isFinite else { return }
        if let measuredAt, time - measuredAt >= 5 { seconds = 0 }
        measuredAt = time
        observations += 1
        guard observations > 2 else { return }
        seconds = seconds == 0 ? duration : max(duration, seconds * 0.8 + duration * 0.2)
    }
}
