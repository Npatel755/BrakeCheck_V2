import Foundation

public enum LaneTrackingMode: String, CaseIterable, Sendable {
    case raw, averaging, kalman
    public var title: String {
        switch self {
        case .raw: return "Raw"
        case .averaging: return "Averaging"
        case .kalman: return "Kalman"
        }
    }
}

/// A native, display-only position/velocity filter for real UFLDv2 captures.
/// History can bridge the 3 Hz detector cadence, but is never emitted without
/// a fresh observation. Missing fragments, endpoints and timestamps are not
/// extended. This type cannot provide LanePath/lead-selection evidence.
public struct LaneKalmanSmoother {
    private typealias Observation = LanePredictionObservation
    private typealias Boundary = Observation.Boundary
    private typealias Point = Observation.Point

    private struct Filter {
        var value: Double
        var velocity = 0.0
        var p00: Double
        var p01 = 0.0
        var p11 = 0.0009

        init(_ value: Double, variance: Double) { self.value = value; p00 = variance }

        func predicted(_ dt: Double) -> Filter {
            let q = 0.12 * 0.12
            var next = self
            next.value += velocity * dt
            next.p00 += 2 * dt * p01 + dt * dt * p11 + q * pow(dt, 4) / 4
            next.p01 += dt * p11 + q * pow(dt, 3) / 2
            next.p11 += q * dt * dt
            return next
        }

        func corrected(_ measured: Double, variance: Double) -> Filter {
            let residual = measured - value
            guard abs(residual) <= min(0.065, 3.5 * sqrt(max(0, p00 + variance))) else {
                return Filter(measured, variance: variance)
            }
            let k0 = p00 / (p00 + variance), k1 = p01 / (p00 + variance)
            var next = self
            next.value += k0 * residual
            next.velocity += k1 * residual
            // Joseph covariance update, preserving numerical symmetry.
            next.p00 = (1 - k0) * (1 - k0) * p00 + k0 * k0 * variance
            next.p01 = (1 - k0) * (p01 - k1 * p00) + k0 * k1 * variance
            next.p11 = p11 - 2 * k1 * p01 + k1 * k1 * (p00 + variance)
            return next
        }
    }

    private struct Sample { let anchor: Double; var filter: Filter }
    private struct Track { let row: Bool; let samples: [Sample] }
    private var tracks: [Track] = []
    private var previousInput: Observation?
    private var previousOutput: Observation?
    public init() {}
    public mutating func reset() { tracks = []; previousInput = nil; previousOutput = nil }

    public mutating func update(_ observation: LanePredictionObservation?, at now: TimeInterval)
        -> LanePredictionObservation? {
        guard let current = LanePredictionDisplay.visible(observation, at: now) else {
            reset(); return nil
        }
        if let old = previousInput {
            if current.capturedAt == old.capturedAt {
                if current == old { return previousOutput }
                reset() // conflicting same-time input is not another measurement
            } else if current.capturedAt < old.capturedAt {
                reset(); return nil
            } else if current.capturedAt - old.capturedAt > 0.75 {
                reset()
            }
        }
        let dt = previousInput.map { current.capturedAt - $0.capturedAt } ?? 0
        let predicted = tracks.map { track in
            Track(row: track.row, samples: track.samples.map {
                Sample(anchor: $0.anchor, filter: $0.filter.predicted(dt))
            })
        }
        // Associate physical fragments geometrically; trained slot numbers and
        // list ordering do not serve as persistent lane identities.
        var choices: [(boundary: Int, track: Int, cost: Double)] = []
        for (index, boundary) in current.boundaries.enumerated() {
            for (trackIndex, track) in predicted.enumerated() {
                if let cost = association(boundary, track) { choices.append((index, trackIndex, cost)) }
            }
        }
        // Ambiguous associations start fresh instead of blending old branches.
        let ambiguous = Set(current.boundaries.indices.filter { index in
            let costs = choices.filter { $0.boundary == index }.map(\.cost).sorted()
            return costs.count > 1 && costs[1] - costs[0] < 0.004
        })
        var matches: [Int: Int] = [:], used = Set<Int>()
        for choice in choices.sorted(by: { $0.cost < $1.cost }) {
            if !ambiguous.contains(choice.boundary), matches[choice.boundary] == nil, !used.contains(choice.track) {
                matches[choice.boundary] = choice.track; used.insert(choice.track)
            }
        }
        var nextTracks: [Track] = []
        let boundaries = current.boundaries.enumerated().map { index, boundary -> Boundary in
            let row = isRow(boundary)
            let prior = matches[index].map { predicted[$0] }
            let variance = pow(0.008 / boundary.confidence, 2)
            var samples: [Sample] = []
            let points = boundary.points.map { point -> Point in
                let a = anchor(point, row), measured = coordinate(point, row)
                let filter = interpolate(prior, at: a)?.corrected(measured, variance: variance)
                    ?? Filter(measured, variance: variance)
                samples.append(Sample(anchor: a, filter: filter))
                return row ? Point(x: filter.value, y: point.y) : Point(x: point.x, y: filter.value)
            }
            nextTracks.append(Track(row: row, samples: samples.sorted { $0.anchor < $1.anchor }))
            return Boundary(laneSlot: boundary.laneSlot, confidence: boundary.confidence, points: points)
        }
        let candidate = Observation(capturedAt: current.capturedAt, boundaries: boundaries)
        // Reject a new crossing or unstable shape introduced by filtering.
        // The actual current observation is already geometry-filtered upstream.
        let stable = LanePredictionGeometry.filtered(candidate)?.boundaries.count == boundaries.count
        let crossing = introducedIntersection(boundaries, current.boundaries)
        let output = stable && !crossing ? candidate : current
        tracks = stable && !crossing ? nextTracks : current.boundaries.map { boundary in
            let row = isRow(boundary)
            return Track(row: row, samples: boundary.points.map {
                Sample(anchor: anchor($0, row), filter: Filter(coordinate($0, row), variance: pow(0.008 / boundary.confidence, 2)))
            }.sorted { $0.anchor < $1.anchor })
        }
        previousInput = current; previousOutput = output
        return output
    }

