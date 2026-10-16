import Foundation

/// Presentation only. Never feed these coordinates into guidance or collision estimates.
public struct NavigationMapSmoother {
    public private(set) var coordinate: NavigationCoordinate?
    private var target: NavigationCoordinate?
    private var raw: NavigationCoordinate?
    private var timestamp: Double?
    private var accuracy = 0.0
    public init() {}

    @discardableResult public mutating func ingest(_ fix: NavigationCoordinate, accuracy: Double,
        speed: Double, timestamp: Double, now: Double, isCurrent: Bool) -> Bool {
        guard fix.latitude.isFinite, fix.longitude.isFinite,
              (-90...90).contains(fix.latitude), (-180...180).contains(fix.longitude),
              accuracy.isFinite, (0...100).contains(accuracy), timestamp.isFinite, now.isFinite,
              now >= timestamp, !isCurrent || now - timestamp <= 3,
              self.timestamp.map({ timestamp > $0 }) ?? true else { return false }
        self.accuracy = max(5, accuracy)
        raw = fix
        guard let previous = target, let previousTime = self.timestamp else {
            self.timestamp = timestamp; target = fix; coordinate = fix; return true
        }
        let dt = timestamp - previousTime
        let displacement = Self.distance(previous, fix)
        let velocity = speed.isFinite && speed >= 0 ? speed : 0
        // Single implausible jumps inflate the uncertainty circle instead of moving the dot.
        if (isCurrent || now - timestamp <= 3) && dt <= 5 && displacement > max(15, velocity + 10) * dt + self.accuracy * 2 {
            return false
        }
        self.timestamp = timestamp
        if dt > 15 {
            target = fix; coordinate = fix; return true
        }
        if velocity < 2 && displacement <= max(3, min(10, accuracy * 0.5)) { return true }
        let response = velocity >= 2 ? 0.7 + accuracy / 50 : 2.0 + accuracy / 25
        target = Self.interpolate(previous, fix, fraction: 1 - exp(-dt / response))
        return true
    }

    public mutating func advance(seconds: Double) -> NavigationCoordinate? {
        guard seconds.isFinite, seconds >= 0, let target, let coordinate else { return self.coordinate }
        self.coordinate = Self.interpolate(coordinate, target, fraction: 1 - exp(-min(seconds, 0.2) / 0.35))
        return self.coordinate
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
