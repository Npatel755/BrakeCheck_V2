import Foundation

public enum GPSSpeedEstimateSource: String {
    case measured, interpolated, projected, held
}

/// A display estimate is deliberately separate from a measured GPS sample.
/// It cannot renew measurement age or become an input to direction/risk rules.
public struct GPSSpeedEstimate {
    public let metersPerSecond: Double
    public let accuracy: Double
    public let lastMeasurementUptime: TimeInterval
    public let evaluatedAt: TimeInterval
    public let source: GPSSpeedEstimateSource
    public var isEstimated: Bool { source != .measured }
}

/// Retains accepted measurement timestamps. `reading` remains causal and
/// measured-only. The separate display estimate can bridge a bounded gap, or
/// interpolate an earlier frame once both surrounding measurements exist.
public struct FrameAlignedSpeedHistory {
    public static let maximumEstimateAge: TimeInterval = 2.5
    private var samples: [GPSSpeedSample] = []
    public init() {}

    public mutating func reset() { samples.removeAll() }

    public mutating func append(_ sample: GPSSpeedSample) {
        guard sample.measurementUptime.isFinite, sample.metersPerSecond.isFinite,
              (0...90).contains(sample.metersPerSecond), sample.accuracy.isFinite,
              (0...8).contains(sample.accuracy), !sample.simulated,
              samples.last.map({ sample.measurementUptime > $0.measurementUptime }) ?? true else { return }
        samples.removeAll { sample.measurementUptime - $0.measurementUptime > 4 }
        samples.append(sample)
        if samples.count > 64 { samples.removeFirst(samples.count - 64) }
    }

    public func reading(at capture: TimeInterval, maximumAge: TimeInterval = 1.5) -> GPSSpeedSample? {
        guard capture.isFinite, maximumAge.isFinite, maximumAge >= 0 else { return nil }
        return samples.last {
            $0.measurementUptime <= capture && capture - $0.measurementUptime <= maximumAge
        }
    }

    public func estimate(at time: TimeInterval) -> GPSSpeedEstimate? {
        guard time.isFinite, let before = samples.last(where: { $0.measurementUptime <= time }) else { return nil }
        let age = time - before.measurementUptime
        guard age <= Self.maximumEstimateAge else { return nil }
        if age > 0, let after = samples.first(where: { $0.measurementUptime > time }) {
            let span = after.measurementUptime - before.measurementUptime
            // A later fix can refine an already-captured frame; it cannot
            // create an estimate before the first fix or bridge a long outage.
            if span <= Self.maximumEstimateAge, before.accuracy <= 3, after.accuracy <= 3,
               abs(after.metersPerSecond - before.metersPerSecond) / span <= 6 {
                let fraction = age / span
                let speed = before.metersPerSecond + fraction * (after.metersPerSecond - before.metersPerSecond)
                let uncertainty = max(before.accuracy, after.accuracy) + min(age, span - age)
                return GPSSpeedEstimate(metersPerSecond: speed, accuracy: uncertainty,
                    lastMeasurementUptime: before.measurementUptime, evaluatedAt: time, source: .interpolated)
            }
        }
        if age <= 1.5 {
            return GPSSpeedEstimate(metersPerSecond: before.metersPerSecond, accuracy: before.accuracy,
                lastMeasurementUptime: before.measurementUptime, evaluatedAt: time, source: .measured)
        }
        guard before.accuracy <= 3 else { return nil }
        let uncertainty = sqrt(before.accuracy * before.accuracy + pow(2 * age, 2))
        guard uncertainty <= 5 else { return nil }
        let recent = samples.filter {
            $0.measurementUptime <= before.measurementUptime
                && before.measurementUptime - $0.measurementUptime <= 3
        }
        var trend: Double?
        if recent.count >= 3, recent.allSatisfy({ $0.accuracy <= 2 }),
           let slope = DriveMath.slope(recent.map { TimedValue(time: $0.measurementUptime, value: $0.metersPerSecond) }, minimumSpan: 1),
           abs(slope) <= 3, let first = recent.first,
           zip(recent, recent.dropFirst()).allSatisfy({ pair in pair.1.measurementUptime - pair.0.measurementUptime <= 1.5 }) {
            let meanTime = recent.reduce(0) { $0 + $1.measurementUptime - first.measurementUptime } / Double(recent.count)
            let meanSpeed = recent.reduce(0) { $0 + $1.metersPerSecond } / Double(recent.count)
            let residual = sqrt(recent.reduce(0) { sum, sample in
                let fitted = meanSpeed + slope * (sample.measurementUptime - first.measurementUptime - meanTime)
                return sum + pow(sample.metersPerSecond - fitted, 2)
            } / Double(recent.count))
            if residual <= 0.75 { trend = slope }
        }
        let change = DriveMath.clamp((trend ?? 0) * age, -3, 3)
        let speed = DriveMath.clamp(before.metersPerSecond + change, 0, 90)
        return GPSSpeedEstimate(metersPerSecond: speed, accuracy: uncertainty,
            lastMeasurementUptime: before.measurementUptime, evaluatedAt: time,
            source: trend == nil ? .held : .projected)
    }
}

/// Display-only absolute lead speed. Requires a fresh, fitted camera gap on
/// the same confirmed rear vehicle; never reuses a held lead speed or emits
/// acceleration/headway/risk from an estimated GPS value.
public enum GPSSpeedDisplayRules {
    public static func estimatedLeadSpeed(ego: GPSSpeedEstimate?, camera: CameraRangeSnapshot,
                                          confidence: Double, rearConfirmed: Bool,
                                          capturedAt: TimeInterval, now: TimeInterval) -> Double? {
        guard rearConfirmed, let ego, ego.evaluatedAt == capturedAt,
              ego.accuracy.isFinite, (0...5).contains(ego.accuracy),
              TrackingFreshness.measurementIsFresh(capturedAt: capturedAt, now: now),
              confidence.isFinite, (0.80...1).contains(confidence),
              let distance = camera.distance, distance.isFinite, (1...150).contains(distance),
              let rate = camera.rangeRate, rate.isFinite, abs(rate) <= 60,
              let closing = camera.closingSpeed, closing.isFinite,
              abs(closing - max(0, -rate)) <= 0.001 else { return nil }
        let speed = ego.metersPerSecond + rate
        return (-2...90).contains(speed) ? max(0, speed) : nil
    }
}
