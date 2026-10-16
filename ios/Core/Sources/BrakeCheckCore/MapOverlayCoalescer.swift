import Foundation

/// Avoid replacing MapKit's immutable accuracy overlay for sub-meter noise.
/// Renderers add five meters: three for center lag and two for radius lag.
public struct MapOverlayCoalescer {
    public private(set) var center: NavigationCoordinate?
    public private(set) var radius: Double = 0
    public init() {}
    public mutating func reset() { center = nil; radius = 0 }
    public mutating func accept(center next: NavigationCoordinate, radius nextRadius: Double) -> Bool {
        guard next.latitude.isFinite, next.longitude.isFinite, (-90...90).contains(next.latitude),
              (-180...180).contains(next.longitude), nextRadius.isFinite, nextRadius >= 0 else { return false }
        if let center, Self.distance(center, next) <= 3, abs(radius - nextRadius) <= 2 { return false }
        center = next; radius = nextRadius
        return true
    }
    public static func distance(_ first: NavigationCoordinate, _ second: NavigationCoordinate) -> Double {
        let north = (second.latitude - first.latitude) * 111_195
        let longitude = (second.longitude - first.longitude + 540).truncatingRemainder(dividingBy: 360) - 180
        let east = longitude * 111_195 * cos((first.latitude + second.latitude) * .pi / 360)
        return hypot(north, east)
    }
}
