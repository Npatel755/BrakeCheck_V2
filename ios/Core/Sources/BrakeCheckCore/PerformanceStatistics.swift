import Foundation

/// A fixed-size histogram: recording a frame never allocates a sample array or sorts.
public struct PerformanceStatistics: Codable, Sendable {
    public static let boundaries: [Double] = [0.00025, 0.0005, 0.001, 0.002, 0.004, 0.008,
        0.012, 1.0 / 60, 0.020, 0.025, 1.0 / 30, 0.050, 0.075, 0.100,
        0.150, 0.200, 0.300, 0.500, 0.750, 1, 2, 5, 10]
    public private(set) var count: UInt64 = 0
    public private(set) var totalSeconds: Double = 0
    public private(set) var maximumSeconds: Double = 0
    public private(set) var buckets = Array(repeating: UInt64(0), count: boundaries.count + 1)
    public init() {}
    public mutating func record(seconds: Double) {
        guard seconds.isFinite, seconds >= 0 else { return }
        count += 1; totalSeconds += seconds; maximumSeconds = max(maximumSeconds, seconds)
        buckets[Self.boundaries.firstIndex(where: { seconds <= $0 }) ?? Self.boundaries.count] += 1
    }
    public var meanSeconds: Double { count > 0 ? totalSeconds / Double(count) : 0 }
    /// Conservative bucket upper bound, not a falsely precise sample percentile.
    public func percentileUpperBound(_ percentile: Double) -> Double? {
        guard count > 0, percentile.isFinite, (0...1).contains(percentile) else { return nil }
        let target = max(1, UInt64(ceil(Double(count) * percentile)))
        var seen: UInt64 = 0
        for (index, value) in buckets.enumerated() {
            seen += value
            if seen >= target { return index < Self.boundaries.count ? Self.boundaries[index] : maximumSeconds }
        }
        return maximumSeconds
    }
}

/// Delivered display callbacks, using their actual target interval rather than assuming 60 Hz.
/// These are scheduling measurements; they do not prove every rendered frame reached the screen.
public struct DisplayPacingStatistics: Codable, Sendable {
    public private(set) var callbacks: UInt64 = 0
    public private(set) var delayedCallbacks: UInt64 = 0
    public private(set) var estimatedMissedRefreshes: UInt64 = 0
    public private(set) var intervals = PerformanceStatistics()
    private var previousTimestamp: Double?
    private var previousTargetInterval: Double?
    public init() {}
    public mutating func resetContinuity() { previousTimestamp = nil; previousTargetInterval = nil }
    public mutating func record(timestamp: Double, targetInterval: Double) {
        guard timestamp.isFinite, targetInterval.isFinite, targetInterval > 0, targetInterval <= 1 else { return }
        callbacks += 1
        defer { previousTimestamp = timestamp; previousTargetInterval = targetInterval }
        guard let previousTimestamp, let previousTargetInterval, timestamp > previousTimestamp,
              abs(targetInterval - previousTargetInterval) <= previousTargetInterval * 0.20 else { return }
        let elapsed = timestamp - previousTimestamp
        intervals.record(seconds: elapsed)
        if elapsed > targetInterval * 1.5 {
            delayedCallbacks += 1
            estimatedMissedRefreshes += UInt64(max(0, (elapsed / targetInterval).rounded() - 1))
        }
    }
}
