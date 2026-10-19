import Foundation

/// Outline presentation only. No mask, lead eligibility or AR projection may
/// use these filtered bounds. Negative inference still clears the raw road mask.
public struct RoadDisplayContinuity {
    private var display = LaneDisplayContinuity(rejectOutliers: true, smoothingSeconds: 0.55)
    public init() {}
    public mutating func reset() { display.reset() }
    public func current(at time: TimeInterval) -> RoadRegion? {
        guard let lane = display.current(at: time), time - lane.capturedAt <= 0.8 else { return nil }
        return RoadRegion(bands: lane.bands, confidence: lane.confidence, capturedAt: lane.capturedAt)
    }
    public mutating func update(_ road: RoadRegion?, at time: TimeInterval) -> RoadRegion? {
        let lane = road.flatMap { road -> LanePath? in
            guard road.isUsable(at: time), let first = road.bands.first, let last = road.bands.last else { return nil }
            // Fixed vertical samples keep display bounds stable when each
            // segmentation result has a different number of rows.
            let rows = (1..<40).compactMap { index -> LaneBand? in
                let y = Double(index) * 0.025
                guard y >= first.y, y <= last.y, let bounds = road.bounds(at: y) else { return nil }
                return LaneBand(y: y, left: bounds.left, right: bounds.right)
            }
            return LanePath(bands: rows.count >= 5 ? rows : road.bands,
                confidence: road.confidence, capturedAt: road.capturedAt, source: .neural)
        }
        _ = display.update(lane, at: time)
        return current(at: time)
    }
}
