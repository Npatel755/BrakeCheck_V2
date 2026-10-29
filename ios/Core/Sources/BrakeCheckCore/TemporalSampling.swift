import Foundation

/// Adapt sample *history* to actual cadence, without extending the freshness
/// of any output or allowing gaps over the measurement freshness limit.
public enum TemporalSampling {
    public static func window(times: [TimeInterval], minimumSamples: Int,
                              minimumWindow: TimeInterval, maximumWindow: TimeInterval) -> TimeInterval {
        let recent = Array(times.suffix(7))
        var spacing: TimeInterval = 0
        for (previous, current) in zip(recent, recent.dropFirst()) {
            let gap = current - previous
            if gap.isFinite && gap > 0 && gap <= TrackingFreshness.measurementSeconds + 1e-6 {
                spacing = max(spacing, gap)
            }
        }
        return max(minimumWindow, min(maximumWindow, spacing * Double(max(1, minimumSamples - 1)) + 0.10))
    }
}
