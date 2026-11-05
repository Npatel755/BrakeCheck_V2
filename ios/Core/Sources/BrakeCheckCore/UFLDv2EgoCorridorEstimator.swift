import Foundation
#if canImport(CoreGraphics)
import CoreGraphics
#endif

/// Fits the two trained ego slots independently. Unlike a slotless ridge
/// fitter, this cannot reinterpret one jagged model head as both road sides.
/// Raw short-span display predictions never enter this estimator.
public struct UFLDv2EgoCorridorEstimator {
    private struct Sample {
        let x: Double
        let y: Double
        let anchor: Int
        let strength: Double
    }
    /// One rejected or incomplete capture inside this window keeps a confirmed
    /// pair's history, so a single dropped frame does not cost two confirmations.
    public static let missTolerance: TimeInterval = 0.6
    /// A confirmed corridor whose near width changes by more than this factor in one
    /// capture is a slot hopping to the neighbouring line, not a lane; it is held like
    /// a miss, and only a repeated new width re-confirms (a real lane change).
    public static let widthJumpFactor = 1.35
    private var previous: LanePath?
    private var consecutive = 0
    private var lastUpdate: TimeInterval?
    private var lastAccepted: TimeInterval?
    private var missed = 0
    public private(set) var status = "waiting for ego lane pair"
    public init() {}
    public mutating func reset() {
        previous = nil; consecutive = 0; lastUpdate = nil; lastAccepted = nil; missed = 0
        status = "waiting for ego lane pair"
    }

    public mutating func update(_ candidates: [UFLDv2LaneDecoder.Candidate],
                                excluding boxes: [CGRect] = [], at time: TimeInterval) -> LanePath? {
        guard time.isFinite, lastUpdate.map({ time > $0 }) ?? true else {
            reset(); status = "invalid ego observation time"; return nil
        }
        if let lastUpdate, time - lastUpdate > 1.10 { previous = nil; consecutive = 0; missed = 0 }
        lastUpdate = time
        // A slot with several decoded pieces keeps its best-supported piece.
        let left = candidates.filter { $0.laneSlot == 1 && $0.axis == .row }
            .max { $0.positiveAnchors < $1.positiveAnchors }.map { [$0] } ?? []
        let right = candidates.filter { $0.laneSlot == 2 && $0.axis == .row }
            .max { $0.positiveAnchors < $1.positiveAnchors }.map { [$0] } ?? []
        guard left.count == 1, right.count == 1 else {
            return reject("both independent ego boundaries required", at: time)
        }
        let masks = boxes.filter {
            $0.origin.x.isFinite && $0.origin.y.isFinite && $0.width.isFinite && $0.height.isFinite
                && $0.width > 0 && $0.height > 0
        }.map { $0.insetBy(dx: -0.008, dy: -0.008) }
        let leftSegments = segments(left[0], excluding: masks)
        let rightSegments = segments(right[0], excluding: masks)
        var best: LanePath?
        for l in leftSegments {
            for r in rightSegments {
                guard let path = pair(l, r, leftProbability: left[0].meanExistenceProbability,
                                      rightProbability: right[0].meanExistenceProbability, at: time) else { continue }
                let span = path.bands.last!.y - path.bands.first!.y
                let priorSpan = best.map { $0.bands.last!.y - $0.bands.first!.y } ?? -1
                if span > priorSpan { best = path }
            }
        }
        guard let path = best else { return reject("ego boundaries incomplete or inconsistent", at: time) }
        if let previous, consecutive >= 2, let jump = Self.widthJump(from: previous, to: path),
           jump > Self.widthJumpFactor || jump < 1 / Self.widthJumpFactor {
            return reject(String(format: "ego width jumped %.0f%% · holding", (jump - 1) * 100), at: time)
        }
        if let previous {
            let shifts = path.bands.compactMap { band -> Double? in
                guard let old = previous.bounds(at: band.y) else { return nil }
                return max(abs(old.left - band.left), abs(old.right - band.right))
            }
            if shifts.count < 5 || shifts.reduce(0, +) / Double(max(1, shifts.count)) > 0.10 {
                consecutive = 0
            }
        }
        previous = path; consecutive += 1; missed = 0; lastAccepted = time
        status = consecutive >= 2 ? "measured ego lane confirmed" : "confirming ego lane pair"
        return consecutive >= 2 ? path : nil
    }

    /// Nothing is emitted for a rejected capture. A confirmed pair survives one
    /// brief miss so the next complete capture is measured immediately; a second
    /// consecutive miss, or a longer gap, requires re-confirmation.
    private mutating func reject(_ reason: String, at time: TimeInterval) -> LanePath? {
        if consecutive >= 2, missed == 0, let lastAccepted, time - lastAccepted <= Self.missTolerance {
            missed = 1; status = reason + " · holding confirmed pair"
            return nil
        }
        previous = nil; consecutive = 0; missed = 0; lastAccepted = nil; status = reason
        return nil
    }

