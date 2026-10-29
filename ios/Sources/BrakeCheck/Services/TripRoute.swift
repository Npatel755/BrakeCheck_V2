import Foundation

/// Where a trip went and what happened on it, for the trip page's map and "What happened" list.
/// Saved only on this iPhone, one JSON file per trip next to the trip history, keyed by TripMetrics.id.
struct TripRoute: Codable {
    struct Point: Codable {
        let latitude: Double
        let longitude: Double
        let time: Date
    }

    enum Kind: String, Codable {
        case hardBrake, closeCall, brakeAlert

        var title: String {
            switch self {
            case .hardBrake: return "Car ahead braked hard"
            case .closeCall: return "Close call"
            case .brakeAlert: return "BRAKE alert"
            }
        }
    }

    struct Event: Codable, Identifiable {
        var id = UUID()
        let kind: Kind
        let time: Date
        let latitude: Double?
        let longitude: Double?
        let speedMPH: Double?
        /// Estimated time to collision when it was flagged, if known.
        let closingSeconds: Double?
    }

    var destination: String?
    var points: [Point] = []
    var events: [Event] = []

    static var folder: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("TripRoutes", isDirectory: true)
    }

    static func url(for tripID: UUID) -> URL { folder.appendingPathComponent("\(tripID.uuidString).json") }

    static func load(_ tripID: UUID) -> TripRoute? {
        guard let data = try? Data(contentsOf: url(for: tripID)) else { return nil }
        let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601
        return try? decoder.decode(TripRoute.self, from: data)
    }

    /// Removes every saved route (Settings › Recordings, alongside the speed logs).
    static func deleteAll() {
        try? FileManager.default.removeItem(at: folder)
    }
}

/// Collects a route during a trip. DriveModel feeds it GPS fixes and risk events on the main actor.
@MainActor
final class TripRouteRecorder {
    private var route = TripRoute()
    private var recording = false
    private var lastPoint: (time: Date, latitude: Double, longitude: Double)?
    /// Events of one kind closer together than this are one event (a flickering estimate).
    private let eventSpacing: TimeInterval = 8

    func start(destination: String?) {
        route = TripRoute(destination: destination)
        lastPoint = nil
        recording = true
    }

    func cancel() { recording = false; route = TripRoute() }

    /// One point every 5 seconds, skipped while stopped (under ~10 m of movement).
    func add(_ location: CrashLocation) {
        guard recording else { return }
        if let last = lastPoint {
            guard location.measuredAt.timeIntervalSince(last.time) >= 5 else { return }
            let dLat = (location.latitude - last.latitude) * 111_000
            let dLon = (location.longitude - last.longitude) * 111_000 * cos(location.latitude * .pi / 180)
            guard dLat * dLat + dLon * dLon >= 100 else { return }
        }
        route.points.append(.init(latitude: location.latitude, longitude: location.longitude, time: location.measuredAt))
        lastPoint = (location.measuredAt, location.latitude, location.longitude)
    }

    func add(_ kind: TripRoute.Kind, at location: CrashLocation?, speedMPH: Double?, closingSeconds: Double?) {
        guard recording else { return }
        let now = Date()
        if let previous = route.events.last(where: { $0.kind == kind }), now.timeIntervalSince(previous.time) < eventSpacing { return }
        let fresh = location.flatMap { now.timeIntervalSince($0.measuredAt) < 15 ? $0 : nil }
        route.events.append(.init(kind: kind, time: now, latitude: fresh?.latitude, longitude: fresh?.longitude,
                                  speedMPH: speedMPH, closingSeconds: closingSeconds))
    }

    func finish(tripID: UUID) {
        guard recording else { return }
        recording = false
        let encoder = JSONEncoder(); encoder.dateEncodingStrategy = .iso8601
        guard let data = try? encoder.encode(route) else { return }
        try? FileManager.default.createDirectory(at: TripRoute.folder, withIntermediateDirectories: true)
        try? data.write(to: TripRoute.url(for: tripID), options: [.atomic, .completeFileProtection])
        route = TripRoute()
    }
}
