import Foundation

/// At most one crop per slot. Complete a pending rear check promptly, refresh
/// the lead, and share slow classifier slots with other cars and cut-ins.
public struct VehicleViewpointScheduler {
    private var scheduledAt: [UUID: TimeInterval] = [:]
    private var lastUpdate: TimeInterval?
    private var lastChosenID: UUID?
    public init() {}
    public mutating func reset() { scheduledAt = [:]; lastUpdate = nil; lastChosenID = nil }

    /// A stale or cancelled crop did not use a classifier slot. Only release
    /// its own reservation; an older callback cannot cancel a newer check.
    public mutating func cancel(for id: UUID, capturedAt: TimeInterval) {
        if scheduledAt[id] == capturedAt {
            scheduledAt[id] = nil
            if lastChosenID == id { lastChosenID = nil }
        }
    }

    public mutating func choose(_ vehicles: [LeadVehicle], at time: TimeInterval,
                                preferredID: UUID?, lane: LanePath?, boundaries: LanePredictionObservation? = nil) -> LeadVehicle? {
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
        let lead = candidates.first { vehicle in
            let bounds = lane.flatMap { $0.isUsable(at: time) ? $0.bounds(at: Double(vehicle.box.minY)) : nil }
            return vehicle.trackID == preferredID
                && !LeadLaneRelevance.excludes(vehicle.box, boundaries: boundaries, at: time)
                && (bounds == nil || laneOverlap(vehicle, at: time, lane: lane) >= 0.10)
        }
        let others = candidates.filter { $0.trackID != preferredID }
        // At slow cadence every slot is already overdue for the lead. Without
        // this turn-taking rule, a cut-in never gets its own rear check.
        let otherNeedsTurn = lastChosenID == preferredID && others.contains {
            $0.trackID.flatMap { scheduledAt[$0] }.map { time - $0 >= 0.90 } ?? true
        }
        let supported = candidates.filter { laneOverlap($0, at: time, lane: lane) >= 0.35 }
        let relevant = candidates.filter { !LeadLaneRelevance.excludes($0.box, boundaries: boundaries, at: time) }
        let pending = candidates.filter {
            $0.viewpoint?.isRearFacing(at: time) == true
                && $0.viewpoint?.hasConfirmedRearView(at: time) != true
                // Complete the in-lane car's rear check before an adjacent
                // pending crop when a current measured pair distinguishes them.
                && (supported.isEmpty || laneOverlap($0, at: time, lane: lane) >= 0.35)
                && (relevant.isEmpty || !LeadLaneRelevance.excludes($0.box, boundaries: boundaries, at: time))
        }.max { priority($0, at: time, lane: lane, boundaries: boundaries) < priority($1, at: time, lane: lane, boundaries: boundaries) }
        let chosen: LeadVehicle?
        if let pending {
            // At slow cadence alternating single captures can expire both
            // cars' two-capture confirmation windows before either completes.
            chosen = pending
        } else if let lead, !otherNeedsTurn, let id = lead.trackID,
           scheduledAt[id].map({ time - $0 >= 0.50 - 1e-6 }) ?? true {
            chosen = lead
        } else {
            chosen = candidates.filter { vehicle in
                // A confirmed lead needs two refreshes per second, rather
                // than occupying every slot while another car stays unknown.
                vehicle.trackID != preferredID || (!otherNeedsTurn && vehicle.viewpoint?.hasConfirmedRearView(at: time) != true)
            }.max { priority($0, at: time, lane: lane, boundaries: boundaries) < priority($1, at: time, lane: lane, boundaries: boundaries) }
        }
        if let id = chosen?.trackID { scheduledAt[id] = time; lastChosenID = id }
        return chosen
    }

    private func priority(_ vehicle: LeadVehicle, at time: TimeInterval, lane: LanePath?, boundaries: LanePredictionObservation?) -> Double {
        let elapsed = vehicle.trackID.flatMap { scheduledAt[$0] }.map { time - $0 } ?? 1.25
        let pending = vehicle.viewpoint?.isRearFacing(at: time) == true
            && vehicle.viewpoint?.hasConfirmedRearView(at: time) != true
        let bounds = lane.flatMap { $0.isUsable(at: time) ? $0.bounds(at: Double(vehicle.box.minY)) : nil }
        let overlap = laneOverlap(vehicle, at: time, lane: lane)
        let excluded = LeadLaneRelevance.excludes(vehicle.box, boundaries: boundaries, at: time)
            || (bounds != nil && overlap < 0.10)
        return 2 * min(3, elapsed) + (pending && !excluded ? 3 : 0)
            + (vehicle.viewpoint == nil ? 0.5 : 0) + 2.0 * overlap
            - (excluded ? 1.5 : 0)
            + (bounds == nil ? 0.8 * LeadVehicleGeometry.forwardOverlap(vehicle) : 0)
            + 0.4 * max(0, 0.75 - Double(vehicle.box.minY)) + 0.2 * vehicle.confidence
    }

    private func laneOverlap(_ vehicle: LeadVehicle, at time: TimeInterval, lane: LanePath?) -> Double {
        guard let lane, lane.isUsable(at: time), let bounds = lane.bounds(at: Double(vehicle.box.minY)) else { return 0 }
        return max(0, min(Double(vehicle.box.maxX), bounds.right) - max(Double(vehicle.box.minX), bounds.left))
            / Double(vehicle.box.width)
    }
}
