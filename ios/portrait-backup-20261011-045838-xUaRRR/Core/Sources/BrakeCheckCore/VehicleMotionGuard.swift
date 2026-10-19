import Foundation

public enum VehicleTravelDirection: Equatable {
    case uncertain
    case forwardConsistent
    case rapidApproach
}
/// Independent temporal evidence, using approximate visual range and GPS.
/// It is not a measured heading. Uncertain evidence does not authorize a
/// road-only lead, and rapid approach vetoes a misleading rear prediction.
public struct VehicleTravelEvidence {
    public let direction: VehicleTravelDirection
    public let capturedAt: TimeInterval
    public let closingSpeed: Double
    public init(direction: VehicleTravelDirection, capturedAt: TimeInterval, closingSpeed: Double) {
        self.direction = direction; self.capturedAt = capturedAt; self.closingSpeed = closingSpeed
    }
    public func isFresh(at time: TimeInterval) -> Bool {
        time.isFinite && capturedAt.isFinite && closingSpeed.isFinite
            && time >= capturedAt && time - capturedAt <= 0.50
    }
    public func vetoesLead(at time: TimeInterval) -> Bool { isFresh(at: time) && direction == .rapidApproach }
    public func supportsForwardTravel(at time: TimeInterval) -> Bool {
        isFresh(at: time) && direction == .forwardConsistent
    }
}

public struct VehicleMotionGuard {
    private struct Observation { let time: TimeInterval; let range: Double; let speed: Double; let accuracy: Double }
    private struct History { var samples: [Observation] = []; var seenAt: TimeInterval; var vetoUntil: TimeInterval = -.infinity }
    private var histories: [UUID: History] = [:]
    private var lastUpdate: TimeInterval?
    public init() {}
    public mutating func reset() { histories = [:]; lastUpdate = nil }

    public mutating func update(_ vehicles: [LeadVehicle], at time: TimeInterval,
                                horizontalFOV: Double?, ego: GPSSpeedSample?) -> [LeadVehicle] {
        guard time.isFinite else { reset(); return vehicles }
        if let prior = lastUpdate, time <= prior { reset(); return vehicles }
        lastUpdate = time
        histories = histories.filter { time - $0.value.seenAt <= 0.60 }
        return vehicles.map { vehicle in
            guard let id = vehicle.trackID else { return vehicle }
            var history = histories[id] ?? History(seenAt: time)
            if time - history.seenAt > 0.40 { history.samples = [] }
            history.seenAt = time
            var evidence: VehicleTravelEvidence?
            if let fov = horizontalFOV, fov.isFinite, (20...120).contains(fov),
               let ego, ego.metersPerSecond.isFinite, ego.accuracy.isFinite,
               (0...65).contains(ego.metersPerSecond), (0...3).contains(ego.accuracy), !ego.simulated,
               ego.measurementUptime.isFinite, time >= ego.measurementUptime, time - ego.measurementUptime <= 1.5,
               vehicle.confidence >= 0.45, LeadVehicle.validBox(vehicle.box),
               vehicle.box.minX > 0.005, vehicle.box.maxX < 0.995,
               vehicle.box.minY > 0.005, vehicle.box.maxY < 0.995 {
                let range = vehicle.assumedWidthMeters / (2 * tan(fov * .pi / 360) * Double(vehicle.box.width))
                if range.isFinite, (1...120).contains(range) {
                    if let previous = history.samples.last,
                       !(0.70...1.40).contains(range / previous.range) { history.samples = [] }
                    history.samples.removeAll { time - $0.time > 1.2 }
                    history.samples.append(Observation(time: time, range: range,
                        speed: ego.metersPerSecond, accuracy: ego.accuracy))
                    if history.samples.count > 16 { history.samples.removeFirst(history.samples.count - 16) }
                    evidence = Self.evaluate(history.samples, at: time)
                    if evidence?.direction == .rapidApproach { history.vetoUntil = time + 0.8 }
                } else {
                    history.samples = []
                }
            } else {
                // Invalid GPS cannot be bridged by a remembered forward label.
                history.samples = []
            }
            if time < history.vetoUntil {
                evidence = VehicleTravelEvidence(direction: .rapidApproach, capturedAt: time,
                    closingSpeed: evidence?.closingSpeed ?? 0)
            }
            histories[id] = history
            return LeadVehicle(box: vehicle.box, label: vehicle.label, confidence: vehicle.confidence,
                viewpoint: vehicle.viewpoint, trackID: id, travel: evidence)
        }
    }

    private static func evaluate(_ samples: [Observation], at time: TimeInterval) -> VehicleTravelEvidence? {
        guard samples.count >= 4, let first = samples.first, let last = samples.last,
              last.time - first.time >= 0.50,
              samples.allSatisfy({ abs($0.speed - last.speed) <= 3 }) else { return nil }
        let count = Double(samples.count), meanTime = samples.map(\.time).reduce(0, +) / count
        let meanRange = samples.map(\.range).reduce(0, +) / count
        let denominator = samples.reduce(0) { $0 + pow($1.time - meanTime, 2) }
        guard denominator > 0.01 else { return nil }
        let slope = samples.reduce(0) { $0 + ($1.time - meanTime) * ($1.range - meanRange) } / denominator
        let residual = sqrt(samples.reduce(0) {
            $0 + pow($1.range - meanRange - slope * ($1.time - meanTime), 2)
        } / count)
        guard residual <= max(0.50, meanRange * 0.035), slope.isFinite else { return nil }
        let closing = -slope
        let rateError = max(1.5, 2 * residual / (last.time - first.time))
        let accuracy = samples.map(\.accuracy).max() ?? 3
        let egoLow = max(0, last.speed - 2 * accuracy), egoHigh = last.speed + 2 * accuracy
        // Vehicle width is assumed, so use broad scale bounds. Treat an
        // implausibly rapid approach as a veto, not proof of oncoming heading.
        let closingLow = closing >= 0 ? 0.70 * closing - rateError : 1.45 * closing - rateError
        let closingHigh = closing >= 0 ? 1.45 * closing + rateError : 0.70 * closing + rateError
        let direction: VehicleTravelDirection
        if closingLow > egoHigh + 3 { direction = .rapidApproach }
        else if egoLow >= 3, egoLow - closingHigh >= 2 { direction = .forwardConsistent }
        else { direction = .uncertain }
        return VehicleTravelEvidence(direction: direction, capturedAt: time, closingSpeed: closing)
    }
}
