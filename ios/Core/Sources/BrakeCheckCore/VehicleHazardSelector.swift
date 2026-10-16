import Foundation

/// A collision hazard need not be moving, rear-facing, or braking. When
/// markings are missing, camera projection can support a possible obstacle
/// warning without claiming that an ego lane was measured.
public struct VehicleHazardSelector {
    private enum PathSupport: Equatable { case measuredLane, roadProjection, cameraProjection }
    private var preferredID: UUID?
    private var candidateStarted: TimeInterval?
    private var lastUpdate: TimeInterval?
    private var lastEligibleAt: TimeInterval?
    private var observations = 0
    private var lastImageAspect: Double?

    public init() {}
    public mutating func reset() {
        preferredID = nil; candidateStarted = nil; lastUpdate = nil
        observations = 0; lastImageAspect = nil; lastEligibleAt = nil
    }

    public mutating func update(_ vehicles: [LeadVehicle], at time: TimeInterval,
                                lane: LanePath?, road: RoadRegion?, imageAspect: Double) -> LeadSelection? {
        guard time.isFinite, imageAspect.isFinite, (0.1...10).contains(imageAspect) else {
            reset(); return nil
        }
        if let previous = lastUpdate {
            guard time > previous else { reset(); return nil }
            if time - previous > 0.60 + 1e-6 { reset() }
        }
        if let previous = lastImageAspect, abs(previous - imageAspect) > 0.001 { reset() }
        lastUpdate = time; lastImageAspect = imageAspect
        if let lastEligibleAt, time - lastEligibleAt > 0.60 + 1e-6 {
            preferredID = nil; candidateStarted = nil; observations = 0
            self.lastEligibleAt = nil
        }
        let lane = lane.flatMap { $0.isUsable(at: time) ? $0 : nil }
        let road = road.flatMap { $0.isUsable(at: time) ? $0 : nil }
        let eligible = vehicles.compactMap { vehicle -> (vehicle: LeadVehicle, support: PathSupport)? in
            guard vehicle.trackID != nil, vehicle.confidence.isFinite,
                  (0.65...1).contains(vehicle.confidence), LeadVehicle.validBox(vehicle.box),
                  ["car", "truck", "bus", "motorcycle", "motorbike"].contains(vehicle.label),
                  let travel = vehicle.travel, travel.supportsCollisionApproach(at: time),
                  (0.5...60).contains(travel.closingSpeed),
                  let support = Self.pathSupport(vehicle, lane: lane, road: road, at: time) else { return nil }
            let shape = Double(vehicle.box.width / vehicle.box.height) * imageAspect
            guard (0.25...(vehicle.label == "car" ? 3.5 : 4.5)).contains(shape) else { return nil }
            return (vehicle, support)
        }
        // The FOV factor is common to all ranges in this frame and cancels
        // when ranking TTC. Keep a nearly tied current hazard to avoid churn.
        func urgency(_ vehicle: LeadVehicle) -> Double {
            vehicle.assumedWidthMeters / (Double(vehicle.box.width) * max(0.001, vehicle.travel?.closingSpeed ?? 0))
        }
        guard var best = eligible.min(by: { urgency($0.vehicle) < urgency($1.vehicle) }) else {
            // Keep confirmation across a brief missing observation, without
            // emitting a held hazard or renewing the last real evidence.
            return nil
        }
        if let current = eligible.first(where: { $0.vehicle.trackID == preferredID }),
           urgency(current.vehicle) <= urgency(best.vehicle) * 1.15 { best = current }
        if preferredID != best.vehicle.trackID {
            preferredID = best.vehicle.trackID; candidateStarted = time; observations = 1
        } else { observations += 1 }
        lastEligibleAt = time
        // Track identity and camera approach have already been established.
        // Confirm the hazard itself, without reapplying a generic lane-center
        // or rear-view gate through the moving-lead selector.
        guard let id = preferredID, let started = candidateStarted,
              observations >= 3, time - started >= 0.20 - 1e-6 else { return nil }
        return LeadSelection(id: id, vehicle: best.vehicle, isFreshDetection: true,
            isPathSupported: true, isCollisionHazard: true,
            isCameraProjectedPath: best.support == .cameraProjection)
    }

    private static func pathSupport(_ vehicle: LeadVehicle, lane: LanePath?,
                                    road: RoadRegion?, at time: TimeInterval) -> PathSupport? {
        let box = vehicle.box
        let foot = Double(box.minY), center = Double(box.midX)
        if let lane, let bounds = lane.bounds(at: foot) {
            let overlap = max(0, min(Double(box.maxX), bounds.right) - max(Double(box.minX), bounds.left))
                / Double(box.width)
            if overlap >= 0.50 && center >= bounds.left && center <= bounds.right { return .measuredLane }
        }
        guard vehicle.travel?.hasStableBearing == true,
              box.minX > 0.005, box.maxX < 0.995, box.minY > 0.005, box.maxY < 0.995 else { return nil }
        // Project a 1.9 m ego width at the target's estimated depth. This
        // requires a fixed, centered, forward-facing camera. The target's
        // assumed width supplies scale; no measured lane is invented.
        let halfWidth = Double(box.width) * 1.9 / vehicle.assumedWidthMeters / 2
        let left = max(0, 0.5 - halfWidth), right = min(1, 0.5 + halfWidth)
        let intersection = max(0, min(Double(box.maxX), right) - max(Double(box.minX), left))
        let projectedOverlap = intersection / Double(box.width)
        if let road {
            let roadOverlap = road.overlap(with: box, at: time)
            let rows = [foot, foot - 0.025, foot - 0.05].compactMap { road.bounds(at: $0) }
            if (roadOverlap ?? 0) >= 0.70,
               projectedOverlap >= 0.20 / vehicle.assumedWidthMeters,
               rows.count >= 2, rows.allSatisfy({
                   max(0, min(right, $0.right) - max(left, $0.left)) / max(0.001, right - left) >= 0.70
               }) { return .roadProjection }
        }
        // Night/unmarked-road fallback: fresh approach + stable bearing + a
        // mostly overlapping forward footprint. Neural lane/road estimates
        // provide positive support, but cannot veto this direct approach.
        guard vehicle.confidence >= 0.75, foot < 0.75,
              projectedOverlap >= 0.55, center >= left, center <= right else { return nil }
        return .cameraProjection
    }
}
