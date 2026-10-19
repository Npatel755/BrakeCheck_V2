import Foundation

/// At most one crop per slot. Refresh the current lead at least every half
/// second, and use the remaining slots to confirm other cars and cut-ins.
public struct VehicleViewpointScheduler {
    private var scheduledAt: [UUID: TimeInterval] = [:]
    private var lastUpdate: TimeInterval?
    public init() {}
    public mutating func reset() { scheduledAt = [:]; lastUpdate = nil }

    public mutating func choose(_ vehicles: [LeadVehicle], at time: TimeInterval,
                                preferredID: UUID?, lane: LanePath?) -> LeadVehicle? {
        guard time.isFinite else { reset(); return nil }
        if let previous = lastUpdate {
            if time == previous { return nil }
            if time < previous { reset() }
        }
        lastUpdate = time
        scheduledAt = scheduledAt.filter { time - $0.value <= 3.25 }
        let candidates = vehicles.filter {
            guard let id = $0.trackID, LeadVehicle.validBox($0.box),
                  $0.confidence.isFinite, (0.40...1).contains($0.confidence),
                  ["car", "truck"].contains($0.label) else { return false }
            return scheduledAt[id].map { time - $0 >= 0.24 } ?? true
        }
        let lead = candidates.first { $0.trackID == preferredID }
        let chosen: LeadVehicle?
        if let lead, let id = lead.trackID,
           scheduledAt[id].map({ time - $0 >= 0.50 - 1e-6 }) ?? true {
            chosen = lead
        } else {
            chosen = candidates.filter { vehicle in
                // A confirmed lead needs two refreshes per second, rather
                // than occupying every slot while another car stays unknown.
                vehicle.trackID != preferredID || vehicle.viewpoint?.hasConfirmedRearView(at: time) != true
            }.max { priority($0, at: time, lane: lane) < priority($1, at: time, lane: lane) }
        }
        if let id = chosen?.trackID { scheduledAt[id] = time }
        return chosen
    }

    private func priority(_ vehicle: LeadVehicle, at time: TimeInterval, lane: LanePath?) -> Double {
        let elapsed = vehicle.trackID.flatMap { scheduledAt[$0] }.map { time - $0 } ?? 1.25
        let pending = vehicle.viewpoint?.isRearFacing(at: time) == true
            && vehicle.viewpoint?.hasConfirmedRearView(at: time) != true
        let bounds = lane.flatMap { $0.isUsable(at: time) ? $0.bounds(at: Double(vehicle.box.minY)) : nil }
        let overlap = bounds.map {
            max(0, min(Double(vehicle.box.maxX), $0.right) - max(Double(vehicle.box.minX), $0.left))
                / Double(vehicle.box.width)
        } ?? 0
        return 2 * min(3, elapsed) + (pending ? 3 : 0)
            + (vehicle.viewpoint == nil ? 0.5 : 0) + 0.6 * overlap
            + 0.4 * max(0, 0.75 - Double(vehicle.box.minY)) + 0.2 * vehicle.confidence
    }
}
