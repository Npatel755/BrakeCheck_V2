import Foundation

/// Cadence limits, independent of camera recording and lane geometry.
public enum VisionHeat: Int { case nominal, fair, serious, critical }
public struct VisionWorkPolicy {
    public let vehicleInterval: TimeInterval
    public private(set) var laneInterval: TimeInterval
    public private(set) var viewpointInterval: TimeInterval
    public private(set) var recoveryInterval: TimeInterval?
    public init(night: Bool, heat: VisionHeat, vehicleLatency: TimeInterval = 0) {
        switch heat {
        case .nominal:
            vehicleInterval = night ? 0.20 : 0.10
            laneInterval = night ? 1.0 / 3 : 0.20
            viewpointInterval = 0.25
            recoveryInterval = night ? 1.25 : 0.75
        case .fair:
            vehicleInterval = night ? 0.25 : 0.125
            laneInterval = 1.0 / 3
            viewpointInterval = 1.0 / 3
            recoveryInterval = night ? nil : 1.0
        case .serious:
            vehicleInterval = 1.0 / 3
            laneInterval = 0.40
            viewpointInterval = 0.50
            recoveryInterval = nil
        case .critical:
            vehicleInterval = 0.50
            laneInterval = 0.50
            viewpointInterval = 0.50
            recoveryInterval = nil
        }
        // If the vehicle path is taking over 200 ms, trim auxiliary work
        // without increasing the age allowed for numerical measurements.
        if vehicleLatency.isFinite, vehicleLatency > 0.20 {
            self.laneInterval = max(laneInterval, 0.40)
            self.viewpointInterval = max(viewpointInterval, 0.50)
            self.recoveryInterval = nil
        }
    }
}
