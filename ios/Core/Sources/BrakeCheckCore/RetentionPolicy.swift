import Foundation

public struct VideoSegment: Codable, Equatable {
    public let filename: String
    public let start: Date
    public let end: Date
    public init(filename: String, start: Date, end: Date) {
        self.filename = filename; self.start = start; self.end = end
    }
}

public struct IncidentManifest: Codable, Identifiable {
    public let id: UUID
    public let triggerTime: Date
    public let reason: String
    public let requestedStart: Date
    public let requestedEnd: Date
    public var segments: [VideoSegment] = []
    public var notes: [String] = []
    public var state = "Collecting available footage"

    public init(at date: Date, reason: String) {
        id = UUID()
        triggerTime = date
        self.reason = reason
        requestedStart = date.addingTimeInterval(-RetentionPolicy.preEventSeconds)
        requestedEnd = date.addingTimeInterval(RetentionPolicy.postEventSeconds)
    }
}

public enum RetentionPolicy {
    public static let preEventSeconds: TimeInterval = 15 * 60
    public static let postEventSeconds: TimeInterval = 15 * 60
    public static let segmentSeconds: TimeInterval = 10

    public static func overlaps(_ segment: VideoSegment, _ event: IncidentManifest) -> Bool {
        segment.end >= event.requestedStart && segment.start <= event.requestedEnd
    }

    public static func shouldDeleteFromRing(_ segment: VideoSegment, now: Date) -> Bool {
        segment.end < now.addingTimeInterval(-preEventSeconds)
    }
}
