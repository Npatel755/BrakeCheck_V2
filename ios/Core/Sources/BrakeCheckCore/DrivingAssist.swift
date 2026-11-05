import Foundation
#if canImport(CoreGraphics)
import CoreGraphics
#endif

public enum DrivingAssistFeature: String, CaseIterable, Identifiable, Codable {
    case trafficStart, laneDeparture, cutIn, greenLight, followingDistance
    public var id: String { rawValue }
    public var preferenceKey: String { "premium.drivingAssist." + rawValue }
    public var title: String {
        switch self {
        case .trafficStart: return "Traffic Start Alert"
        case .laneDeparture: return "Lane Departure"
        case .cutIn: return "Cut-In Prediction"
        case .greenLight: return "Green Light Notification"
        case .followingDistance: return "Following Distance"
        }
    }
    public var detail: String {
        switch self {
        case .trafficStart: return "A reminder when the tracked vehicle ahead moves while you remain stopped."
        case .laneDeparture: return "A reminder when you drift toward a visible lane line above 18 mph. Requires camera alignment."
        case .cutIn: return "A heads-up when a tracked vehicle appears to be moving into your lane."
        case .greenLight: return "A reminder after a clearly visible red signal changes to green while you are stopped."
        case .followingDistance: return "A gentle reminder when you follow the car ahead closer than about one second for several seconds."
        }
    }
    public var icon: String {
        switch self {
        case .trafficStart: return "car.side.arrowtriangle.up.arrowtriangle.down"
        case .laneDeparture: return "road.lanes"
        case .cutIn: return "arrow.triangle.branch"
        case .greenLight: return "light.beacon.max"
        case .followingDistance: return "car.2"
        }
    }
    public var alertText: String {
        switch self {
        case .trafficStart: return "Traffic ahead is moving"
        case .laneDeparture: return "Check your lane position"
        case .cutIn: return "Vehicle may enter your lane"
        case .greenLight: return "Signal changed — check the road"
        case .followingDistance: return "Leave more room ahead"
        }
    }
    /// The alerts BrakeSense offers. Lane Departure and Cut-In rely on the bundled, locally trained
    /// UFLDv2 lane model and stay behind the engine's alignment, speed and confidence gates.
    public static let offered: [Self] = [.trafficStart, .greenLight, .laneDeparture, .cutIn, .followingDistance]
    /// Alerts that only make sense while stopped, shown together as "Go alerts".
    public static let stopped: [Self] = [.trafficStart, .greenLight]
    /// Alerts that watch lane geometry while moving.
    public static let moving: [Self] = [.laneDeparture, .cutIn, .followingDistance]
    public static func enabled(in defaults: UserDefaults, premium: Bool) -> Set<Self> {
        guard premium else { return [] }
        return Set(offered.filter { defaults.bool(forKey: $0.preferenceKey) })
    }
}

public struct DrivingAssistAlert: Equatable {
    public enum Side: Equatable { case left, right }
    public let feature: DrivingAssistFeature
    public let capturedAt: TimeInterval
    /// Lane Departure: the side of the lane the vehicle is drifting toward.
    public let side: Side?
    public init(_ feature: DrivingAssistFeature, at time: TimeInterval, side: Side? = nil) {
        self.feature = feature; capturedAt = time; self.side = side
    }
    public var text: String {
        switch (feature, side) {
        case (.laneDeparture, .left?): return "Drifting left — check your lane"
        case (.laneDeparture, .right?): return "Drifting right — check your lane"
        default: return feature.alertText
        }
    }
    public var spanishText: String {
        switch (feature, side) {
        case (.trafficStart, _): return "El tráfico de delante se mueve."
        case (.laneDeparture, .left?): return "Te desvías a la izquierda. Comprueba tu carril."
        case (.laneDeparture, .right?): return "Te desvías a la derecha. Comprueba tu carril."
        case (.laneDeparture, nil): return "Comprueba tu posición en el carril."
        case (.cutIn, _): return "Un vehículo podría entrar en tu carril."
        case (.greenLight, _): return "El semáforo ha cambiado. Comprueba la vía."
        case (.followingDistance, _): return "Deja más distancia con el vehículo de delante."
        }
    }
}

public enum TrafficSignalColor: String { case red, green, unknown }
public struct TrafficSignalObservation {
    public let box: CGRect
    public let color: TrafficSignalColor
    public let capturedAt: TimeInterval
    public init(box: CGRect, color: TrafficSignalColor, capturedAt: TimeInterval) {
        self.box = box; self.color = color; self.capturedAt = capturedAt
    }
}

