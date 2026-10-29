import Foundation

/// Presentation quality checks, not paint classification or driving evidence.
/// Reject badly jagged fragments instead of making them look authoritative.
public enum LanePredictionGeometry {
    private typealias Observation = LanePredictionObservation
    private typealias Boundary = Observation.Boundary
    private typealias Point = Observation.Point

    public static func filtered(_ observation: LanePredictionObservation?) -> LanePredictionObservation? {
        guard let observation,
              let valid = LanePredictionDisplay.visible(observation, at: observation.capturedAt) else { return nil }
        let boundaries = valid.boundaries.compactMap(clean)
        return boundaries.isEmpty ? nil : .init(capturedAt: valid.capturedAt, boundaries: boundaries)
    }

    private static func clean(_ boundary: Boundary) -> Boundary? {
        let points = boundary.points
        let row = boundary.laneSlot == 1 || boundary.laneSlot == 2
        func anchor(_ point: Point) -> Double { row ? point.y : point.x }
        func coordinate(_ point: Point) -> Double { row ? point.x : point.y }
        let steps = zip(points, points.dropFirst()).map { anchor($1) - anchor($0) }
        guard steps.allSatisfy({ $0 > 1e-9 }) || steps.allSatisfy({ $0 < -1e-9 }) else { return nil }
        // A large lateral jump between nearby observed anchors is unsupported.
        guard zip(points, points.dropFirst()).allSatisfy({
            abs(coordinate($1) - coordinate($0)) <= 0.12
        }) else { return nil }
        guard points.count >= 3 else { return boundary }
        var residuals: [Double] = []
        for index in 1..<(points.count - 1) {
            let a = points[index - 1], current = points[index], b = points[index + 1]
            let fraction = (anchor(current) - anchor(a)) / (anchor(b) - anchor(a))
            let expected = coordinate(a) + fraction * (coordinate(b) - coordinate(a))
            residuals.append(abs(coordinate(current) - expected))
        }
        let sorted = residuals.sorted()
        let median = sorted[sorted.count / 2]
        let upper = sorted[min(sorted.count - 1, Int(Double(sorted.count - 1) * 0.9))]
        guard median <= 0.015, upper <= 0.04, (sorted.last ?? 0) <= 0.10 else { return nil }

        // A bounded local median suppresses small bin jitter. Endpoints, the
        // observed anchor axis, confidence, fragments and capture time stay put.
        // Larger discrepancies remain unsmoothed, rather than being fitted away.
        let cleaned = points.enumerated().map { index, point -> Point in
            guard index > 0, index < points.count - 1 else { return point }
            let local = [coordinate(points[index - 1]), coordinate(point), coordinate(points[index + 1])].sorted()[1]
            let current = coordinate(point)
            guard abs(local - current) <= 0.012 else { return point }
            let value = 0.5 * current + 0.5 * local
            return row ? .init(x: value, y: point.y) : .init(x: point.x, y: value)
        }
        return .init(laneSlot: boundary.laneSlot, confidence: boundary.confidence, points: cleaned)
    }
}
