import Foundation

/// Notices a camera that cannot see the road: frames with almost no brightness
/// variation (lens covered, fogged windshield, mount turned to the dash) for a
/// sustained spell while the car is moving on real GPS. A night road still has
/// headlit asphalt against a dark sky, so only near-uniform frames count. The
/// state holds while the car is stopped and clears on the first varied frame.
public struct CameraViewMonitor: Equatable, Sendable {
    public static let uniformSpread = 0.06
    public static let clearSpread = 0.12
    public static let confirmationSeconds: TimeInterval = 15
    public static let minimumSpeed = 5.0
    public static let maximumAccuracy = 15.0

    public private(set) var blocked = false
    private var uniformSince: TimeInterval?
    private var lastSampleAt: TimeInterval?

    public init() {}

    public mutating func reset() {
        blocked = false; uniformSince = nil; lastSampleAt = nil
    }

    @discardableResult
    public mutating func update(_ sample: DetectorLightingSample, speed: GPSSpeedSample?,
                                at time: TimeInterval) -> Bool {
        guard time.isFinite, sample.isUsable, let spread = sample.spread else {
            reset(); return false
        }
        if let last = lastSampleAt, time < last || time - last > 5 { uniformSince = nil }
        lastSampleAt = time
        if spread >= Self.clearSpread { blocked = false; uniformSince = nil; return false }
        guard let speed, !speed.simulated,
              [speed.metersPerSecond, speed.accuracy, speed.measurementUptime].allSatisfy(\.isFinite),
              speed.metersPerSecond >= Self.minimumSpeed, speed.accuracy <= Self.maximumAccuracy,
              (0...3).contains(time - speed.measurementUptime) else {
            uniformSince = nil; return blocked
        }
        guard spread <= Self.uniformSpread else { return blocked }
        guard let since = uniformSince else { uniformSince = time; return blocked }
        if time - since >= Self.confirmationSeconds { blocked = true }
        return blocked
    }
}
