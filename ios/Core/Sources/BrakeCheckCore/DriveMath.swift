import Foundation

public struct TimedValue {
    public let time: TimeInterval
    public let value: Double
    public init(time: TimeInterval, value: Double) { self.time = time; self.value = value }
}

public enum DriveMath {
    public static func clamp(_ value: Double, _ lower: Double, _ upper: Double) -> Double {
        min(upper, max(lower, value))
    }

    /// Least-squares slope; times are centered to avoid precision loss at large timestamps.
    public static func slope(_ samples: [TimedValue], minimumSpan: Double) -> Double? {
        guard samples.count >= 3, let first = samples.first, let last = samples.last,
              last.time - first.time >= minimumSpan,
              samples.allSatisfy({ $0.time.isFinite && $0.value.isFinite }) else { return nil }
        let meanT = samples.reduce(0) { $0 + ($1.time - first.time) } / Double(samples.count)
        let meanV = samples.reduce(0) { $0 + $1.value } / Double(samples.count)
        var numerator = 0.0
        var denominator = 0.0
        for sample in samples {
            let t = sample.time - first.time - meanT
            numerator += t * (sample.value - meanV)
            denominator += t * t
        }
        guard denominator > 1e-8 else { return nil }
        return numerator / denominator
    }

    /// Same object, same camera zoom, full rear silhouette: distance scales inversely with width.
    public static func calibratedDistance(referenceMeters: Double, referenceWidth: Double,
                                   currentWidth: Double) -> Double? {
        guard referenceMeters.isFinite, referenceMeters > 0,
              referenceWidth.isFinite, referenceWidth > 0, referenceWidth <= 1,
              currentWidth.isFinite, currentWidth > 0.015, currentWidth <= 1 else { return nil }
        let result = referenceMeters * referenceWidth / currentWidth
        return (1...150).contains(result) ? result : nil
    }
}

public enum RiskLevel: String, Codable {
    case unknown = "Unavailable"
    case low = "Lower indication"
    case elevated = "Elevated"
    case high = "High"
    case critical = "Critical"
}

public struct RiskSnapshot {
    public var level: RiskLevel = .unknown
    public var score: Double? = nil
    public var distance: Double? = nil
    public var closingSpeed: Double? = nil
    public var leadSpeed: Double? = nil
    public var leadAcceleration: Double? = nil
    public var timeToCollision: Double? = nil
    public var headway: Double? = nil
    public var reason = "Select and calibrate a vehicle to begin."

    public init(level: RiskLevel = .unknown, score: Double? = nil, distance: Double? = nil,
                closingSpeed: Double? = nil, leadSpeed: Double? = nil, leadAcceleration: Double? = nil,
                timeToCollision: Double? = nil, headway: Double? = nil,
                reason: String = "Waiting for a vehicle and a fresh GPS speed.") {
        self.level = level; self.score = score; self.distance = distance
        self.closingSpeed = closingSpeed; self.leadSpeed = leadSpeed
        self.leadAcceleration = leadAcceleration; self.timeToCollision = timeToCollision
        self.headway = headway; self.reason = reason
    }
}

public enum RiskRules {
    /// A heuristic indication, NOT a calibrated collision probability or a braking controller.
    public static func evaluate(distance: Double, egoSpeed: Double, rangeRate: Double,
                         leadAcceleration: Double?) -> RiskSnapshot {
        guard distance.isFinite, distance > 0, egoSpeed.isFinite, egoSpeed >= 0,
              rangeRate.isFinite, abs(rangeRate) <= 60 else {
            return RiskSnapshot(reason: "Measurements are invalid.")
        }
        let inferredSpeed = egoSpeed + rangeRate
        guard inferredSpeed >= -2, inferredSpeed <= 90 else {
            return RiskSnapshot(reason: "Tracking and GPS disagree. Recalibrate.")
        }
        let closing = max(0, -rangeRate)
        let ttc = closing > 0.5 ? distance / closing : nil
        let headway = egoSpeed > 2 ? distance / egoSpeed : nil
        let acceleration = leadAcceleration.flatMap { $0.isFinite && abs($0) <= 15 ? $0 : nil }
        var score = 0.0
        var reasons: [String] = []
        if let ttc {
            if ttc < 1.5 { score = 95; reasons.append("Very short closing time") }
            else if ttc < 3 { score = 78; reasons.append("Rapidly closing gap") }
            else if ttc < 5 { score = 52; reasons.append("Closing gap") }
        }
        if let headway {
            if headway < 0.8 { score = max(score, 85); reasons.append("Very short following interval") }
            else if headway < 1.5 { score = max(score, 58); reasons.append("Short following interval") }
            else if headway < 2.5 { score = max(score, 30) }
        }
        if let acceleration, acceleration < -2.5, let headway, headway < 3 {
            score = max(score, acceleration < -5 ? 85 : 65)
            reasons.append("Lead vehicle appears to be braking")
        }
        let level: RiskLevel = score >= 85 ? .critical : score >= 60 ? .high : score >= 30 ? .elevated : .low
        return RiskSnapshot(level: level, score: score, distance: distance,
                            closingSpeed: closing, leadSpeed: max(0, inferredSpeed),
                            leadAcceleration: acceleration, timeToCollision: ttc,
                            headway: headway,
                            reason: reasons.isEmpty ? "No elevated indication in the current estimates." : reasons.joined(separator: " · "))
    }
}

public struct RiskEstimator {
    private var distances: [TimedValue] = []
    private var leadSpeeds: [TimedValue] = []
    private var lastTime: TimeInterval?
    public init() {}

    public mutating func reset() {
        distances.removeAll()
        leadSpeeds.removeAll()
        lastTime = nil
    }

    public mutating func update(time: TimeInterval, distance: Double, egoSpeed: Double?,
                         gpsAge: TimeInterval, confidence: Double) -> RiskSnapshot {
        guard time.isFinite, distance.isFinite, (1...150).contains(distance),
              confidence >= 0.65, let egoSpeed, egoSpeed.isFinite,
              egoSpeed >= 0, gpsAge >= 0, gpsAge <= 2 else {
            reset()
            return RiskSnapshot(reason: "Waiting for reliable tracking and a fresh GPS speed.")
        }
        if let lastTime {
            guard time > lastTime else {
                reset()
                return RiskSnapshot(reason: "Waiting for ordered camera samples.")
            }
            if time - lastTime > 0.6 { reset() }
        }
        if let previous = distances.last,
           abs(distance - previous.value) > max(2, previous.value * 0.15) {
            reset()
            return RiskSnapshot(reason: "Distance changed abruptly. Check the selected vehicle.")
        }
        lastTime = time
        distances.append(TimedValue(time: time, value: distance))
        distances.removeAll { time - $0.time > 1.5 }
        guard let rangeRate = DriveMath.slope(distances, minimumSpan: 0.9) else {
            return RiskSnapshot(distance: distance, reason: "Collecting distance samples…")
        }
        let speed = egoSpeed + rangeRate
        guard speed >= -2, speed <= 90, abs(rangeRate) <= 60 else {
            reset()
            return RiskSnapshot(reason: "Tracking and GPS disagree. Reselect the target.")
        }
        leadSpeeds.append(TimedValue(time: time, value: max(0, speed)))
        leadSpeeds.removeAll { time - $0.time > 2 }
        let acceleration = DriveMath.slope(leadSpeeds, minimumSpan: 1.4)
        return RiskRules.evaluate(distance: distance, egoSpeed: egoSpeed,
                                  rangeRate: rangeRate, leadAcceleration: acceleration)
    }
}
