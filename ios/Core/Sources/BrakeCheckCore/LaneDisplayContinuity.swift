import Foundation

/// Presentation only. Never use this held/smoothed corridor for lead eligibility.
/// Capture times remain original; missing inference does not renew the hold.
public struct LaneDisplayContinuity {
    public static let holdSeconds: TimeInterval = 3
    private var displayed: LanePath?
    public init() {}
    public mutating func reset() { displayed = nil }

    public func current(at now: TimeInterval) -> LanePath? {
        guard let displayed, now.isFinite, now >= displayed.capturedAt,
              now - displayed.capturedAt <= Self.holdSeconds + 1e-6 else { return nil }
        return displayed
    }

    public mutating func update(_ lane: LanePath?, at now: TimeInterval) -> LanePath? {
        guard now.isFinite else { reset(); return nil }
        guard let lane, lane.isUsable(at: now) else { return current(at: now) }
        guard let prior = current(at: now) else { displayed = lane; return lane }
        guard lane.capturedAt > prior.capturedAt else { return prior }
        let elapsed = lane.capturedAt - prior.capturedAt
        let alpha = 1 - exp(-elapsed / 0.25)
        let bands = lane.bands.map { band -> LaneBand in
            guard let old = prior.bounds(at: band.y),
                  max(abs(old.left - band.left), abs(old.right - band.right)) <= 0.12,
                  elapsed <= 0.5 else { return band }
            return LaneBand(y: band.y, left: old.left + (band.left - old.left) * alpha,
                            right: old.right + (band.right - old.right) * alpha)
        }
        // Large scene changes and recovery after a gap use the new measurement.
        let result = LanePath(bands: bands, confidence: lane.confidence,
                              capturedAt: lane.capturedAt, source: lane.source)
        displayed = result
        return result
    }
}
