import Foundation

/// Retains the original timestamps of accepted GPS fixes. Delayed camera work
/// can select the newest valid fix at/before capture, without borrowing a
/// future measurement or altering its age. Owns no filtering or extrapolation.
public struct FrameAlignedSpeedHistory {
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
}