/// Conservative temporal candidates, not calibrated probabilities or vehicle control.
/// Each stream requires distinct fresh captures; repeated display ticks cannot arm it.
public struct DrivingAssistEngine {
    private var cooldown: [DrivingAssistFeature: TimeInterval] = [:]
    private var leadID: UUID?
    private var leadTime: TimeInterval?
    private var stoppedSince: TimeInterval?
    private var stoppedRange: Double?
    private var leadArmed = false
    private var movingSince: TimeInterval?
    private var laneTime: TimeInterval?
    private var centeredSince: TimeInterval?
    private var departureSince: TimeInterval?
    private var laneArmed = false
    private var previousOffset: Double?
    private struct VehicleSample { let box: CGRect; let time: TimeInterval; let offset: Double }
    private var vehicles: [UUID: VehicleSample] = [:]
    private var inwardSince: [UUID: TimeInterval] = [:]
    private var vehicleTime: TimeInterval?
    private var signalBox: CGRect?
    private var signalTime: TimeInterval?
    private var redSince: TimeInterval?
    private var redArmed = false
    private var greenSince: TimeInterval?

    public init() {}
    public mutating func reset() { self = Self() }
    private mutating func emit(_ feature: DrivingAssistFeature, at time: TimeInterval,
                               side: DrivingAssistAlert.Side? = nil) -> DrivingAssistAlert? {
        guard time - (cooldown[feature] ?? -.infinity) >= 12 else { return nil }
        cooldown[feature] = time
        return DrivingAssistAlert(feature, at: time, side: side)
    }
    private func fresh(_ time: TimeInterval, now: TimeInterval) -> Bool {
        time.isFinite && now.isFinite && now >= time && now - time <= 0.5
    }
    private mutating func resetLead() {
        leadID = nil; leadTime = nil; stoppedSince = nil; stoppedRange = nil
        leadArmed = false; movingSince = nil
    }
    public mutating func updateLead(id: UUID, distance: Double?, rangeRate: Double?, confidence: Double,
                                   rearConfirmed: Bool, stationary: Bool, enabled: Bool,
                                   capturedAt time: TimeInterval, now: TimeInterval) -> DrivingAssistAlert? {
        guard enabled, stationary, rearConfirmed, confidence >= 0.8, fresh(time, now: now),
              let distance, distance.isFinite, (2...50).contains(distance),
              let rate = rangeRate, rate.isFinite else { resetLead(); return nil }
        // A repeated callback is not another observation. Ignore it instead
        // of clearing already measured stopped-lead history.
        if leadID == id, let previous = leadTime, time == previous { return nil }
        if leadID != id || leadTime.map({ time < $0 || time - $0 > 0.6 }) == true { resetLead() }
        leadID = id; leadTime = time
        if !leadArmed {
            guard abs(rate) <= 0.3 else { stoppedSince = nil; stoppedRange = nil; return nil }
            if stoppedSince == nil { stoppedSince = time; stoppedRange = distance }
            if let range = stoppedRange, abs(distance - range) > 0.7 {
                stoppedSince = time; stoppedRange = distance
            }
            if time - (stoppedSince ?? time) >= 2 { leadArmed = true }
            return nil
        }
        guard rate >= 0.65, distance - (stoppedRange ?? distance) >= 0.9 else {
            movingSince = nil; return nil
        }
        if movingSince == nil { movingSince = time }
        guard time - (movingSince ?? time) >= 0.6 else { return nil }
        resetLead()
        return emit(.trafficStart, at: time)
    }

    private var headwayLeadID: UUID?
    private var headwayTime: TimeInterval?
    private var closeSince: TimeInterval?
    /// Sustained short headway behind the same confirmed lead at road speed.
    /// This is coaching, with a long cooldown; collision warnings are separate.
    public mutating func updateHeadway(leadID: UUID, headway: Double?, speed: Double?, enabled: Bool,
                                      capturedAt time: TimeInterval, now: TimeInterval) -> DrivingAssistAlert? {
        guard enabled, fresh(time, now: now), let speed, speed.isFinite, speed >= 10,
              let headway, headway.isFinite, headway > 0 else {
            headwayLeadID = nil; headwayTime = nil; closeSince = nil; return nil
        }
        if headwayLeadID != leadID || headwayTime.map({ time < $0 || time - $0 > 0.6 }) == true { closeSince = nil }
        headwayLeadID = leadID; headwayTime = time
        guard headway < 1.0 else { closeSince = nil; return nil }
        if closeSince == nil { closeSince = time }
        guard time - (closeSince ?? time) >= 4 else { return nil }
        closeSince = nil
        guard let alert = emit(.followingDistance, at: time) else { return nil }
        cooldown[.followingDistance] = time + 48   // at most once a minute
        return alert
    }