    private func isRow(_ boundary: Boundary) -> Bool { boundary.laneSlot == 1 || boundary.laneSlot == 2 }
    private func anchor(_ point: Point, _ row: Bool) -> Double { row ? point.y : point.x }
    private func coordinate(_ point: Point, _ row: Bool) -> Double { row ? point.x : point.y }

    private func interpolate(_ track: Track?, at value: Double) -> Filter? {
        guard let samples = track?.samples, samples.count >= 2 else { return nil }
        if let exact = samples.first(where: { abs($0.anchor - value) < 1e-9 }) { return exact.filter }
        for (a, b) in zip(samples, samples.dropFirst()) {
            guard value >= a.anchor, value <= b.anchor, b.anchor - a.anchor > 1e-9,
                  b.anchor - a.anchor <= 0.065 else { continue }
            let t = (value - a.anchor) / (b.anchor - a.anchor)
            var result = a.filter
            result.value += t * (b.filter.value - a.filter.value)
            result.velocity += t * (b.filter.velocity - a.filter.velocity)
            result.p00 += t * (b.filter.p00 - a.filter.p00)
            result.p01 += t * (b.filter.p01 - a.filter.p01)
            result.p11 += t * (b.filter.p11 - a.filter.p11)
            return result
        }
        return nil
    }

    private func association(_ boundary: Boundary, _ track: Track) -> Double? {
        guard isRow(boundary) == track.row, boundary.points.count >= 3 else { return nil }
        let anchors = boundary.points.map { anchor($0, track.row) }
        let steps = zip(anchors, anchors.dropFirst()).map { $1 - $0 }
        guard steps.allSatisfy({ $0 > 1e-9 }) || steps.allSatisfy({ $0 < -1e-9 }) else { return nil }
        var residuals: [Double] = [], accepted = 0
        let variance = pow(0.008 / boundary.confidence, 2)
        for point in boundary.points {
            guard let old = interpolate(track, at: anchor(point, track.row)) else { continue }
            let residual = abs(coordinate(point, track.row) - old.value)
            residuals.append(residual)
            if residual <= min(0.065, 3.5 * sqrt(max(0, old.p00 + variance))) { accepted += 1 }
        }
        guard residuals.count >= 3, Double(accepted) / Double(boundary.points.count) >= 0.60,
              residuals.sorted()[residuals.count / 2] <= 0.065 else { return nil }
        return residuals.reduce(0, +) / Double(residuals.count)
    }

    private func introducedIntersection(_ smoothed: [Boundary], _ current: [Boundary]) -> Bool {
        guard smoothed.count >= 2 else { return false }
        for i in 0..<(smoothed.count - 1) {
            for j in (i + 1)..<smoothed.count {
                if intersects(smoothed[i], smoothed[j]) && !intersects(current[i], current[j]) { return true }
            }
        }
        return false
    }

    private func intersects(_ first: Boundary, _ second: Boundary) -> Bool {
        func cross(_ a: Point, _ b: Point, _ c: Point) -> Double {
            (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)
        }
        func contains(_ a: Point, _ b: Point, _ c: Point) -> Bool {
            c.x >= min(a.x, b.x) - 1e-10 && c.x <= max(a.x, b.x) + 1e-10
                && c.y >= min(a.y, b.y) - 1e-10 && c.y <= max(a.y, b.y) + 1e-10
        }
        for (a, b) in zip(first.points, first.points.dropFirst()) {
            for (c, d) in zip(second.points, second.points.dropFirst()) {
                let ac = cross(a, b, c), ad = cross(a, b, d), ca = cross(c, d, a), cb = cross(c, d, b)
                if ((ac > 0 && ad < 0) || (ac < 0 && ad > 0))
                    && ((ca > 0 && cb < 0) || (ca < 0 && cb > 0)) { return true }
                if abs(ac) <= 1e-10 && contains(a, b, c) || abs(ad) <= 1e-10 && contains(a, b, d)
                    || abs(ca) <= 1e-10 && contains(c, d, a) || abs(cb) <= 1e-10 && contains(c, d, b) { return true }
            }
        }
        return false
    }
}
