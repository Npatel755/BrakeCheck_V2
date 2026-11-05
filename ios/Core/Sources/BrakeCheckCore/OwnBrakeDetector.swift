import Foundation

/// Detects the driver's own hard braking from measured GPS deceleration: two consecutive
/// real, accurate fixes at or below `threshold` (m/s², negative) while moving, then a
/// cooldown so one long stop counts once. Simulated or stale fixes never count.
public struct OwnBrakeDetector: Equatable, Sendable {
    public struct Event: Equatable, Sendable {
        public let peakDeceleration: Double
        public let speedAtOnset: Double
    }

    public let threshold: Double
    public let minimumSpeed: Double
    public let maximumAccuracy: Double
    public let cooldown: TimeInterval
    private var pending: (deceleration: Double, speed: Double, uptime: TimeInterval)?
    private var lastEventUptime: TimeInterval?

    public init(threshold: Double = -3.5, minimumSpeed: Double = 5, maximumAccuracy: Double = 15,
                cooldown: TimeInterval = 6) {
        self.threshold = threshold
        self.minimumSpeed = minimumSpeed
        self.maximumAccuracy = maximumAccuracy
        self.cooldown = cooldown
    }

    public static func == (lhs: OwnBrakeDetector, rhs: OwnBrakeDetector) -> Bool {
        lhs.threshold == rhs.threshold && lhs.minimumSpeed == rhs.minimumSpeed
            && lhs.maximumAccuracy == rhs.maximumAccuracy && lhs.cooldown == rhs.cooldown
            && lhs.pending?.uptime == rhs.pending?.uptime && lhs.lastEventUptime == rhs.lastEventUptime
    }

    public mutating func reset() {
        pending = nil
        lastEventUptime = nil
    }

    public mutating func update(_ sample: GPSSpeedSample?) -> Event? {
        guard let sample, !sample.simulated, let acceleration = sample.acceleration,
              [sample.metersPerSecond, sample.accuracy, acceleration, sample.measurementUptime].allSatisfy(\.isFinite),
              sample.accuracy <= maximumAccuracy else {
            pending = nil
            return nil
        }
        let now = sample.measurementUptime
        if let last = lastEventUptime, now - last < cooldown { pending = nil; return nil }
        guard acceleration <= threshold else { pending = nil; return nil }
        guard let first = pending else {
            guard sample.metersPerSecond >= minimumSpeed else { return nil }
            pending = (acceleration, sample.metersPerSecond, now)
            return nil
        }
        guard now - first.uptime <= 3 else {
            pending = (acceleration, sample.metersPerSecond, now)
            return nil
        }
        pending = nil
        lastEventUptime = now
        return Event(peakDeceleration: min(first.deceleration, acceleration), speedAtOnset: first.speed)
    }
}
