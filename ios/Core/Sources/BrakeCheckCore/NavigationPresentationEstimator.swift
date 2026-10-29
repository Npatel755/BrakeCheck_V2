import Foundation

public struct NavigationPresentationPosition: Equatable, Sendable {
    public let coordinate: NavigationCoordinate
    public let bearing: Double?
    public let uncertaintyRadius: Double
    public let isEstimated: Bool
    public let age: TimeInterval
}

/// Display-only continuity across a short GPS gap. This type has no progress,
/// maneuver, arrival or safety output. Consumers must keep its result out of
/// route tracking, speech and collision calculations.
public struct NavigationPresentationEstimator {
    private var coordinate: NavigationCoordinate?
    private var bearing: Double?
    private var speed = 0.0
    private var accuracy = 0.0
    private var timestamp: TimeInterval?
    public let maximumOutage: TimeInterval
    public let maximumDistance: Double
    public init(maximumOutage: TimeInterval = 4, maximumDistance: Double = 80) {
        self.maximumOutage = maximumOutage.isFinite ? min(5, max(0, maximumOutage)) : 4
        self.maximumDistance = maximumDistance.isFinite ? min(100, max(0, maximumDistance)) : 80
    }
    @discardableResult public mutating func ingest(coordinate: NavigationCoordinate, bearing: Double?, speed: Double,
        accuracy: Double, timestamp: TimeInterval, now: TimeInterval) -> Bool {
        guard coordinate.isValid, timestamp.isFinite, now.isFinite, timestamp >= 0,
              (0...2).contains(now - timestamp), accuracy.isFinite, (0...35).contains(accuracy),
              self.timestamp.map({ timestamp > $0 }) ?? true else { return false }
        if let previous = self.coordinate, let previousTime = self.timestamp {
            guard previous.distance(to: coordinate) <= 65 * (timestamp - previousTime) + max(25, accuracy + self.accuracy) else { return false }
        }
        self.coordinate = coordinate
        self.bearing = bearing.flatMap { $0.isFinite && (0..<360).contains($0) ? $0 : nil }
        self.speed = speed.isFinite && (0...65).contains(speed) ? speed : 0
        self.accuracy = accuracy; self.timestamp = timestamp
        return true
    }
    public func estimate(now: TimeInterval) -> NavigationPresentationPosition? {
        guard now.isFinite, let timestamp, let coordinate, now >= timestamp else { return nil }
        let age = now - timestamp
        guard age <= maximumOutage else { return nil }
        // The first second is the last measured position. Extrapolation is only
        // enabled with a trustworthy moving course; it is explicitly estimated.
        let elapsed = max(0, age - 1)
        let travel = bearing != nil && speed >= 2 && accuracy <= 20 ? min(maximumDistance, speed * elapsed) : 0
        let projected: NavigationCoordinate
        if let bearing, travel > 0 {
            let angular = travel / 6_371_008.8
            let heading = bearing * .pi / 180
            let latitude = coordinate.latitude * .pi / 180
            let longitude = coordinate.longitude * .pi / 180
            let nextLatitude = asin(sin(latitude) * cos(angular) + cos(latitude) * sin(angular) * cos(heading))
            let nextLongitude = longitude + atan2(sin(heading) * sin(angular) * cos(latitude),
                cos(angular) - sin(latitude) * sin(nextLatitude))
            projected = NavigationCoordinate(latitude: nextLatitude * 180 / .pi,
                longitude: (nextLongitude * 180 / .pi + 540).truncatingRemainder(dividingBy: 360) - 180)
        } else { projected = coordinate }
        return NavigationPresentationPosition(coordinate: projected, bearing: bearing,
            uncertaintyRadius: accuracy + age * max(3, speed * 0.35) + travel * 0.2,
            isEstimated: age > 1, age: age)
    }
    public mutating func reset() { coordinate = nil; bearing = nil; timestamp = nil; speed = 0; accuracy = 0 }
}
