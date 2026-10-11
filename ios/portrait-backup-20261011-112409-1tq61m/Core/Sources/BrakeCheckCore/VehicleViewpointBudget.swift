import Foundation

/// A 240 ms rear classifier cannot run every 250 ms alongside the vehicle
/// and lane models. Leave roughly two thirds of accelerator time for them.
public struct VehicleViewpointBudget {
    private var latency: TimeInterval = 0
    public init() {}
    public mutating func reset() { latency = 0 }
    public mutating func record(predictionSeconds: TimeInterval) {
        guard predictionSeconds.isFinite, (0.001...2).contains(predictionSeconds) else { return }
        // React quickly to slow work and recover gradually when it speeds up.
        latency = max(predictionSeconds, 0.80 * latency + 0.20 * predictionSeconds)
    }
    public func interval(minimum: TimeInterval) -> TimeInterval {
        max(minimum, min(0.75, latency * 3))
    }
    public func laneInterval(minimum: TimeInterval) -> TimeInterval {
        max(minimum, latency > 0.15 ? 1.0 / 3 : 0)
    }
}
