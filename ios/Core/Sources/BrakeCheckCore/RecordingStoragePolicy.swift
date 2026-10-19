import Foundation

public enum RecordingStoragePolicy {
    public static let allowedLimitsGB = [0, 1, 2, 5, 10]
    public static let allowedRetentionDays = [0, 7, 30, 90]
    public static func limitBytes(gigabytes: Int) -> Int64? {
        guard allowedLimitsGB.contains(gigabytes), gigabytes > 0 else { return nil }
        return Int64(gigabytes) * 1_000_000_000
    }
    public static func shouldDelete(_ event: IncidentManifest, now: Date, retentionDays: Int,
                                    protectedIDs: Set<UUID>) -> Bool {
        guard allowedRetentionDays.contains(retentionDays), retentionDays > 0,
              !protectedIDs.contains(event.id),
              !event.state.hasPrefix("Collecting"), event.requestedEnd < now else { return false }
        return event.triggerTime < now.addingTimeInterval(-Double(retentionDays) * 86_400)
    }
}
