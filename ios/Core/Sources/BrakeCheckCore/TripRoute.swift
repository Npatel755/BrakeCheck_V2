import Foundation

/// A measured trip trail. GPS gaps remain separate paths, never invented road geometry.
public struct TripRoute: Codable, Sendable {
    public struct Point: Codable, Sendable {
        public let latitude: Double
        public let longitude: Double
        public let time: Date
        public init(latitude: Double, longitude: Double, time: Date) {
            self.latitude = latitude; self.longitude = longitude; self.time = time
        }
        var valid: Bool {
            latitude.isFinite && longitude.isFinite && (-90...90).contains(latitude)
                && (-180...180).contains(longitude) && TripRoute.validDate(time)
        }
    }
    public enum Kind: String, Codable, Sendable {
        case hardBrake, closeCall, brakeAlert
        public var title: String {
            switch self {
            case .hardBrake: return "Car ahead braked hard"
            case .closeCall: return "Close call"
            case .brakeAlert: return "BRAKE alert"
            }
        }
    }
    public struct Event: Codable, Identifiable, Sendable {
        public let id: UUID
        public let kind: Kind
        public let time: Date
        public let latitude: Double?
        public let longitude: Double?
        public let speedMPH: Double?
        public let closingSeconds: Double?
        public init(id: UUID = UUID(), kind: Kind, time: Date, latitude: Double?, longitude: Double?,
                    speedMPH: Double?, closingSeconds: Double?) {
            self.id = id; self.kind = kind; self.time = time
            self.latitude = latitude; self.longitude = longitude
            self.speedMPH = speedMPH; self.closingSeconds = closingSeconds
        }
        var valid: Bool {
            let locationValid = latitude == nil && longitude == nil || latitude.flatMap { lat in
                longitude.map { Point(latitude: lat, longitude: $0, time: time).valid }
            } == true
            return TripRoute.validDate(time) && locationValid
                && (speedMPH.map { $0.isFinite && (0...250).contains($0) } ?? true)
                && (closingSeconds.map { $0.isFinite && $0 >= 0 } ?? true)
        }
    }
    public var destination: String?
    public var points: [Point]
    public var events: [Event]
    public init(destination: String? = nil, points: [Point] = [], events: [Event] = []) {
        self.destination = destination; self.points = points; self.events = events
    }
    static func validDate(_ date: Date) -> Bool {
        (-62_135_596_800...253_402_300_799).contains(date.timeIntervalSince1970)
    }
    public var valid: Bool {
        points.count <= 100_000 && events.count <= 10_000 && (destination?.utf8.count ?? 0) <= 16_384
            && points.allSatisfy(\.valid) && events.allSatisfy(\.valid)
            && Set(events.map(\.id)).count == events.count
            && zip(points, points.dropFirst()).allSatisfy { $0.time < $1.time }
    }
    /// Gaps over 30 seconds and physically implausible jumps do not count as driven distance.
    /// Standstill sampling can omit a point for longer; that gap is intentionally left unknown.
    public var continuousSegments: [[Point]] {
        guard valid, let first = points.first else { return [] }
        var result: [[Point]] = []; var current = [first]
        for point in points.dropFirst() {
            let previous = current.last!
            let seconds = point.time.timeIntervalSince(previous.time)
            if seconds > 30 || Self.distance(previous, point) > max(30, seconds * 90) {
                result.append(current); current = []
            }
            current.append(point)
        }
        result.append(current)
        return result
    }
    public var distanceMeters: Double? {
        let total = continuousSegments.reduce(0.0) { total, segment in
            total + zip(segment, segment.dropFirst()).reduce(0.0) { $0 + Self.distance($1.0, $1.1) }
        }
        return total > 0 ? total : nil
    }
    public static func distance(_ a: Point, _ b: Point) -> Double {
        let lat1 = a.latitude * .pi / 180, lat2 = b.latitude * .pi / 180
        let dLat = lat2 - lat1, dLon = (b.longitude - a.longitude) * .pi / 180
        let h = sin(dLat / 2) * sin(dLat / 2) + cos(lat1) * cos(lat2) * sin(dLon / 2) * sin(dLon / 2)
        return 12_742_017.6 * asin(sqrt(min(1, max(0, h))))
    }
}

/// Bounded, validated in-memory collection. Invalid fixes cannot poison later observations.
public struct TripRouteAccumulator {
    public private(set) var route: TripRoute
    public init(destination: String? = nil) { route = TripRoute(destination: destination.map { String($0.prefix(4_000)) }) }
    public mutating func updateDestination(_ destination: String?) {
        route.destination = destination.map { String($0.prefix(4_000)) }
    }
    @discardableResult public mutating func add(_ point: TripRoute.Point) -> Bool {
        guard point.valid, route.points.count < 100_000 else { return false }
        if let last = route.points.last {
            guard point.time.timeIntervalSince(last.time) >= 5, TripRoute.distance(last, point) >= 10 else { return false }
        }
        route.points.append(point); return true
    }
    @discardableResult public mutating func add(_ kind: TripRoute.Kind, at now: Date,
        location: TripRoute.Point?, speedMPH: Double?, closingSeconds: Double?) -> Bool {
        guard TripRoute.validDate(now), route.events.count < 10_000 else { return false }
        if let previous = route.events.last(where: { $0.kind == kind }), now.timeIntervalSince(previous.time) < 8 { return false }
        let fresh = location.flatMap { $0.valid && (0...15).contains(now.timeIntervalSince($0.time)) ? $0 : nil }
        let speed = speedMPH.flatMap { $0.isFinite && (0...250).contains($0) ? $0 : nil }
        let closing = closingSeconds.flatMap { $0.isFinite && $0 >= 0 ? $0 : nil }
        route.events.append(.init(kind: kind, time: now, latitude: fresh?.latitude, longitude: fresh?.longitude,
                                 speedMPH: speed, closingSeconds: closing))
        return true
    }
}
