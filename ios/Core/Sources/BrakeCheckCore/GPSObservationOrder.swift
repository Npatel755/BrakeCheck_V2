import Foundation

/// Invalid future timestamps must not block later real fixes from the receiver.
public struct GPSObservationOrder {
    private var latest: TimeInterval?
    public init() {}
    public mutating func reset() { latest = nil }

    public mutating func accept(timestamp: TimeInterval, now: TimeInterval) -> Bool {
        guard timestamp.isFinite, now.isFinite, (-0.5...15).contains(now - timestamp),
              latest.map({ timestamp > $0 }) ?? true else { return false }
        latest = timestamp
        return true
    }
}
