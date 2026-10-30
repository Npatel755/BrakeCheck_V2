import Foundation

/// Ordered trip persistence. A durable journal preserves a completed trip until
/// both its route and history entry are saved; startup/retry can finish interrupted work.
public actor TripArchiveStore {
    public struct Snapshot: Sendable {
        public let trips: [TripMetrics]
        public let pendingCount: Int
        public let warnings: [String]
    }
    private struct Entry: Codable {
        let version: Int
        let trip: TripMetrics
        let route: TripRoute
    }
    public enum ArchiveError: LocalizedError {
        case invalidArchive, conflictingPendingTrip, conflictingRoute, conflictingMetrics, tooLarge
        public var errorDescription: String? {
            switch self {
            case .invalidArchive: return "The saved trip data could not be read safely. Original files were kept."
            case .conflictingPendingTrip: return "A different pending save already uses this trip ID. Original files were kept."
            case .conflictingRoute: return "This trip already has different route data. Original files were kept."
            case .conflictingMetrics: return "This trip already has different saved totals. Original files were kept."
            case .tooLarge: return "The trip archive exceeds the supported size. Original files were kept."
            }
        }
    }
    private let metrics: TripMetricsStore
    public let routesFolder: URL
    public let pendingFolder: URL
    public init(historyURL: URL, routesFolder: URL, pendingFolder: URL) {
        metrics = TripMetricsStore(url: historyURL)
        self.routesFolder = routesFolder; self.pendingFolder = pendingFolder
    }
    public func load() throws -> Snapshot {
        var warnings: [String] = []
        for path in try pendingFiles() {
            do {
                let entry = try decodeEntry(path)
                try commit(entry)
                try FileManager.default.removeItem(at: path)
            } catch { warnings.append("A trip is waiting to be saved: " + error.localizedDescription) }
        }
        return Snapshot(trips: try metrics.load(), pendingCount: try pendingFiles().count, warnings: warnings)
    }
    public func save(_ trip: TripMetrics, route: TripRoute) throws -> Snapshot {
        guard trip.valid, route.valid else { throw ArchiveError.invalidArchive }
        let entry = Entry(version: 1, trip: trip, route: route)
        let data = try RecordingMetadataCodec.encoder().encode(entry)
        guard data.count <= 32_000_000 else { throw ArchiveError.tooLarge }
        let journal = pendingFolder.appendingPathComponent(trip.id.uuidString + ".json")
        if FileManager.default.fileExists(atPath: journal.path) {
            let existing = try decodeEntry(journal)
            guard try RecordingMetadataCodec.encoder().encode(existing) == data else { throw ArchiveError.conflictingPendingTrip }
        } else { try write(data, to: journal) }
        try commit(entry)
        try FileManager.default.removeItem(at: journal)
        return try load()
    }
    public func loadRoute(_ id: UUID) throws -> TripRoute? {
        let path = routeURL(id)
        guard FileManager.default.fileExists(atPath: path.path) else { return nil }
        let route = try RecordingMetadataCodec.decoder().decode(TripRoute.self, from: read(path))
        guard route.valid else { throw ArchiveError.invalidArchive }
        return route
    }
    /// Runs on the same actor as saves/recovery so a pending write cannot recreate
    /// GPS trails after deletion. A failed recovery must be resolved before deleting.
    public func deleteRoutes() throws {
        guard try pendingFiles().isEmpty else { throw ArchiveError.conflictingPendingTrip }
        if FileManager.default.fileExists(atPath: routesFolder.path) {
            try FileManager.default.removeItem(at: routesFolder)
        }
    }
    private func routeURL(_ id: UUID) -> URL { routesFolder.appendingPathComponent(id.uuidString + ".json") }
    private func pendingFiles() throws -> [URL] {
        guard FileManager.default.fileExists(atPath: pendingFolder.path) else { return [] }
        return try FileManager.default.contentsOfDirectory(at: pendingFolder, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension.lowercased() == "json" }.sorted { $0.lastPathComponent < $1.lastPathComponent }
    }
    private func decodeEntry(_ path: URL) throws -> Entry {
        let entry = try RecordingMetadataCodec.decoder().decode(Entry.self, from: read(path))
        guard entry.version == 1, entry.trip.valid, entry.route.valid,
              path.deletingPathExtension().lastPathComponent == entry.trip.id.uuidString else { throw ArchiveError.invalidArchive }
        return entry
    }
    private func commit(_ entry: Entry) throws {
        // A recovered journal is a retry of one immutable completed trip. It
        // must never replace another saved trip merely because an ID matches.
        if let existing = try metrics.load().first(where: { $0.id == entry.trip.id }) {
            let encoder = RecordingMetadataCodec.encoder()
            guard try encoder.encode(existing) == encoder.encode(entry.trip) else { throw ArchiveError.conflictingMetrics }
        }
        if let existing = try loadRoute(entry.trip.id) {
            let encoder = RecordingMetadataCodec.encoder()
            guard try encoder.encode(existing) == encoder.encode(entry.route) else { throw ArchiveError.conflictingRoute }
        } else { try write(RecordingMetadataCodec.encoder().encode(entry.route), to: routeURL(entry.trip.id)) }
        _ = try metrics.save(entry.trip)
    }
    private func read(_ path: URL) throws -> Data {
        guard try path.resourceValues(forKeys: [.fileSizeKey]).fileSize.map({ $0 <= 32_000_000 }) == true else {
            throw ArchiveError.tooLarge
        }
        return try Data(contentsOf: path)
    }
    private func write(_ data: Data, to path: URL) throws {
        try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
        #if os(iOS)
        try data.write(to: path, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        #else
        try data.write(to: path, options: .atomic)
        #endif
    }
}
