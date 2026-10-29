import Foundation

/// Align a camera-clock timestamp with uptime using its measured delivery age.
/// Receipt time must never make a queued frame appear newly captured.
public enum CaptureFrameTiming {
    public static func captureUptime(frameHostTime: TimeInterval, hostNow: TimeInterval,
                                     uptimeNow: TimeInterval,
                                     maximumAge: TimeInterval = TrackingFreshness.measurementSeconds) -> TimeInterval? {
        guard frameHostTime.isFinite, hostNow.isFinite, uptimeNow.isFinite,
              maximumAge.isFinite, maximumAge >= 0,
              frameHostTime >= 0, hostNow >= frameHostTime, uptimeNow >= 0 else { return nil }
        let age = hostNow - frameHostTime
        guard age <= maximumAge, uptimeNow >= age else { return nil }
        return uptimeNow - age
    }

    public static func captureDate(capturedAt: TimeInterval, uptimeNow: TimeInterval, wallNow: Date) -> Date? {
        guard capturedAt.isFinite, uptimeNow.isFinite, capturedAt >= 0, uptimeNow >= capturedAt,
              wallNow.timeIntervalSince1970.isFinite else { return nil }
        let date = wallNow.addingTimeInterval(capturedAt - uptimeNow)
        return date.timeIntervalSince1970.isFinite ? date : nil
    }
}
