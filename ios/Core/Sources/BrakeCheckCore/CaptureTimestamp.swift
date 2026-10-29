import Foundation

/// Maps an already clock-converted camera presentation timestamp into the
/// uptime domain used by GPS and tracking. Host time and uptime need not share
/// an epoch: only the age between two readings of the same host clock is used.
/// A delayed delivery retains its full age; it is never stamped as a new frame.
/// Used exclusively on the serial capture queue.
public struct CaptureTimestamp {
    /// Clock reads are adjacent, but conversion/rounding can lead by a tiny
    /// amount. Larger future values indicate an invalid or unmapped clock.
    public static let futureTolerance: TimeInterval = 0.002
    private var notBefore: TimeInterval?
    private var lastHostTimestamp: TimeInterval?
    private var lastCaptureUptime: TimeInterval?

    public init() {}

    /// Start a new camera/geometry epoch. Captures before its boundary are
    /// excluded even if delivered after the new session has started.
    public mutating func reset(notBefore boundary: TimeInterval) {
        notBefore = boundary.isFinite && boundary >= 0 ? boundary : nil
        lastHostTimestamp = nil
        lastCaptureUptime = nil
    }

    /// `hostTimestamp` must first be converted from the capture session clock
    /// to the same host clock used by `hostNow`. Nil/invalid conversions fail
    /// closed; raw session seconds must never be passed as uptime.
    /// Sample `uptimeNow` just before `hostNow`: if the thread is preempted
    /// between reads, the resulting age is conservative rather than too fresh.
    public mutating func uptime(hostTimestamp: TimeInterval?, hostNow: TimeInterval?,
                                receivedAt uptimeNow: TimeInterval) -> TimeInterval? {
        guard let boundary = notBefore, let hostTimestamp, let hostNow,
              hostTimestamp.isFinite, hostNow.isFinite,
              uptimeNow.isFinite, uptimeNow >= 0 else { return nil }
        let age = hostNow - hostTimestamp
        guard age.isFinite, age >= -Self.futureTolerance else { return nil }
        let capturedAt = uptimeNow - max(0, age)
        guard capturedAt.isFinite, capturedAt >= boundary,
              lastHostTimestamp.map({ hostTimestamp > $0 }) ?? true,
              lastCaptureUptime.map({ capturedAt > $0 }) ?? true else { return nil }
        lastHostTimestamp = hostTimestamp
        lastCaptureUptime = capturedAt
        return capturedAt
    }
}
