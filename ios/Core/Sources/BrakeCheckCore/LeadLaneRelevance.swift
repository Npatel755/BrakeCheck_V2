import Foundation

/// A fresh observed ego boundary can show that a car is on its outside even
/// when the other boundary is missing. This is a relevance constraint only:
/// passing it cannot establish lane membership, orientation, range or a hazard.
public enum LeadLaneRelevance {
    public static func excludes(_ box: CGRect, boundaries: LanePredictionObservation?, at time: TimeInterval) -> Bool {
        guard LeadVehicle.validBox(box), let visible = LanePredictionDisplay.visible(boundaries, at: time) else { return false }
        let foot = Double(box.minY), center = Double(box.midX)
        for slot in [1, 2] {
            let values = visible.boundaries.filter { $0.laneSlot == slot && $0.confidence >= 0.80 && $0.points.count >= 8 }
                .compactMap { value($0.points, at: foot) }
            // Overlapping fragments must agree before excluding an object.
            guard !values.isEmpty, (values.max()! - values.min()!) <= 0.015 else { continue }
            let edge = values.reduce(0, +) / Double(values.count)
            let margin = max(0.015, Double(box.width) * 0.15)
            if slot == 1 && center < edge - margin { return true }
            if slot == 2 && center > edge + margin { return true }
        }
        return false
    }

    private static func value(_ points: [LanePredictionObservation.Point], at y: Double) -> Double? {
        let points = points.sorted { $0.y < $1.y }
        guard zip(points, points.dropFirst()).allSatisfy({ $1.y > $0.y }) else { return nil }
        if let exact = points.first(where: { abs($0.y - y) < 1e-9 }) { return exact.x }
        for (a, b) in zip(points, points.dropFirst()) where y >= a.y && y <= b.y {
            guard b.y - a.y <= 0.065 else { return nil }
            return a.x + (b.x - a.x) * (y - a.y) / (b.y - a.y)
        }
        return nil
    }
}
