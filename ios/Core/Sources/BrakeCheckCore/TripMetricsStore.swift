import Foundation

public struct TripMetrics: Codable, Identifiable, Sendable {
    public let id: UUID
    public let startedAt: Date
    public let endedAt: Date
    public let maxSpeedMPH: Double?
    public let estimatedLeadHardBrakes: Int
    public let estimatedCloseCalls: Int
    /// Times the driver's own GPS deceleration showed hard braking; nil for trips saved before it was measured.
    public let ownHardBrakes: Int?
    public var durationSeconds: Double { endedAt.timeIntervalSince(startedAt) }

    public init(id: UUID = UUID(), startedAt: Date, endedAt: Date, maxSpeedMPH: Double?,
                estimatedLeadHardBrakes: Int, estimatedCloseCalls: Int, ownHardBrakes: Int? = nil) {
        self.id = id; self.startedAt = startedAt; self.endedAt = endedAt
        self.maxSpeedMPH = maxSpeedMPH
        self.estimatedLeadHardBrakes = estimatedLeadHardBrakes
        self.estimatedCloseCalls = estimatedCloseCalls
        self.ownHardBrakes = ownHardBrakes
    }
    /// Lead-car and own hard brakes together, as shown in trip totals.
    public var totalHardBrakes: Int { estimatedLeadHardBrakes + (ownHardBrakes ?? 0) }
    var valid: Bool {
        [startedAt, endedAt].allSatisfy { (-62_135_596_800...253_402_300_799).contains($0.timeIntervalSince1970) }
            && durationSeconds.isFinite && durationSeconds >= 0 && estimatedLeadHardBrakes >= 0 && estimatedCloseCalls >= 0
            && (maxSpeedMPH.map { $0.isFinite && $0 >= 0 } ?? true) && (ownHardBrakes ?? 0) >= 0
    }

    /// Explicit identity is authoritative for new incidents, even if the
    /// phone clock changed mid-drive. Older manifests keep timestamp matching.
    public func includes(_ incident: IncidentManifest) -> Bool {
        guard valid, incident.valid else { return false }
        if let tripID = incident.tripID { return tripID == id }
        return incident.triggerTime >= startedAt && incident.triggerTime <= endedAt
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
        let decoder = RecordingMetadataCodec.decoder()
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
        let encoder = RecordingMetadataCodec.encoder()
        let data = try encoder.encode(trips)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: url, options: .atomic)
        return trips
    }
}