    public mutating func updateLane(_ lane: LanePath?, cameraCenter: Double, speed: Double?,
                                   aligned: Bool, enabled: Bool, now: TimeInterval) -> DrivingAssistAlert? {
        guard enabled, aligned, let speed, speed.isFinite, speed >= 8,
              cameraCenter.isFinite, (0.35...0.65).contains(cameraCenter),
              let lane, fresh(lane.capturedAt, now: now), lane.isUsable(at: now),
              lane.source != .markings, lane.confidence >= 0.85,
              let band = lane.bands.first, band.y <= 0.3,
              (0.15...0.95).contains(band.right - band.left) else {
            laneTime = nil; centeredSince = nil; departureSince = nil; laneArmed = false; previousOffset = nil
            return nil
        }
        let time = lane.capturedAt
        if let last = laneTime {
            if time <= last { return nil }
            if time - last > 0.6 { centeredSince = nil; departureSince = nil; laneArmed = false; previousOffset = nil }
        }
        laneTime = time
        let offset = (cameraCenter - (band.left + band.right) / 2) / (band.right - band.left)
        if let previousOffset, abs(offset - previousOffset) > 0.12 {
            laneArmed = false; centeredSince = nil; departureSince = nil
        }
        previousOffset = offset
        if abs(offset) < 0.16 {
            departureSince = nil
            if centeredSince == nil { centeredSince = time }
            if time - (centeredSince ?? time) >= 1.5 { laneArmed = true }
            return nil
        }
        centeredSince = nil
        guard laneArmed, abs(offset) >= 0.30 else { departureSince = nil; return nil }
        if departureSince == nil { departureSince = time }
        guard time - (departureSince ?? time) >= 0.6 else { return nil }
        laneArmed = false; departureSince = nil
        // offset is camera centre relative to lane centre: positive means the
        // vehicle sits right of centre and is drifting toward the right line.
        return emit(.laneDeparture, at: time, side: offset > 0 ? .right : .left)
    }

    public mutating func updateVehicles(_ observations: [LeadVehicle], lane: LanePath?,
                                       speed: Double?, enabled: Bool, capturedAt time: TimeInterval,
                                       now: TimeInterval) -> DrivingAssistAlert? {
        guard enabled, fresh(time, now: now), let speed, speed.isFinite, speed >= 2,
              let lane, lane.isUsable(at: time), lane.source != .markings, lane.confidence >= 0.85 else {
            vehicles = [:]; inwardSince = [:]; vehicleTime = nil; return nil
        }
        if let last = vehicleTime {
            if time <= last { return nil }
            if time - last > 0.6 { vehicles = [:]; inwardSince = [:] }
        }
        vehicleTime = time
        var next: [UUID: VehicleSample] = [:]
        var result: DrivingAssistAlert?
        for vehicle in observations {
            guard let id = vehicle.trackID, vehicle.confidence >= 0.8, LeadVehicle.validBox(vehicle.box),
                  vehicle.viewpoint?.isRearFacing(at: time) == true,
                  vehicle.box.width >= 0.035,
                  let bounds = lane.bounds(at: Double(vehicle.box.minY)) else { continue }
            let width = bounds.right - bounds.left
            let offset = (Double(vehicle.box.midX) - (bounds.left + bounds.right) / 2) / width
            next[id] = VehicleSample(box: vehicle.box, time: time, offset: offset)
            guard let old = vehicles[id], (0.08...0.6).contains(time - old.time),
                  abs(Double(vehicle.box.minY - old.box.minY)) < 0.04,
                  (0.8...1.25).contains(Double(vehicle.box.width / old.box.width)),
                  offset * old.offset > 0, (0.32...1.25).contains(abs(old.offset)) else {
                inwardSince[id] = nil; continue
            }
            let inwardSpeed = (abs(old.offset) - abs(offset)) / (time - old.time)
            let entryTime = (abs(offset) - 0.30) / max(0.001, inwardSpeed)
            guard inwardSpeed >= 0.10, inwardSpeed <= 1.2, abs(offset) > 0.3,
                  (0...2).contains(entryTime) else { inwardSince[id] = nil; continue }
            if inwardSince[id] == nil { inwardSince[id] = old.time }
            if time - (inwardSince[id] ?? time) >= 0.6, result == nil {
                result = emit(.cutIn, at: time); inwardSince[id] = nil
            }
        }
        vehicles = next; inwardSince = inwardSince.filter { next[$0.key] != nil }
        return result
    }

    public mutating func updateSignal(_ observation: TrafficSignalObservation?, stationary: Bool,
                                     enabled: Bool, now: TimeInterval) -> DrivingAssistAlert? {
        guard enabled, stationary, let observation, observation.color != .unknown,
              fresh(observation.capturedAt, now: now), LeadVehicle.validBox(observation.box) else {
            signalBox = nil; signalTime = nil; redSince = nil; greenSince = nil; redArmed = false
            return nil
        }
        let time = observation.capturedAt, box = observation.box
        if let last = signalTime, time <= last { return nil }
        if let previous = signalBox {
            let overlap = previous.intersection(box)
            let intersection = overlap.isNull ? 0 : overlap.width * overlap.height
            let union = previous.width * previous.height + box.width * box.height - intersection
            if intersection / max(0.000001, union) < 0.5 || time - (signalTime ?? time) > 0.6 {
                redSince = nil; redArmed = false; greenSince = nil
            }
        }
        signalTime = time; signalBox = box
        if observation.color == .red {
            greenSince = nil
            if redSince == nil { redSince = time }
            if time - (redSince ?? time) >= 1.5 { redArmed = true }
            return nil
        }
        guard redArmed else { return nil }
        if greenSince == nil { greenSince = time }
        guard time - (greenSince ?? time) >= 0.8 else { return nil }
        redSince = nil; redArmed = false; greenSince = nil
        return emit(.greenLight, at: time)
    }
}
