import Foundation

public struct NavigationRoutePreferences: Codable, Equatable, Sendable {
    public var avoidTolls: Bool
    public var avoidMotorways: Bool
    public init(avoidTolls: Bool = false, avoidMotorways: Bool = false) {
        self.avoidTolls = avoidTolls; self.avoidMotorways = avoidMotorways
    }
}

public struct NavigationRouteStop: Codable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public var title: String
    public var coordinate: NavigationCoordinate
    public var dwellTime: TimeInterval
    /// Measured route progress required to reach this routed stop, in provider
    /// distance units. Older saved routes may have no recorded milestone.
    public var routeDistanceFromStart: Double?
    public init(id: UUID = UUID(), title: String, coordinate: NavigationCoordinate, dwellTime: TimeInterval = 0,
                routeDistanceFromStart: Double? = nil) {
        self.id = id; self.title = title; self.coordinate = coordinate; self.dwellTime = dwellTime
        self.routeDistanceFromStart = routeDistanceFromStart
    }
    public var isValid: Bool {
        coordinate.isValid && dwellTime.isFinite && (0...86_400).contains(dwellTime)
            && (routeDistanceFromStart.map { $0.isFinite && $0 >= 0 } ?? true)
    }
}

public struct NavigationRouteStep: Codable, Equatable, Sendable {
    public var instruction: String
    public var distance: Double
    public var expectedTravelTime: TimeInterval
    public var coordinates: [NavigationCoordinate]
    public var notice: String?
    public init(instruction: String, distance: Double, expectedTravelTime: TimeInterval = 0,
                coordinates: [NavigationCoordinate] = [], notice: String? = nil) {
        self.instruction = instruction; self.distance = distance; self.expectedTravelTime = expectedTravelTime
        self.coordinates = coordinates; self.notice = notice
    }
    public var isValid: Bool {
        distance.isFinite && distance >= 0 && expectedTravelTime.isFinite && expectedTravelTime >= 0
            && coordinates.allSatisfy(\.isValid)
    }
}

/// Route geometry and instructions can be followed without a network. An offline
/// map or a road graph is separate data and is never implied by a saved route.
public struct SavedNavigationRoute: Codable, Equatable, Identifiable, Sendable {
    public var id: UUID
    public var title: String
    public var coordinates: [NavigationCoordinate]
    public var steps: [NavigationRouteStep]
    public var distance: Double
    public var expectedTravelTime: TimeInterval
    public var createdAt: Date
    public var source: String
    public var stops: [NavigationRouteStop]
    public var preferences: NavigationRoutePreferences
    public init(id: UUID = UUID(), title: String, coordinates: [NavigationCoordinate], steps: [NavigationRouteStep],
                distance: Double, expectedTravelTime: TimeInterval, createdAt: Date = Date(),
                source: String = "Apple Maps", stops: [NavigationRouteStop] = [],
                preferences: NavigationRoutePreferences = .init()) {
        self.id = id; self.title = title; self.coordinates = coordinates; self.steps = steps
        self.distance = distance; self.expectedTravelTime = expectedTravelTime; self.createdAt = createdAt
        self.source = source; self.stops = stops; self.preferences = preferences
    }
    public var isValid: Bool {
        coordinates.count >= 2 && coordinates.count <= 500_000 && coordinates.allSatisfy(\.isValid)
            && zip(coordinates, coordinates.dropFirst()).contains { $0.distance(to: $1) > 0.1 }
            && distance.isFinite && distance > 0 && expectedTravelTime.isFinite && expectedTravelTime >= 0
            && steps.allSatisfy(\.isValid) && stops.allSatisfy(\.isValid)
            && stops.compactMap(\.routeDistanceFromStart).allSatisfy { $0 <= distance + 1 }
            && zip(stops.compactMap(\.routeDistanceFromStart), stops.compactMap(\.routeDistanceFromStart).dropFirst())
                .allSatisfy { $0 <= $1 }
            && createdAt.timeIntervalSince1970.isFinite
    }
    public func progressTracker() -> NavigationProgressTracker {
        NavigationProgressTracker(coordinates: coordinates, routeDistance: distance, stepDistances: steps.map(\.distance))
    }
    /// Stable across launches; unlike Swift Hasher this can key local history.
    public var historyKey: String {
        guard let start = coordinates.first, let end = coordinates.last,
              coordinates.allSatisfy(\.isValid), stops.allSatisfy(\.isValid) else { return "invalid" }
        func quantized(_ point: NavigationCoordinate) -> String {
            "\(Int((point.latitude * 1_000).rounded())):\(Int((point.longitude * 1_000).rounded()))"
        }
        let intermediateStops = stops.filter {
            $0.routeDistanceFromStart.map { $0 < distance - 1 } ?? ($0.coordinate.distance(to: end) > 1)
        }.map { quantized($0.coordinate) }.joined(separator: ";")
        // Sample by geometric distance, not vertex index, so adding vertices on
        // the same path does not assign its history to a different route.
        let lengths = zip(coordinates, coordinates.dropFirst()).map { $0.distance(to: $1) }
        let total = lengths.reduce(0, +)
        guard total.isFinite, total > 0 else { return "invalid" }
        var samples: [String] = []
        var index = 0
        var offset = 0.0
        for sample in 0...16 {
            let target = total * Double(sample) / 16
            while index < lengths.count - 1, offset + lengths[index] < target {
                offset += lengths[index]; index += 1
            }
            let fraction = lengths[index] > 0 ? min(1, max(0, (target - offset) / lengths[index])) : 0
            let a = coordinates[index], b = coordinates[index + 1]
            let longitudeDelta = (b.longitude - a.longitude + 540).truncatingRemainder(dividingBy: 360) - 180
            samples.append(quantized(NavigationCoordinate(latitude: a.latitude + (b.latitude - a.latitude) * fraction,
                longitude: (a.longitude + longitudeDelta * fraction + 540).truncatingRemainder(dividingBy: 360) - 180)))
        }
        // FNV-1a supplies a bounded deterministic geometry fingerprint.
        var fingerprint: UInt64 = 14_695_981_039_346_656_037
        for byte in samples.joined(separator: ";").utf8 { fingerprint = (fingerprint ^ UInt64(byte)) &* 1_099_511_628_211 }
        return "v2:\(quantized(start)):\(quantized(end)):via=\(intermediateStops):path=\(String(fingerprint, radix: 16))"
            + ":\(preferences.avoidTolls):\(preferences.avoidMotorways)"
    }
}

public enum NavigationRouteArchive {
    public static func decode(_ data: Data) throws -> [SavedNavigationRoute] {
        guard data.count <= 32_000_000 else { throw NavigationArchiveError.tooLarge }
        let routes = try JSONDecoder().decode([SavedNavigationRoute].self, from: data)
        guard routes.count <= 100, routes.allSatisfy(\.isValid) else { throw NavigationArchiveError.invalidRoute }
        return routes
    }
    public static func encode(_ routes: [SavedNavigationRoute]) throws -> Data {
        guard routes.count <= 100, routes.allSatisfy(\.isValid) else { throw NavigationArchiveError.invalidRoute }
        let data = try JSONEncoder().encode(routes)
        guard data.count <= 32_000_000 else { throw NavigationArchiveError.tooLarge }
        return data
    }
}
public enum NavigationArchiveError: Error { case tooLarge, invalidRoute }
