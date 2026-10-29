import Foundation

/// Presentation lifetime is independent of measurement validity and target
/// identity. Once raised, a warning survives loss, resets and lower risk.
public struct BrakeWarningLatch {
    public static let minimumSeconds: TimeInterval = 5
    private var expiresAt: TimeInterval = -.infinity
    public init() {}
    public mutating func trigger(at time: TimeInterval) {
        guard time.isFinite else { return }
        expiresAt = max(expiresAt, time + Self.minimumSeconds)
    }
    public func isActive(at time: TimeInterval) -> Bool {
        time.isFinite && time < expiresAt
    }
}
