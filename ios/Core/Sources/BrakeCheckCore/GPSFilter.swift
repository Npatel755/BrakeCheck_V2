import Foundation

public struct GPSFix {
    public let speed: Double
    public let speedAccuracy: Double
    public let horizontalAccuracy: Double
    public let timestamp: TimeInterval
    public let simulated: Bool

    public init(speed: Double, speedAccuracy: Double, horizontalAccuracy: Double,
                timestamp: TimeInterval, simulated: Bool = false) {
        self.speed = speed
        self.speedAccuracy = speedAccuracy
        self.horizontalAccuracy = horizontalAccuracy
        self.timestamp = timestamp
        self.simulated = simulated
    }
}

public struct GPSSpeedSample {
    public let metersPerSecond: Double
    public let accuracy: Double
    public let acceleration: Double?
    public let measurementUptime: TimeInterval
    public let simulated: Bool
    /// Setup verification uses a causal measured fix, independently of how
    /// the dashboard labels interpolation/holding between GPS updates.
    public func isFreshStationary(at time: TimeInterval) -> Bool {
        [time, measurementUptime, metersPerSecond, accuracy].allSatisfy(\.isFinite)
            && !simulated && (0...1.5).contains(time - measurementUptime)
            && (0..<0.5).contains(metersPerSecond) && (0...1.5).contains(accuracy)
    }
    public func isConfidentlyMoving(at time: TimeInterval) -> Bool {
        [time, measurementUptime, metersPerSecond, accuracy].allSatisfy(\.isFinite)
            && !simulated && (0...1.5).contains(time - measurementUptime)
            && (0...90).contains(metersPerSecond) && (0...8).contains(accuracy)
            && metersPerSecond - 2 * accuracy > 0.5
    }
}

/// GPS-only filter. Preserves original BrakeCheckCore interface.
/// When GPS cannot distinguish stationary from slow movement, it reports
/// unavailable instead of inventing a precise speed or forcing zero.
public struct GPSFilter {
    public private(set) var samples: [GPSSpeedSample] = []
    public private(set) var status = "Waiting for a GPS speed fix…"

    private var previousTimestamp: TimeInterval?
    private var filteredSpeed: Double?
    private var variance: Double = 4.0
    private var stationaryEvidence = 0
    private var movingEvidence = 0
    private var confirmedStationary = false
    private var history: [(time: TimeInterval, speed: Double)] = []

    private let maximumFixAge: TimeInterval = 2.0
    private let maximumSampleAge: TimeInterval = 1.5
    private let maximumSpeedAccuracy = 8.0
    private let maximumHorizontalAccuracy = 50.0
    private let processNoisePerSecond = 3.0

    public init() {}

    public mutating func reset() {
        samples.removeAll()
        history.removeAll()
        previousTimestamp = nil
        filteredSpeed = nil
        variance = 4.0
        stationaryEvidence = 0
        movingEvidence = 0
        confirmedStationary = false
        status = "Waiting for a GPS speed fix…"
    }

