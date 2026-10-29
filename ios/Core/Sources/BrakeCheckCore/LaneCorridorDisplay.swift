import Foundation

/// Camera presentation only. Pair independently observed ego boundaries without
/// extending their support or supplying driving/calibration evidence.
public enum LaneCorridorDisplay {
    public struct Section: Equatable, Sendable {
        public let left: [LanePredictionObservation.Point]
        public let right: [LanePredictionObservation.Point]
        public var polygon: [LanePredictionObservation.Point] { left + right.reversed() }
    }
    private typealias Point = LanePredictionObservation.Point
    private typealias Boundary = LanePredictionObservation.Boundary

    public static func sections(_ observation: LanePredictionObservation?, at time: TimeInterval) -> [Section] {
        guard let current = LanePredictionDisplay.visible(observation, at: time) else { return [] }
        let left = current.boundaries.filter { $0.laneSlot == 1 }.compactMap(sorted)
        let right = current.boundaries.filter { $0.laneSlot == 2 }.compactMap(sorted)
        var result: [Section] = []
        for l in left {
            for r in right {
                let bottom = max(l.first!.y, r.first!.y), top = min(l.last!.y, r.last!.y)
                guard top - bottom >= 0.04 else { continue }
                let anchors = Array(Set((l + r).map(\.y).filter { $0 >= bottom && $0 <= top })).sorted()
                var ls: [Point] = [], rs: [Point] = []
                func finish() {
                    if ls.count >= 3, ls.last!.y - ls.first!.y >= 0.04 {
                        result.append(Section(left: ls, right: rs))
                    }
                    ls = []; rs = []
                }
                for y in anchors {
                    guard let lx = interpolate(l, y), let rx = interpolate(r, y),
                          (0.045...0.90).contains(rx - lx) else { finish(); continue }
                    if let prior = ls.last, let pr = rs.last,
                       y - prior.y > 0.065 || abs(lx - prior.x) > 0.12 || abs(rx - pr.x) > 0.12 {
                        finish()
                    }
                    // Overlapping fragments make the branch ambiguous.
                    guard left.filter({ interpolate($0, y) != nil }).count == 1,
                          right.filter({ interpolate($0, y) != nil }).count == 1 else { finish(); continue }
                    ls.append(.init(x: lx, y: y)); rs.append(.init(x: rx, y: y))
                }
                finish()
            }
        }
        return result
    }

    private static func sorted(_ boundary: Boundary) -> [Point]? {
        let points = boundary.points.sorted { $0.y < $1.y }
        guard points.count >= 3, zip(points, points.dropFirst()).allSatisfy({ $1.y > $0.y }) else { return nil }
        return points
    }

    private static func interpolate(_ points: [Point], _ y: Double) -> Double? {
        guard let first = points.first, let last = points.last, y >= first.y, y <= last.y else { return nil }
        if let exact = points.first(where: { abs($0.y - y) < 1e-9 }) { return exact.x }
        for (a, b) in zip(points, points.dropFirst()) where y >= a.y && y <= b.y {
            guard b.y - a.y > 1e-9, b.y - a.y <= 0.065 else { return nil }
            return a.x + (b.x - a.x) * (y - a.y) / (b.y - a.y)
        }
        return nil
    }
}
