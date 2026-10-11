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
    public let isReliableApproach: Bool
    public let hasStableBearing: Bool
    public init(direction: VehicleTravelDirection, capturedAt: TimeInterval, closingSpeed: Double,
                isReliableApproach: Bool = false, hasStableBearing: Bool = false) {
        self.direction = direction; self.capturedAt = capturedAt; self.closingSpeed = closingSpeed
        self.isReliableApproach = isReliableApproach; self.hasStableBearing = hasStableBearing
    }
    public func isFresh(at time: TimeInterval) -> Bool {
        time.isFinite && capturedAt.isFinite && closingSpeed.isFinite
            && time >= capturedAt && time - capturedAt <= 0.50
    }
    public func vetoesLead(at time: TimeInterval) -> Bool { isFresh(at: time) && direction == .rapidApproach }
    public func supportsForwardTravel(at time: TimeInterval) -> Bool {
        isFresh(at: time) && direction == .forwardConsistent
    }
    public func supportsCollisionApproach(at time: TimeInterval) -> Bool {
        isFresh(at: time) && isReliableApproach
    }
}

public struct VehicleMotionGuard {
    private struct Observation {
        let time: TimeInterval; let range: Double
        let speed: Double?; let accuracy: Double?
        let bearing: Double; let aspect: Double
    }
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
        histories = histories.filter { time - $0.value.seenAt <= TrackingFreshness.measurementSeconds + 1e-6 }
        return vehicles.map { vehicle in
            guard let id = vehicle.trackID else { return vehicle }
            var history = histories[id] ?? History(seenAt: time)
            // A skipped 5 Hz night detection can leave a 400–600 ms gap.
            // Keep its real samples through that bounded gap; evaluate only
            // when another fresh detector measurement arrives.
            if time - history.seenAt > TrackingFreshness.measurementSeconds + 1e-6 { history.samples = [] }
            if let reliable = history.samples.last,
               time - reliable.time > TrackingFreshness.measurementSeconds + 1e-6 { history.samples = [] }
            history.seenAt = time
            var evidence: VehicleTravelEvidence?
            if let fov = horizontalFOV, fov.isFinite, (20...120).contains(fov),
               vehicle.confidence.isFinite, (0...1).contains(vehicle.confidence), LeadVehicle.validBox(vehicle.box),
               vehicle.box.minX > 0.005, vehicle.box.maxX < 0.995,
               vehicle.box.minY > 0.005, vehicle.box.maxY < 0.995 {
                let range = vehicle.assumedWidthMeters / (2 * tan(fov * .pi / 360) * Double(vehicle.box.width))
                if range.isFinite, (1...120).contains(range), vehicle.confidence >= 0.45 {
                    if let previous = history.samples.last,
                       !(0.70...1.40).contains(range / previous.range) { history.samples = [] }
                    let window = TemporalSampling.window(times: history.samples.map(\.time) + [time],
                        minimumSamples: 4, minimumWindow: 1.2, maximumWindow: 2.0)
                    history.samples.removeAll { time - $0.time > window + 1e-6 }
                    let gps = ego.flatMap { sample -> GPSSpeedSample? in
                        guard sample.metersPerSecond.isFinite, sample.accuracy.isFinite,
                              (0...65).contains(sample.metersPerSecond), (0...3).contains(sample.accuracy),
                              !sample.simulated, sample.measurementUptime.isFinite,
                              time >= sample.measurementUptime, time - sample.measurementUptime <= 1.5 else { return nil }
                        return sample
                    }
                    history.samples.append(Observation(time: time, range: range,
                        speed: gps?.metersPerSecond, accuracy: gps?.accuracy,
                        bearing: (Double(vehicle.box.midX) - 0.5) / Double(vehicle.box.width),
                        aspect: Double(vehicle.box.width / vehicle.box.height)))
                    if history.samples.count > 16 { history.samples.removeFirst(history.samples.count - 16) }
                    evidence = Self.evaluate(history.samples, at: time)
                    if evidence?.direction == .rapidApproach { history.vetoUntil = time + 0.8 }
                } else if !range.isFinite || !(1...120).contains(range) {
                    history.samples = []
                }
            } else {
                // Invalid range geometry cannot preserve temporal evidence.
                history.samples = []
            }
            if time < history.vetoUntil {
                evidence = VehicleTravelEvidence(direction: .rapidApproach, capturedAt: time,
                    closingSpeed: evidence?.closingSpeed ?? 0,
                    isReliableApproach: evidence?.isReliableApproach ?? false,
                    hasStableBearing: evidence?.hasStableBearing ?? false)
            }
            histories[id] = history
            return LeadVehicle(box: vehicle.box, label: vehicle.label, confidence: vehicle.confidence,
                viewpoint: vehicle.viewpoint, trackID: id, travel: evidence)
        }
    }

    private static func evaluate(_ samples: [Observation], at time: TimeInterval) -> VehicleTravelEvidence? {
        guard samples.count >= 4, let first = samples.first, let last = samples.last,
              last.time - first.time >= 0.50 else { return nil }
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
        // Approach evidence needs camera geometry, not a moving target, GPS
        // speed, or a braking event. Require proportional growth and enough
        // signal above box jitter; yawing vehicles cannot supply that evidence.
        let aspects = samples.map(\.aspect), bearings = samples.map(\.bearing)
        let proportionalGrowth = (aspects.max() ?? 1) / max(0.001, aspects.min() ?? 1) <= 1.20
        let approach = proportionalGrowth && closing > max(0.5, 2 * residual / (last.time - first.time))
        let stableBearing = (bearings.max() ?? 0) - (bearings.min() ?? 0) <= 0.25
        // Vehicle width is assumed, so use broad scale bounds. Treat an
        // implausibly rapid approach as a veto, not proof of oncoming heading.
        let closingLow = closing >= 0 ? 0.70 * closing - rateError : 1.45 * closing - rateError
        let closingHigh = closing >= 0 ? 1.45 * closing + rateError : 0.70 * closing + rateError
        let direction: VehicleTravelDirection
        if let speed = last.speed, samples.allSatisfy({ sample in
            sample.speed.map { abs($0 - speed) <= 3 } == true && sample.accuracy != nil
        }) {
            let accuracy = samples.compactMap(\.accuracy).max() ?? 3
            let egoLow = max(0, speed - 2 * accuracy), egoHigh = speed + 2 * accuracy
            if closingLow > egoHigh + 3 { direction = .rapidApproach }
            else if egoLow >= 3, egoLow - closingHigh >= 2 { direction = .forwardConsistent }
            else { direction = .uncertain }
        } else { direction = .uncertain }
        return VehicleTravelEvidence(direction: direction, capturedAt: time, closingSpeed: closing,
            isReliableApproach: approach, hasStableBearing: stableBearing)
    }
}
