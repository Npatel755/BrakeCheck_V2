import Foundation

/// Presentation only. Never use this held/smoothed corridor for lead eligibility.
/// Capture times remain original; missing inference does not renew the hold.
public struct LaneDisplayContinuity {
    public static let holdSeconds: TimeInterval = 3
    private var displayed: LanePath?
    private var pending: LanePath?
    private var pendingCount = 0
    private let rejectOutliers: Bool
    private let smoothingSeconds: Double
    public init(rejectOutliers: Bool = false, smoothingSeconds: Double = 0.25) {
        self.rejectOutliers = rejectOutliers
        self.smoothingSeconds = smoothingSeconds.isFinite ? max(0.05, smoothingSeconds) : 0.25
    }
    public mutating func reset() { displayed = nil; pending = nil; pendingCount = 0 }

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
        if rejectOutliers {
            if prior.source != .markings, lane.source == .markings, now - prior.capturedAt <= 0.8 { return prior }
            let shifts = lane.bands.compactMap { band -> Double? in
                guard let old = prior.bounds(at: band.y) else { return nil }
                return max(abs(old.left - band.left), abs(old.right - band.right))
            }
            if !shifts.isEmpty, shifts.reduce(0, +) / Double(shifts.count) > 0.10 {
                if let pending, lane.capturedAt <= pending.capturedAt { return prior }
                let agrees = pending.map { candidate in
                    let shared = lane.bands.compactMap { band -> Double? in
                        guard let old = candidate.bounds(at: band.y) else { return nil }
                        return max(abs(old.left - band.left), abs(old.right - band.right))
                    }
                    return lane.capturedAt - candidate.capturedAt <= 0.6 && shared.count >= 3
                        && shared.reduce(0, +) / Double(shared.count) <= 0.06
                } ?? false
                pendingCount = agrees ? pendingCount + 1 : 1
                pending = lane
                guard pendingCount >= 3 else { return prior }
            }
            pending = nil; pendingCount = 0
        }
        let elapsed = lane.capturedAt - prior.capturedAt
        let alpha = 1 - exp(-elapsed / smoothingSeconds)
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
