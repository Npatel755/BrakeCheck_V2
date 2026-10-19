import Foundation

public struct VehicleRangeCapture {
    public let targetID: UUID
    public let capturedAt: TimeInterval
    public let snapshot: CameraRangeSnapshot
    public func isCurrent(for id: UUID, at time: TimeInterval) -> Bool {
        id == targetID && time.isFinite && capturedAt == time
    }
}

/// Collect real detector ranges before lead confirmation finishes. Histories
/// are isolated by ID/model/FOV and do not authorize a target or warning.
public struct VehicleRangeHistory {
    private struct State {
        var estimator = CameraRangeEstimator()
        var seenAt: TimeInterval
    }
    private var states: [UUID: State] = [:]
    private var lastUpdate: TimeInterval?
    private var lastFOV: Double?
    private var generation: Int?
    public init() {}
    public mutating func reset() { states = [:]; lastUpdate = nil; lastFOV = nil; generation = nil }
    public mutating func update(_ vehicles: [LeadVehicle], at time: TimeInterval,
                                horizontalFOV: Double, detectorGeneration: Int) -> [UUID: VehicleRangeCapture] {
        guard time.isFinite, horizontalFOV.isFinite, (20...120).contains(horizontalFOV) else { reset(); return [:] }
        if let lastUpdate, time <= lastUpdate { reset(); return [:] }
        if generation != detectorGeneration || lastFOV.map({ abs($0 - horizontalFOV) > 0.01 }) == true { reset() }
        lastUpdate = time; lastFOV = horizontalFOV; generation = detectorGeneration
        states = states.filter { time - $0.value.seenAt <= 0.6 + 1e-6 }
        var result: [UUID: VehicleRangeCapture] = [:]
        for vehicle in vehicles.prefix(64) {
            guard let id = vehicle.trackID, result[id] == nil else { continue }
            var state = states[id] ?? State(seenAt: time)
            let snapshot: CameraRangeSnapshot
            if let distance = Self.distance(for: vehicle, horizontalFOV: horizontalFOV) {
                snapshot = state.estimator.update(targetID: id, time: time, distance: distance,
                    confidence: vehicle.confidence, fresh: true)
            } else {
                state.estimator.reset()
                snapshot = CameraRangeSnapshot(reason: "Vehicle clipped · gap numbers paused")
            }
            state.seenAt = time; states[id] = state
            result[id] = VehicleRangeCapture(targetID: id, capturedAt: time, snapshot: snapshot)
        }
        if states.count > 128 {
            states = Dictionary(uniqueKeysWithValues: states.sorted { $0.value.seenAt > $1.value.seenAt }.prefix(128).map { ($0.key, $0.value) })
        }
        return result
    }
    public static func distance(for vehicle: LeadVehicle, horizontalFOV: Double) -> Double? {
        guard horizontalFOV.isFinite, (20...120).contains(horizontalFOV), LeadVehicle.validBox(vehicle.box),
              vehicle.box.minX > 0.005, vehicle.box.maxX < 0.995,
              vehicle.box.minY > 0.005, vehicle.box.maxY < 0.995 else { return nil }
        let distance = vehicle.assumedWidthMeters / (2 * tan(horizontalFOV * .pi / 360) * Double(vehicle.box.width))
        return distance.isFinite && (1...150).contains(distance) ? distance : nil
    }
}
