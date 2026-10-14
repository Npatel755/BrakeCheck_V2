import Foundation

/// Auxiliary budgets only. Never increases the vehicle or viewpoint interval.
public struct ARInferencePolicy {
    public let laneInterval: TimeInterval
    public let laneAllowed: Bool
    public let roadInterval: TimeInterval?
    public init(minimumLaneInterval: TimeInterval, heat: VisionHeat, vehicleLatency: TimeInterval,
                laneLatency: TimeInterval, roadLatency: TimeInterval, stationary: Bool = false) {
        func valid(_ value: Double) -> Double { value.isFinite && value >= 0 ? value : 1 }
        laneInterval = min(0.5, max(0.2, valid(minimumLaneInterval), valid(laneLatency) * 4))
        laneAllowed = heat != .critical && valid(vehicleLatency) <= 0.25
            && (stationary || valid(laneLatency) <= 0.15)
        let base: Double
        switch heat {
        case .nominal: base = 0.60
        case .fair: base = 0.75
        case .serious: base = 0.80
        case .critical: roadInterval = nil; return
        }
        // Once vehicle predictions consume their measurement budget, stop
        // admitting heavy segmentation until the vehicle path recovers.
        guard valid(vehicleLatency) <= 0.20, stationary || valid(roadLatency) <= 0.12 else { roadInterval = nil; return }
        roadInterval = min(1.2, max(base, valid(roadLatency) * 6))
    }
}
