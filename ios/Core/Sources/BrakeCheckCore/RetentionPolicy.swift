import Foundation

public struct VideoSegment: Codable, Equatable {
    public let filename: String
    public let start: Date
    public let end: Date
    public init(filename: String, start: Date, end: Date) {
        self.filename = filename; self.start = start; self.end = end
    }

    public var valid: Bool {
        !filename.isEmpty && filename != "." && filename != ".."
            && !filename.contains("/") && !filename.contains("\\")
            && !filename.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) })
            && (filename as NSString).pathExtension.lowercased() == "mp4"
            && RecordingMetadata.validDate(start) && RecordingMetadata.validDate(end) && end >= start
    }
}

public struct IncidentManifest: Codable, Identifiable {
    public let id: UUID
    public let tripID: UUID?
    public let triggerTime: Date
    public let reason: String
    public let requestedStart: Date
    public let requestedEnd: Date
    public var segments: [VideoSegment] = []
    public var notes: [String] = []
    public var state = "Collecting available footage"

    public init(at date: Date, reason: String, tripID: UUID? = nil) {
        id = UUID()
        self.tripID = tripID
        triggerTime = date
        self.reason = reason
        requestedStart = date.addingTimeInterval(-RetentionPolicy.preEventSeconds)
        requestedEnd = date.addingTimeInterval(RetentionPolicy.postEventSeconds)
    }

    public var valid: Bool {
        validWindow
            && segments.allSatisfy(\.valid) && Set(segments.map(\.filename)).count == segments.count
    }

    fileprivate var validWindow: Bool {
        [triggerTime, requestedStart, requestedEnd].allSatisfy(RecordingMetadata.validDate)
            && requestedStart <= triggerTime && triggerTime <= requestedEnd
    }
}

private enum RecordingMetadata {
    static func validDate(_ date: Date) -> Bool {
        (-62_135_596_800...253_402_300_799).contains(date.timeIntervalSince1970)
    }
}

public enum RetentionPolicy {
    public static let preEventSeconds: TimeInterval = 15 * 60
    public static let postEventSeconds: TimeInterval = 15 * 60
    public static let segmentSeconds: TimeInterval = 10

    public static func overlaps(_ segment: VideoSegment, _ event: IncidentManifest) -> Bool {
        segment.valid && event.validWindow && segment.end >= event.requestedStart && segment.start <= event.requestedEnd
    }

    public static func shouldDeleteFromRing(_ segment: VideoSegment, now: Date) -> Bool {
        segment.valid && RecordingMetadata.validDate(now) && segment.end < now.addingTimeInterval(-preEventSeconds)
    }
}
