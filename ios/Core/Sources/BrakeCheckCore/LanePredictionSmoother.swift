import Foundation

/// Temporal presentation of repeated, compatible model observations only.
/// This is not LanePath evidence and must never feed safety, AR or calibration.
/// Apply it once per actual decoded capture, before current vehicle masking;
/// pass nil for a new negative result and reset after rotation/session changes.
public struct LanePredictionSmoother {
    private typealias Observation = LanePredictionObservation
    private typealias Boundary = Observation.Boundary
    private typealias Point = Observation.Point
    private var previousInput: Observation?
    private var previousOutput: Observation?
    private let smoothingSeconds: Double
    private let maximumShift: Double

    public init(smoothingSeconds: Double = 0.35, maximumCoordinateShift: Double = 0.05) {
        self.smoothingSeconds = smoothingSeconds.isFinite ? max(0.05, smoothingSeconds) : 0.35
        self.maximumShift = maximumCoordinateShift.isFinite
            ? min(0.10, max(0, maximumCoordinateShift)) : 0.05
    }

    public mutating func reset() { previousInput = nil; previousOutput = nil }

    /// Current observed endpoints, fragments, anchor coordinates, confidence and
    /// original capture time are preserved. Unsupported points are not filled.
    /// Nil, stale, future or invalid input clears history instead of holding it.
    public mutating func update(_ observation: LanePredictionObservation?, at now: TimeInterval)
        -> LanePredictionObservation? {
        guard let current = LanePredictionDisplay.visible(observation, at: now) else {
            reset(); return nil
        }
        guard let previousInput, let prior = previousOutput else {
            self.previousInput = current; previousOutput = current; return current
        }
        let elapsed = current.capturedAt - prior.capturedAt
        if elapsed == 0 {
            // Re-publishing a sample is not another model observation. A
            // conflicting same-time payload starts fresh rather than mixing it.
            if current == previousInput { return prior }
            self.previousInput = current; previousOutput = current; return current
        }
        guard elapsed > 0 else { reset(); return nil }
        guard elapsed <= LanePredictionDisplay.maximumAge else {
            self.previousInput = current; previousOutput = current; return current
        }

        let alpha = 1 - exp(-elapsed / smoothingSeconds)
        let boundaries = current.boundaries.map { boundary -> Boundary in
            let compatible = prior.boundaries.filter {
                $0.laneSlot == boundary.laneSlot && matches(boundary, prior: $0)
            }
            // A slot can contain disconnected or overlapping pieces. Never
            // choose an arbitrary old branch when association is ambiguous.
            guard compatible.count == 1, let old = compatible.first else { return boundary }
            let points = boundary.points.enumerated().map { index, point -> Point in
                // Keep the actual measured endpoints, including newly revealed
                // paint. No endpoint or support extension comes from history.
                guard index > 0, index < boundary.points.count - 1,
                      let previous = interpolate(old, at: anchor(point, slot: boundary.laneSlot)),
                      abs(previous - coordinate(point, slot: boundary.laneSlot)) <= maximumShift else { return point }
                let value = previous + alpha * (coordinate(point, slot: boundary.laneSlot) - previous)
                return boundary.laneSlot == 1 || boundary.laneSlot == 2
                    ? Point(x: value, y: point.y) : Point(x: point.x, y: value)
            }
            return Boundary(laneSlot: boundary.laneSlot, confidence: boundary.confidence, points: points)
        }
        // Preserving differently placed endpoints can otherwise let a blended
        // interior cross a neighboring boundary that is still at its current
        // endpoint. Prefer the actual observation to introducing an intersection.
        let safeBoundaries = introducedIntersection(boundaries, current: current.boundaries)
            ? current.boundaries : boundaries
        let output = Observation(capturedAt: current.capturedAt, boundaries: safeBoundaries)
        self.previousInput = current; previousOutput = output
        return output
    }

    private func matches(_ current: Boundary, prior: Boundary) -> Bool {
        guard monotonic(current), monotonic(prior), current.points.count >= 3 else { return false }
        var count = 0
        for point in current.points {
            guard let old = interpolate(prior, at: anchor(point, slot: current.laneSlot)) else { continue }
            // A large scene/lane change is shown immediately, unsmoothed.
            guard abs(old - coordinate(point, slot: current.laneSlot)) <= maximumShift else { return false }
            count += 1
        }
        return count >= 3 && Double(count) / Double(current.points.count) >= 0.60
    }

    private func anchor(_ point: Point, slot: Int) -> Double {
        slot == 1 || slot == 2 ? point.y : point.x
    }
    private func coordinate(_ point: Point, slot: Int) -> Double {
        slot == 1 || slot == 2 ? point.x : point.y
    }
    private func monotonic(_ boundary: Boundary) -> Bool {
        guard boundary.points.count >= 2 else { return false }
        let deltas = zip(boundary.points, boundary.points.dropFirst()).map {
            anchor($1, slot: boundary.laneSlot) - anchor($0, slot: boundary.laneSlot)
        }
        return deltas.allSatisfy { $0 > 1e-9 } || deltas.allSatisfy { $0 < -1e-9 }
    }

    private func introducedIntersection(_ smoothed: [Boundary], current: [Boundary]) -> Bool {
        guard smoothed.count >= 2 else { return false }
        for first in 0..<(smoothed.count - 1) {
            for last in (first + 1)..<smoothed.count {
                if intersects(smoothed[first], smoothed[last]), !intersects(current[first], current[last]) {
                    return true
                }
            }
        }
        return false
    }

    private func intersects(_ first: Boundary, _ last: Boundary) -> Bool {
        func cross(_ a: Point, _ b: Point, _ c: Point) -> Double {
            (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)
        }
        func contains(_ a: Point, _ b: Point, _ point: Point) -> Bool {
            point.x >= min(a.x, b.x) - 1e-10 && point.x <= max(a.x, b.x) + 1e-10
                && point.y >= min(a.y, b.y) - 1e-10 && point.y <= max(a.y, b.y) + 1e-10
        }
        for (a, b) in zip(first.points, first.points.dropFirst()) {
            for (c, d) in zip(last.points, last.points.dropFirst()) {
                let ac = cross(a, b, c), ad = cross(a, b, d)
                let ca = cross(c, d, a), cb = cross(c, d, b)
                if ((ac > 0 && ad < 0) || (ac < 0 && ad > 0))
                    && ((ca > 0 && cb < 0) || (ca < 0 && cb > 0)) { return true }
                if abs(ac) <= 1e-10 && contains(a, b, c)
                    || abs(ad) <= 1e-10 && contains(a, b, d)
                    || abs(ca) <= 1e-10 && contains(c, d, a)
                    || abs(cb) <= 1e-10 && contains(c, d, b) { return true }
            }
        }
        return false
    }

    /// Interpolate only inside one existing fragment's shared anchor support.
    /// Row slots interpolate x at the current y; column slots interpolate y at x.
    private func interpolate(_ boundary: Boundary, at value: Double) -> Double? {
        for (first, last) in zip(boundary.points, boundary.points.dropFirst()) {
            let a = anchor(first, slot: boundary.laneSlot), b = anchor(last, slot: boundary.laneSlot)
            guard value >= min(a, b), value <= max(a, b), abs(b - a) > 1e-9 else { continue }
            let t = (value - a) / (b - a)
            return coordinate(first, slot: boundary.laneSlot)
                + t * (coordinate(last, slot: boundary.laneSlot) - coordinate(first, slot: boundary.laneSlot))
        }
        return nil
    }
}
