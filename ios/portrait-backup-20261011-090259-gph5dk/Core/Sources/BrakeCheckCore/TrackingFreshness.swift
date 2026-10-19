import Foundation

/// Object continuity and numerical freshness have different lifetimes.
/// Delayed or coasted frames can hold an identity, never an old speed or TTC.
public enum TrackingFreshness {
    public static let measurementSeconds: TimeInterval = 0.60
    public static let identitySeconds: TimeInterval = 3.0
    public static func measurementIsFresh(capturedAt: TimeInterval, now: TimeInterval) -> Bool {
        capturedAt.isFinite && now.isFinite && (0...measurementSeconds).contains(now - capturedAt)
    }
    public static func identityIsFresh(capturedAt: TimeInterval, now: TimeInterval) -> Bool {
        capturedAt.isFinite && now.isFinite && (0...identitySeconds).contains(now - capturedAt)
    }
}
