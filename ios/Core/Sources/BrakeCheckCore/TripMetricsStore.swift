import Foundation

public struct TripMetrics: Codable, Identifiable {
    public let id: UUID
    public let startedAt: Date
    public let endedAt: Date
    public let maxSpeedMPH: Double?
    public let estimatedLeadHardBrakes: Int
    public let estimatedCloseCalls: Int
    public var durationSeconds: Double { endedAt.timeIntervalSince(startedAt) }

    public init(id: UUID = UUID(), startedAt: Date, endedAt: Date, maxSpeedMPH: Double?,
                estimatedLeadHardBrakes: Int, estimatedCloseCalls: Int) {
        self.id = id; self.startedAt = startedAt; self.endedAt = endedAt
        self.maxSpeedMPH = maxSpeedMPH
        self.estimatedLeadHardBrakes = estimatedLeadHardBrakes
        self.estimatedCloseCalls = estimatedCloseCalls
    }
    fileprivate var valid: Bool {
        startedAt.timeIntervalSince1970.isFinite && endedAt.timeIntervalSince1970.isFinite
            && durationSeconds >= 0 && estimatedLeadHardBrakes >= 0 && estimatedCloseCalls >= 0
            && (maxSpeedMPH.map { $0.isFinite && $0 >= 0 } ?? true)
    }
}

/// Local, atomic history storage. Invalid/corrupt history is reported rather
/// than overwritten; appending the same trip ID is safe to retry.
public struct TripMetricsStore {
    public let url: URL
    public init(url: URL) { self.url = url }
    public enum StoreError: Error { case invalidMetrics }
    public func load() throws -> [TripMetrics] {
        guard FileManager.default.fileExists(atPath: url.path) else { return [] }
        let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601
        let trips = try decoder.decode([TripMetrics].self, from: Data(contentsOf: url))
        guard trips.allSatisfy(\.valid), Set(trips.map(\.id)).count == trips.count else {
            throw StoreError.invalidMetrics
        }
        return trips.sorted { $0.startedAt > $1.startedAt }
    }
    public func save(_ trip: TripMetrics) throws -> [TripMetrics] {
        guard trip.valid else { throw StoreError.invalidMetrics }
        var trips = try load()
        trips.removeAll { $0.id == trip.id }
        trips.append(trip); trips.sort { $0.startedAt > $1.startedAt }
        let encoder = JSONEncoder(); encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(trips)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: url, options: .atomic)
        return trips
    }
}