    @discardableResult
    public mutating func ingest(_ fix: GPSFix, wallNow: TimeInterval,
                                uptimeNow: TimeInterval) -> GPSSpeedSample? {
        let age = wallNow - fix.timestamp
        guard [fix.speed, fix.speedAccuracy, fix.horizontalAccuracy, fix.timestamp,
               wallNow, uptimeNow].allSatisfy(\.isFinite),
              (-0.5...maximumFixAge).contains(age),
              (0...90).contains(fix.speed),
              (0...maximumSpeedAccuracy).contains(fix.speedAccuracy),
              (0...maximumHorizontalAccuracy).contains(fix.horizontalAccuracy),
              !fix.simulated else {
            status = "GPS fix invalid or too old"
            return nil
        }

        if let previousTimestamp, fix.timestamp <= previousTimestamp {
            return sample(at: uptimeNow)
        }

        let delta = previousTimestamp.map { fix.timestamp - $0 } ?? 1
        if delta > 3 {
            filteredSpeed = nil
            variance = 4
            stationaryEvidence = 0
            movingEvidence = 0
            confirmedStationary = false
            history.removeAll()
            samples.removeAll()
        }
        previousTimestamp = fix.timestamp
        let time = uptimeNow - max(0, age)

        // Speed uncertainty is in m/s. Low speed whose uncertainty overlaps
        // zero is NOT evidence of movement, even if Core Location says 7 mph.
        let nearZero = fix.speed <= 0.65 && fix.speedAccuracy <= 1.5
        let confidentlyMoving = fix.speed - 2 * fix.speedAccuracy > 1.2
        let ambiguousLowSpeed = fix.speed <= 5.0 && !confidentlyMoving

        if nearZero {
            stationaryEvidence = min(5, stationaryEvidence + 1)
            movingEvidence = 0
        } else if confidentlyMoving {
            movingEvidence = min(5, movingEvidence + 1)
            stationaryEvidence = 0
        } else {
            // Uncertain GPS is not sufficient to declare movement.
            movingEvidence = 0
            stationaryEvidence = max(0, stationaryEvidence - 1)
        }

        if stationaryEvidence >= 3 { confirmedStationary = true }
        if movingEvidence >= 2 { confirmedStationary = false }
        // A prior stop is not evidence that a new, nonstationary fix is zero.
        // Withhold ambiguous departure speeds instead of latching zero until
        // two high-confidence moving fixes happen to arrive.
        if confirmedStationary && !nearZero {
            confirmedStationary = false
            stationaryEvidence = 0
            filteredSpeed = nil
            history.removeAll()
            samples.removeAll()
        }

        // If the reported low speed cannot be distinguished from noise,
        // explicitly withhold it rather than displaying false precision.
        if ambiguousLowSpeed && !confirmedStationary {
            samples.removeAll()
            filteredSpeed = nil
            history.removeAll()
            status = "Low-speed GPS uncertain; waiting for confirmation"
            return nil
        }

        let estimate: Double
        if confirmedStationary {
            estimate = 0
            filteredSpeed = 0
            variance = max(0.05, pow(max(0.4, fix.speedAccuracy), 2))
        } else {
            let measurementVariance = pow(max(0.5, fix.speedAccuracy), 2)
            if let old = filteredSpeed {
                let predictedVariance = variance + processNoisePerSecond * max(0.05, min(delta, 3))
                let innovation = fix.speed - old
                let innovationSD = sqrt(predictedVariance + measurementVariance)
                if abs(innovation) > max(10.0, 4 * innovationSD) && fix.speedAccuracy > 2.0 {
                    status = "Implausible GPS speed jump ignored"
                    return nil
                }
                let gain = predictedVariance / (predictedVariance + measurementVariance)
                estimate = max(0, old + gain * innovation)
                variance = max(0.05, (1 - gain) * predictedVariance)
            } else {
                estimate = fix.speed
                variance = measurementVariance
            }
            filteredSpeed = estimate
        }

        history.append((time: time, speed: estimate))
        history.removeAll { time - $0.time > 3 || $0.time > time }
        var acceleration: Double?
        if let first = history.first, let last = history.last {
            let span = last.time - first.time
            if span >= 1.0 {
                let a = (last.speed - first.speed) / span
                if a.isFinite && abs(a) <= 10 { acceleration = a }
            }
        }
        let reading = GPSSpeedSample(metersPerSecond: estimate,
                                     accuracy: sqrt(max(variance, 0.05)),
                                     acceleration: acceleration,
                                     measurementUptime: time,
                                     simulated: false)
        samples.append(reading)
        samples.removeAll { time - $0.measurementUptime > 3 }
        status = confirmedStationary
            ? "Stationary (GPS-confirmed; provisional)"
            : String(format: "GPS speed ±%.1f m/s", reading.accuracy)
        return reading
    }

    /// Only measurement-time-aligned readings may be used for risk calculations.
    public func sample(at uptime: TimeInterval) -> GPSSpeedSample? {
        samples.last { reading in
            let age = uptime - reading.measurementUptime
            return age >= 0 && age <= maximumSampleAge
        }
    }
}
