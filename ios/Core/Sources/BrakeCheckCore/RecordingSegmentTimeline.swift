import Foundation

/// Describes frames the encoder actually accepted. Gaps and clock changes
/// start a new clip; a single accepted frame is still available footage.
public struct RecordingSegmentTimeline {
    public enum Decision { case append, skip, split }
    public let start: Date
    public let firstPresentationTime: TimeInterval
    public private(set) var lastPresentationTime: TimeInterval
    public private(set) var lastFrameDuration: TimeInterval
    public var endPresentationTime: TimeInterval { lastPresentationTime + lastFrameDuration }
    public var end: Date { start.addingTimeInterval(endPresentationTime - firstPresentationTime) }

    public init?(presentationTime: TimeInterval, capturedAt: Date, frameDuration: TimeInterval?) {
        guard presentationTime.isFinite, presentationTime >= 0,
              capturedAt.timeIntervalSince1970.isFinite else { return nil }
        start = capturedAt
        firstPresentationTime = presentationTime
        lastPresentationTime = presentationTime
        lastFrameDuration = Self.duration(frameDuration)
    }

    public func decision(presentationTime: TimeInterval, capturedAt: Date) -> Decision {
        guard presentationTime.isFinite, presentationTime >= 0,
              capturedAt.timeIntervalSince1970.isFinite else { return .skip }
        if presentationTime == lastPresentationTime { return .skip }
        let elapsed = presentationTime - firstPresentationTime
        let expected = start.addingTimeInterval(elapsed)
        if presentationTime < lastPresentationTime || presentationTime - lastPresentationTime > 1
            || elapsed >= RetentionPolicy.segmentSeconds || abs(capturedAt.timeIntervalSince(expected)) > 1 {
            return .split
        }
        return .append
    }

    @discardableResult
    public mutating func append(presentationTime: TimeInterval, capturedAt: Date,
                                frameDuration: TimeInterval?) -> Bool {
        guard decision(presentationTime: presentationTime, capturedAt: capturedAt) == .append else { return false }
        lastPresentationTime = presentationTime
        lastFrameDuration = Self.duration(frameDuration)
        return true
    }

    public func segment(filename: String) -> VideoSegment {
        VideoSegment(filename: filename, start: start, end: end)
    }

    private static func duration(_ value: TimeInterval?) -> TimeInterval {
        value.flatMap { $0.isFinite && (0.001...1).contains($0) ? $0 : nil } ?? (1.0 / 30)
    }
}