    /// Near-row width ratio between two corridors, measured at the same row.
    static func widthJump(from previous: LanePath, to path: LanePath) -> Double? {
        guard let nearNew = path.bands.first, let nearOld = previous.bands.first else { return nil }
        let y = max(nearNew.y, nearOld.y)
        guard let old = previous.bounds(at: y), let new = path.bounds(at: y),
              old.right - old.left > 1e-6 else { return nil }
        return (new.right - new.left) / (old.right - old.left)
    }

    private func segments(_ candidate: UFLDv2LaneDecoder.Candidate,
                          excluding masks: [CGRect]) -> [[Sample]] {
        guard candidate.positiveAnchors >= UFLDv2LaneDecoder.rowMinimumAnchors,
              candidate.positiveAnchors <= UFLDv2LaneDecoder.rowAnchorCount,
              candidate.meanExistenceProbability.isFinite,
              (0.5...1).contains(candidate.meanExistenceProbability), candidate.points.count >= 5,
              candidate.points.allSatisfy({ point in
                  point.x.isFinite && point.y.isFinite && point.strength.isFinite
                      && (0..<1).contains(point.x) && (0..<1).contains(point.y)
                      && (0.5...1).contains(point.strength)
                      && (0..<UFLDv2LaneDecoder.rowAnchorCount).contains(point.anchorIndex)
                      && abs(point.y - (1 - UFLDv2LaneDecoder.rowAnchors[point.anchorIndex])) < 1e-7
              }), Set(candidate.points.map(\.anchorIndex)).count == candidate.points.count else { return [] }
        let samples = candidate.points.map {
            Sample(x: $0.x, y: $0.y, anchor: $0.anchorIndex, strength: $0.strength)
        }.sorted { $0.y < $1.y }
        var result: [[Sample]] = [], current: [Sample] = []
        func finish() {
            if current.count >= 5 { result.append(current) }
            current = []
        }
        for point in samples {
            if masks.contains(where: { $0.contains(CGPoint(x: point.x, y: point.y)) }) {
                finish(); continue
            }
            if let prior = current.last {
                let dy = point.y - prior.y
                // At most three absent anchors inside a measured section.
                // A large jump is not a painted boundary continuation.
                if dy <= 0 || abs(point.anchor - prior.anchor) > 4
                    || abs(point.x - prior.x) > 3.5 * dy
                    || masks.contains(where: { intersects($0, prior, point) }) { finish() }
            }
            current.append(point)
        }
        finish()
        return result
    }

    private func pair(_ left: [Sample], _ right: [Sample], leftProbability: Double,
                      rightProbability: Double, at time: TimeInterval) -> LanePath? {
        guard let firstLeft = left.first, let firstRight = right.first,
              let lastLeft = left.last, let lastRight = right.last else { return nil }
        let bottom = max(firstLeft.y, firstRight.y), top = min(lastLeft.y, lastRight.y)
        guard top - bottom >= 0.12 else { return nil }
        var bands: [LaneBand] = []
        for y in UFLDv2LaneDecoder.rowAnchors.reversed().map({ 1 - $0 }) where y >= bottom && y <= top {
            guard let l = interpolate(left, at: y), let r = interpolate(right, at: y),
                  (0.045...0.90).contains(r - l) else { return nil }
            bands.append(.init(y: y, left: l, right: r))
        }
        guard bands.count >= 8, let near = bands.first, let far = bands.last,
              far.y - near.y >= 0.12,
              left.filter({ $0.y >= near.y && $0.y <= far.y }).count >= 5,
              right.filter({ $0.y >= near.y && $0.y <= far.y }).count >= 5,
              bands.allSatisfy({ $0.right - $0.left <= near.right - near.left + 0.12 }) else { return nil }
        let confidence = min(0.95, min(leftProbability, rightProbability))
        let lane = LanePath(bands: bands, confidence: confidence, capturedAt: time, source: .neural)
        return lane.isUsable(at: time) ? lane : nil
    }

    private func interpolate(_ points: [Sample], at y: Double) -> Double? {
        guard let first = points.first, let last = points.last, y >= first.y, y <= last.y else { return nil }
        if abs(y - first.y) <= 1e-10 { return first.x }
        for index in 1..<points.count where y <= points[index].y + 1e-10 {
            let a = points[index - 1], b = points[index]
            guard b.y > a.y, abs(b.anchor - a.anchor) <= 4 else { return nil }
            return a.x + (b.x - a.x) * (y - a.y) / (b.y - a.y)
        }
        return last.x
    }

    private func intersects(_ rect: CGRect, _ a: Sample, _ b: Sample) -> Bool {
        var lower = 0.0, upper = 1.0
        for (origin, delta, minimum, maximum) in [
            (a.x, b.x - a.x, Double(rect.minX), Double(rect.maxX)),
            (a.y, b.y - a.y, Double(rect.minY), Double(rect.maxY))
        ] {
            if abs(delta) < 1e-12 {
                if origin < minimum || origin > maximum { return false }
            } else {
                let first = (minimum - origin) / delta, last = (maximum - origin) / delta
                lower = max(lower, min(first, last)); upper = min(upper, max(first, last))
                if lower > upper { return false }
            }
        }
        return true
    }
}
