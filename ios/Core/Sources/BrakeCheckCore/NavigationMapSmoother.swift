import Foundation

/// Presentation only. Never feed these coordinates into guidance or collision estimates.
public struct NavigationMapSmoother {
    public private(set) var coordinate: NavigationCoordinate?
    private var target: NavigationCoordinate?
    private var raw: NavigationCoordinate?
    private var timestamp: Double?
    private var measuredTimestamp: Double?
    private var measuredCoordinate: NavigationCoordinate?
    private var estimated = false
    private var stale = false
    private var accuracy = 0.0
    public init() {}

    @discardableResult public mutating func ingest(_ fix: NavigationCoordinate, accuracy: Double,
        speed: Double, timestamp: Double, now: Double, isCurrent: Bool, isEstimated: Bool = false) -> Bool {
        guard fix.latitude.isFinite, fix.longitude.isFinite,
              (-90...90).contains(fix.latitude), (-180...180).contains(fix.longitude),
              accuracy.isFinite, (0...100).contains(accuracy), timestamp.isFinite, now.isFinite,
              now >= timestamp, !isCurrent || now - timestamp <= 3,
              !isEstimated || now - timestamp <= 1.5 else { return false }
        let isStale = !isCurrent && !isEstimated && now - timestamp > 3
        // Expiration is owned by the service. A return to its cached position
        // cancels the estimated target, even when that cached timestamp is older.
        if isStale {
            guard self.timestamp == nil || !stale || self.timestamp.map({ timestamp >= $0 }) == true else { return false }
            self.timestamp = timestamp; target = fix; coordinate = fix; raw = fix
            self.accuracy = max(5, accuracy); estimated = false; stale = true
            return true
        }
        if isEstimated {
            guard self.timestamp.map({ timestamp > $0 }) ?? true else { return false }
        } else {
            // A recovered measured fix can precede the wall-clock timestamp of
            // the most recent estimate; compare it with measured fixes only.
            guard measuredTimestamp.map({ timestamp > $0 }) ?? true else { return false }
        }
        self.accuracy = max(5, accuracy)
        raw = fix
        guard let previous = target, let previousTime = self.timestamp else {
            self.timestamp = timestamp; target = fix; coordinate = fix
            estimated = isEstimated; stale = false
            if !isEstimated { measuredTimestamp = timestamp; measuredCoordinate = fix }
            return true
        }
        let dt = timestamp - (isEstimated ? previousTime : measuredTimestamp ?? previousTime)
        let displacement = Self.distance(isEstimated ? previous : measuredCoordinate ?? previous, fix)
        let velocity = speed.isFinite && speed >= 0 ? speed : 0
        // Single implausible jumps inflate the uncertainty circle instead of moving the dot.
        if dt > 0, dt <= 5, displacement > max(15, velocity + 10) * dt + self.accuracy * 2 {
            return false
        }
        self.timestamp = timestamp
        let wasEstimated = estimated
        let wasStale = stale
        estimated = isEstimated; stale = false
        if !isEstimated { measuredTimestamp = timestamp; measuredCoordinate = fix }
        if wasStale || dt > 5 { target = fix; coordinate = fix; return true }
        // Service estimates already include motion prediction and uncertainty.
        // Interpolate toward the supplied point without filtering it as stationary
        // GPS or projecting beyond it. Expiration is still owned by the service.
        if isEstimated { target = fix; return true }
        if !wasEstimated, velocity < 2, Self.distance(previous, fix) <= max(3, min(10, accuracy * 0.5)) { return true }
        // One render-time filter is enough. Filtering the moving target as well
        // leaves both the marker and map tens of meters behind accepted GPS.
        target = fix
        return true
    }

    public mutating func advance(seconds: Double) -> NavigationCoordinate? {
        guard seconds.isFinite, seconds >= 0, let target, let coordinate else { return self.coordinate }
        self.coordinate = Self.interpolate(coordinate, target, fraction: 1 - exp(-min(seconds, 0.2) / 0.25))
        return self.coordinate
    }

    /// Rotation is independent of position filtering and takes the short path
    /// through north. It cannot create a geographic position or advance a route.
    public static func interpolatedHeading(from current: Double, toward target: Double, seconds: Double) -> Double {
        guard current.isFinite, target.isFinite, seconds.isFinite, seconds >= 0 else { return current }
        let delta = (target - current + 540).truncatingRemainder(dividingBy: 360) - 180
        return (current + delta * (1 - exp(-min(seconds, 0.2) / 0.35)) + 360)
            .truncatingRemainder(dividingBy: 360)
    }

    /// Includes rendering lag, so smoothing does not imply more precise GPS.
    public var uncertaintyRadius: Double {
        guard let coordinate, let raw else { return accuracy }
        return accuracy + Self.distance(coordinate, raw)
    }

    private static func interpolate(_ a: NavigationCoordinate, _ b: NavigationCoordinate, fraction: Double) -> NavigationCoordinate {
        let delta = (b.longitude - a.longitude + 540).truncatingRemainder(dividingBy: 360) - 180
        let longitude = (a.longitude + delta * fraction + 540).truncatingRemainder(dividingBy: 360) - 180
        return NavigationCoordinate(latitude: a.latitude + (b.latitude - a.latitude) * fraction, longitude: longitude)
    }
    private static func distance(_ a: NavigationCoordinate, _ b: NavigationCoordinate) -> Double {
        let lat = (b.latitude - a.latitude) * .pi / 180
        let lon = (b.longitude - a.longitude) * .pi / 180
        let h = pow(sin(lat / 2), 2) + cos(a.latitude * .pi / 180) * cos(b.latitude * .pi / 180) * pow(sin(lon / 2), 2)
        return 6_371_000 * 2 * asin(sqrt(min(1, max(0, h))))
    }
}
