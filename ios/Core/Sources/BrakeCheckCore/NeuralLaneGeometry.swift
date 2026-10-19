import Foundation
#if canImport(CoreGraphics)
import CoreGraphics
#endif

/// The same aspect-fit transform used before Core ML inference. Model arrays
/// have a top-left origin; lane/vehicle coordinates have Vision's bottom-left.
public struct LaneImageTransform {
    public let width: Int
    public let height: Int
    public let contentWidth: Double
    public let contentHeight: Double
    public let paddingX: Double
    public let paddingY: Double

    public init?(width: Int, height: Int, sourceWidth: Int, sourceHeight: Int) {
        guard width >= 64, height >= 64, sourceWidth > 0, sourceHeight > 0 else { return nil }
        self.width = width; self.height = height
        let scale = min(Double(width) / Double(sourceWidth), Double(height) / Double(sourceHeight))
        contentWidth = Double(sourceWidth) * scale
        contentHeight = Double(sourceHeight) * scale
        paddingX = (Double(width) - contentWidth) / 2
        paddingY = (Double(height) - contentHeight) / 2
    }

    public func pixel(x: Double, y: Double) -> (x: Int, y: Int)? {
        guard x.isFinite, y.isFinite, (0...1).contains(x), (0...1).contains(y) else { return nil }
        let px = Int((paddingX + x * contentWidth - 0.5).rounded())
        let py = Int((paddingY + (1 - y) * contentHeight - 0.5).rounded())
        return (min(width - 1, max(0, px)), min(height - 1, max(0, py)))
    }
}

/// Converts the trained YOLOP foreground/background sigmoid scores into row
/// observations. This is an argmax classifier, not a calibrated probability.
public enum NeuralLaneScanner {
    public static func scan(transform: LaneImageTransform, excluding boxes: [CGRect],
                            scores: (Int, Int) -> (background: Double, lane: Double)) -> [LaneMarkingRow] {
        let columns = max(64, Int(transform.contentWidth.rounded()))
        var rows: [LaneMarkingRow] = []
        for index in 0..<40 {
            let y = 0.035 + Double(index) * 0.015
            var evidence = [Double](repeating: 0, count: columns)
            for column in 1..<(columns - 1) {
                let x = Double(column) / Double(columns - 1)
                let point = CGPoint(x: CGFloat(x), y: CGFloat(y))
                if boxes.contains(where: { $0.insetBy(dx: -0.004, dy: -0.004).contains(point) }) { continue }
                guard let pixel = transform.pixel(x: x, y: y) else { continue }
                var best = 0.0
                // A small vertical strip keeps dashed/thin lines from falling
                // between sampled rows. Padding is never treated as road.
                for dy in -1...1 {
                    let rawY = pixel.y + dy
                    guard rawY >= 0, rawY < transform.height else { continue }
                    let value = scores(pixel.x, rawY)
                    if value.background.isFinite, value.lane.isFinite,
                       (0...1).contains(value.background), (0...1).contains(value.lane),
                       value.lane - value.background >= 0.04 {
                        // YOLOP uses two independent sigmoids. A winning lane
                        // score below 0.5 is still foreground, not background.
                        // Use class dominance as evidence, with a tie margin.
                        best = max(best, 0.5 + 0.5 * (value.lane - value.background)
                                   / max(0.04, value.lane + value.background))
                    }
                }
                evidence[column] = best
            }
            var peaks: [LanePeak] = []
            var start: Int?
            for column in 0...columns {
                let active = column < columns && evidence[column] >= 0.5
                if active, start == nil { start = column }
                if !active, let first = start {
                    let last = column - 1
                    if Double(last - first + 1) / Double(columns) <= 0.07 {
                        let weight = (first...last).reduce(0.0) { $0 + evidence[$1] }
                        let center = (first...last).reduce(0.0) { $0 + Double($1) * evidence[$1] } / weight
                        peaks.append(LanePeak(x: center / Double(columns - 1),
                                              strength: (first...last).map { evidence[$0] }.max() ?? 0))
                    }
                    start = nil
                }
            }
            rows.append(LaneMarkingRow(y: y, peaks: Array(peaks.sorted { $0.strength > $1.strength }.prefix(16))))
        }
        return rows
    }
}

/// Tracks a temporally consistent pair of observed near-road boundaries.
/// Each boundary is followed independently, so far points can cross the image
/// center on a bend. Short gaps are interpolated; unseen road is not extended.
public struct NeuralLaneEstimator {
    private struct Point { let y: Double; let x: Double; let score: Double }
    private struct Seed { let left: Point; let right: Point; let cost: Double }
    private struct Fit { let bands: [LaneBand]; let confidence: Double; let cost: Double }
    private var previous: LanePath?
    private var consecutive = 0
    private var lastUpdate: TimeInterval?
    public private(set) var status = "waiting for lane scores"
    public init() {}
    public mutating func reset() {
        previous = nil; consecutive = 0; lastUpdate = nil; status = "waiting for lane scores"
    }

    public mutating func update(_ observations: [LaneMarkingRow], at time: TimeInterval) -> LanePath? {
        guard time.isFinite else { reset(); return nil }
        if let lastUpdate {
            guard time > lastUpdate else { reset(); return nil }
            if time - lastUpdate > 1.10 { previous = nil; consecutive = 0 }
        }
        lastUpdate = time
        // A slow model can still confirm a new, matching observation. This
        // fitting prior never makes an old path usable for lead selection.
        let old = previous.flatMap { time - $0.capturedAt <= 1.10 ? $0 : nil }
        if old == nil { previous = nil; consecutive = 0 }
        let rows = observations.filter { row in
            row.y.isFinite && (0.025...0.65).contains(row.y)
        }.sorted { $0.y < $1.y }.map { row in
            LaneMarkingRow(y: row.y, peaks: row.peaks.filter {
                $0.x.isFinite && $0.strength.isFinite && (0.01...0.99).contains($0.x)
                    && (0.5...1).contains($0.strength)
            })
        }
        guard rows.count >= 8,
              zip(rows, rows.dropFirst()).allSatisfy({ $0.y < $1.y }) else {
            status = "no lane observations"; return held(at: time)
        }

        // Seed from near-road observations, including separate rows for dashed
        // left/right markings. Prefer a nearby pair consistent with the prior;
        // image center is a preference rather than a required dividing line.
        // On a wide camera, the hood and image edges can hide both boundaries
        // below y=0.25. Start at the first supported pair above them.
        let nearPoints = rows.filter { $0.y <= 0.40 }.flatMap { row in
            row.peaks.map { Point(y: row.y, x: $0.x, score: $0.strength) }
        }
        var seeds: [Seed] = []
        for left in nearPoints {
            for right in nearPoints where right.x > left.x {
                guard abs(left.y - right.y) <= 0.09,
                      (0.12...0.90).contains(right.x - left.x) else { continue }
                let expectedWidth = 2 * (LeadCorridor().halfWidth(at: (left.y + right.y) / 2) ?? 0.25)
                let y = (left.y + right.y) / 2
                let anchor = old?.bounds(at: y).map { ($0.left + $0.right) / 2 } ?? 0.5
                let center = (left.x + right.x) / 2
                let cost = (right.x - left.x) / expectedWidth + 2 * abs(left.y - right.y)
                    + 1.3 * (left.y + right.y) + 4 * abs(center - anchor)
                    - 0.05 * (left.score + right.score)
                seeds.append(Seed(left: left, right: right, cost: cost))
            }
        }
        guard let nearestY = seeds.map({ min($0.left.y, $0.right.y) }).min() else {
            status = "only one boundary or no supported pair"; return held(at: time)
        }
        // Try distinct near-road pairs. A short, bright false marking must not
        // prevent a well-supported adjacent pair from being fitted. Do not jump
        // over a long occlusion to a disconnected far-road segment.
        let choices = seeds.filter { min($0.left.y, $0.right.y) <= nearestY + 0.10 }
            .sorted { $0.cost < $1.cost }
        var tried: [Seed] = []
        var fits: [Fit] = []
        for seed in choices {
            if tried.contains(where: { abs($0.left.x - seed.left.x) < 0.012
                && abs($0.right.x - seed.right.x) < 0.012
                && abs($0.left.y - seed.left.y) < 0.025 }) { continue }
            tried.append(seed)
            if let fit = fit(seed, rows: rows, prior: old) { fits.append(fit) }
            if tried.count >= 32 { break }
        }
        guard let fit = fits.min(by: { $0.cost < $1.cost }) else {
            status = "boundaries incomplete or inconsistent"; return held(at: time)
        }
        var bands = fit.bands

        // A changed lane needs two new observations before replacing a track.
        if let old {
            let shared = bands.compactMap { band -> Double? in
                guard let prior = old.bounds(at: band.y) else { return nil }
                return max(abs(prior.left - band.left), abs(prior.right - band.right))
            }
            if shared.count < 5 || shared.reduce(0, +) / Double(shared.count) > 0.10 { consecutive = 0 }
            if consecutive > 0 {
                bands = bands.map { band in
                    guard let prior = old.bounds(at: band.y) else { return band }
                    return LaneBand(y: band.y, left: 0.25 * prior.left + 0.75 * band.left,
                                    right: 0.25 * prior.right + 0.75 * band.right)
                }
            }
        }
        let candidate = LanePath(bands: bands, confidence: fit.confidence, capturedAt: time, source: .neural)
        previous = candidate; consecutive += 1
        status = consecutive >= 2 ? "measured lane confirmed" : "confirming measured lane"
        return consecutive >= 2 ? candidate : nil
    }

    private func fit(_ seed: Seed, rows: [LaneMarkingRow], prior: LanePath?) -> Fit? {
        let left = trace(from: seed.left, rows: rows, prior: prior, left: true)
        let right = trace(from: seed.right, rows: rows, prior: prior, left: false)
        guard left.count >= 5, right.count >= 5, let lastLeft = left.last,
              let lastRight = right.last else { return nil }
        let bottom = max(left[0].y, right[0].y), top = min(lastLeft.y, lastRight.y)
        guard top - bottom >= 0.12 else { return nil }
        var bands: [LaneBand] = []
        for index in 0..<24 {
            let y = bottom + (top - bottom) * Double(index) / 23
            guard let l = interpolate(left, at: y), let r = interpolate(right, at: y),
                  (0.045...0.90).contains(r - l) else { break }
            bands.append(LaneBand(y: y, left: l, right: r))
        }
        guard bands.count >= 8, let near = bands.first, let far = bands.last,
              far.y - near.y >= 0.12,
              bands.allSatisfy({ $0.right - $0.left < near.right - near.left + 0.12 }) else { return nil }
        let supportedRows = max(1, rows.filter { $0.y >= bottom && $0.y <= far.y }.count)
        let coverage = min(1, Double(min(left.count, right.count)) / Double(supportedRows))
        guard coverage >= 0.35 else { return nil }
        let meanScore = (left + right).reduce(0.0) { $0 + $1.score } / Double(left.count + right.count)
        let priorCost = bands.compactMap { band -> Double? in
            guard let old = prior?.bounds(at: band.y) else { return nil }
            return abs(band.left - old.left) + abs(band.right - old.right)
        }
        let continuity = priorCost.isEmpty ? 0 : priorCost.reduce(0, +) / Double(priorCost.count)
        let maximumGap = (zip(left, left.dropFirst()).map { $1.y - $0.y }
            + zip(right, right.dropFirst()).map { $1.y - $0.y }).max() ?? 0
        return Fit(bands: bands, confidence: min(0.95, 0.45 + 0.30 * meanScore + 0.15 * coverage),
                   cost: seed.cost + 0.8 * (1 - coverage) - 1.4 * (far.y - near.y)
                    + 4 * continuity + 6 * maximumGap)
    }

    private func trace(from seed: Point, rows: [LaneMarkingRow], prior: LanePath?, left: Bool) -> [Point] {
        var points = [seed]
        for row in rows where row.y > seed.y + 0.0001 {
            guard let last = points.last else { break }
            let gap = row.y - last.y
            if gap > 0.16 { break }
            var expected = last.x
            if points.count >= 2 {
                let before = points[points.count - 2]
                let slope = max(-3.5, min(3.5, (last.x - before.x) / (last.y - before.y)))
                expected += slope * gap
            } else if let bounds = prior?.bounds(at: row.y) {
                expected = left ? bounds.left : bounds.right
            }
            let window = 0.045 + 0.60 * gap
            let chosen = row.peaks.filter { abs($0.x - expected) <= window }.max {
                $0.strength - 6 * abs($0.x - expected) < $1.strength - 6 * abs($1.x - expected)
            }
            if let chosen { points.append(Point(y: row.y, x: chosen.x, score: chosen.strength)) }
        }
        return points
    }

    private func interpolate(_ points: [Point], at y: Double) -> Double? {
        guard let first = points.first, let last = points.last, y >= first.y, y <= last.y else { return nil }
        if y == first.y { return first.x }
        for index in 1..<points.count {
            let a = points[index - 1], b = points[index]
            if y <= b.y {
                guard b.y > a.y, b.y - a.y <= 0.16 else { return nil }
                return a.x + (b.x - a.x) * (y - a.y) / (b.y - a.y)
            }
        }
        return last.x
    }

    private mutating func held(at time: TimeInterval) -> LanePath? {
        guard let previous, previous.isUsable(at: time) else {
            self.previous = nil; consecutive = 0; return nil
        }
        guard consecutive >= 2 else { return nil }
        return previous // preserve capture time; a gap is never a fresh observation
    }
}

/// A small, unrotated luma image in camera order (top-left origin).
public struct LaneLumaFrame {
    public let width: Int
    public let height: Int
    public let pixels: [UInt8]
    public let capturedAt: TimeInterval
    public init?(width: Int, height: Int, pixels: [UInt8], capturedAt: TimeInterval) {
        guard width >= 64, height >= 64, width <= 640, height <= 640,
              pixels.count == width * height, capturedAt.isFinite else { return nil }
        self.width = width; self.height = height; self.pixels = pixels; self.capturedAt = capturedAt
    }
}

public struct LaneModelMeasurement {
    public let lane: LanePath
    public let frame: LaneLumaFrame
    public init?(lane: LanePath, frame: LaneLumaFrame) {
        guard lane.source == .neural, abs(lane.capturedAt - frame.capturedAt) < 0.001,
              lane.isUsable(at: frame.capturedAt) else { return nil }
        self.lane = lane; self.frame = frame
    }
}

/// Lane inference can finish a newer frame while YOLO processes an older one.
/// Keep a bounded history and pick the newest observation no later than the
/// vehicle frame. A future mask must not hide a valid older observation.
public struct LaneMeasurementBuffer {
    private var measurements: [LaneModelMeasurement] = []
    public init() {}
    public mutating func reset() { measurements = [] }
    public mutating func append(_ measurement: LaneModelMeasurement) {
        let time = measurement.lane.capturedAt
        guard measurements.last.map({ time > $0.lane.capturedAt }) ?? true else { return }
        measurements.removeAll { time - $0.lane.capturedAt > 1.2 }
        measurements.append(measurement)
        if measurements.count > 6 { measurements.removeFirst(measurements.count - 6) }
    }
    public func latest(at time: TimeInterval) -> LaneModelMeasurement? {
        guard time.isFinite else { return nil }
        return measurements.last { time >= $0.lane.capturedAt && time - $0.lane.capturedAt <= 1.0 }
    }
}

/// Patch correspondences align a measured neural lane to a newer camera frame.
/// Always match back to the original model frame: no accumulating drift and no
/// indefinite timestamp refresh. Untextured/occluded/ambiguous patches fail.
public enum LaneVisualTracker {
    private struct Match { let y: Double; let x: Double; let newY: Double; let error: Double }

    public static func project(_ lane: LanePath, from source: LaneLumaFrame, to current: LaneLumaFrame,
                               excluding boxes: [CGRect] = []) -> LanePath? {
        let age = current.capturedAt - source.capturedAt
        guard lane.source == .neural, source.width == current.width, source.height == current.height,
              abs(lane.capturedAt - source.capturedAt) < 0.001, age >= 0, age <= 1.0,
              lane.isUsable(at: source.capturedAt) else { return nil }
        if age == 0 { return lane }
        let left = matches(lane, left: true, from: source, to: current, excluding: boxes)
        let right = matches(lane, left: false, from: source, to: current, excluding: boxes)
        guard left.count >= 5, right.count >= 5, let firstLeft = left.first, let firstRight = right.first,
              let lastLeft = left.last, let lastRight = right.last else { return nil }
        let bottom = max(firstLeft.newY, firstRight.newY), top = min(lastLeft.newY, lastRight.newY)
        guard top - bottom >= 0.10 else { return nil }
        var bands: [LaneBand] = []
        for index in 0..<24 {
            let y = bottom + (top - bottom) * Double(index) / 23
            guard let l = interpolate(left, at: y), let r = interpolate(right, at: y),
                  l >= 0, r <= 1, (0.045...0.90).contains(r - l) else { return nil }
            bands.append(LaneBand(y: y, left: l, right: r))
        }
        let meanError = (left + right).reduce(0) { $0 + $1.error } / Double(left.count + right.count)
        let confidence = max(0.45, lane.confidence - 0.10 * age - 0.002 * meanError)
        let result = LanePath(bands: bands, confidence: confidence, capturedAt: current.capturedAt, source: .neural)
        return result.isUsable(at: current.capturedAt) ? result : nil
    }

    private static func matches(_ lane: LanePath, left: Bool, from old: LaneLumaFrame,
                                to new: LaneLumaFrame, excluding boxes: [CGRect]) -> [Match] {
        var matches: [Match] = []
        let step = max(1, lane.bands.count / 12)
        for index in stride(from: 0, to: lane.bands.count, by: step) {
            let band = lane.bands[index], x = left ? band.left : band.right
            let px = Int((x * Double(old.width - 1)).rounded())
            let py = Int(((1 - band.y) * Double(old.height - 1)).rounded())
            guard px >= 4, px < old.width - 4, py >= 4, py < old.height - 4,
                  !boxes.contains(where: { $0.insetBy(dx: -0.012, dy: -0.012)
                    .contains(CGPoint(x: CGFloat(x), y: CGFloat(band.y))) }) else { continue }
            var patch: [Double] = []
            for dy in -3...3 { for dx in -3...3 {
                patch.append(Double(old.pixels[(py + dy) * old.width + px + dx]))
            } }
            let mean = patch.reduce(0, +) / 49
            let variance = patch.reduce(0) { $0 + ($1 - mean) * ($1 - mean) } / 49
            guard variance >= 81 else { continue }
            let centered = patch.map { $0 - mean }
            // Rank all offsets with nine samples, then verify a small set
            // with the full 7x7 patch. The old exhaustive two-pass 49-pixel
            // comparison spent over a million pixel operations per frame.
            let sampleOffsets = [-3, 0, 3]
            let sparse = sampleOffsets.flatMap { ry in sampleOffsets.map { rx in
                Double(old.pixels[(py + ry) * old.width + px + rx])
            } }
            let sparseMean = sparse.reduce(0, +) / 9
            var costs: [(dx: Int, dy: Int, cost: Double)] = []
            for dy in -6...6 { for dx in -18...18 {
                let cx = px + dx, cy = py + dy
                guard cx >= 3, cx < new.width - 3, cy >= 3, cy < new.height - 3 else { continue }
                let nx = Double(cx) / Double(new.width - 1), ny = 1 - Double(cy) / Double(new.height - 1)
                guard !boxes.contains(where: { $0.insetBy(dx: -0.012, dy: -0.012)
                    .contains(CGPoint(x: CGFloat(nx), y: CGFloat(ny))) }) else { continue }
                var total = 0.0
                for ry in sampleOffsets { for rx in sampleOffsets {
                    total += Double(new.pixels[(cy + ry) * new.width + cx + rx])
                } }
                let newMean = total / 9
                var error = 0.0, offset = 0
                for ry in sampleOffsets { for rx in sampleOffsets {
                    error += abs(Double(new.pixels[(cy + ry) * new.width + cx + rx]) - newMean - (sparse[offset] - sparseMean))
                    offset += 1
                } }
                costs.append((dx, dy, error / 9 + 0.04 * Double(abs(dx) + abs(dy))))
            } }
            let ranked = costs.sorted { $0.cost < $1.cost }
            func verified(_ candidate: (dx: Int, dy: Int, cost: Double)) -> (dx: Int, dy: Int, cost: Double) {
                let cx = px + candidate.dx, cy = py + candidate.dy
                var total = 0.0
                for ry in -3...3 { for rx in -3...3 { total += Double(new.pixels[(cy + ry) * new.width + cx + rx]) } }
                let newMean = total / 49
                var error = 0.0, offset = 0
                for ry in -3...3 { for rx in -3...3 {
                    error += abs(Double(new.pixels[(cy + ry) * new.width + cx + rx]) - newMean - centered[offset]); offset += 1
                } }
                return (candidate.dx, candidate.dy, error / 49 + 0.04 * Double(abs(candidate.dx) + abs(candidate.dy)))
            }
            let verifiedCandidates = ranked.prefix(12).map(verified)
            guard let best = verifiedCandidates.min(by: { $0.cost < $1.cost }), best.cost < 18 else { continue }
            let alternatives = ranked.filter { abs($0.dx - best.dx) >= 4 }.prefix(4).map(verified)
            guard let alternative = (verifiedCandidates + alternatives).filter({ abs($0.dx - best.dx) >= 4 })
                    .map({ $0.cost }).min(), alternative > best.cost + 3 else { continue }
            let newY = band.y - Double(best.dy) / Double(new.height - 1)
            let newX = x + Double(best.dx) / Double(new.width - 1)
            if let last = matches.last {
                guard newY > last.newY, newY - last.newY <= 0.16,
                      abs((newX - x) - (last.x - (lane.bounds(at: last.y).map { left ? $0.left : $0.right } ?? last.x))) < 0.035
                else { continue }
            }
            matches.append(Match(y: band.y, x: newX, newY: newY, error: best.cost))
        }
        return matches
    }

    private static func interpolate(_ points: [Match], at y: Double) -> Double? {
        guard let first = points.first, let last = points.last, y >= first.newY, y <= last.newY else { return nil }
        for index in 1..<points.count where y <= points[index].newY {
            let a = points[index - 1], b = points[index]
            guard b.newY > a.newY, b.newY - a.newY <= 0.16 else { return nil }
            return a.x + (b.x - a.x) * (y - a.newY) / (b.newY - a.newY)
        }
        return last.x
    }
}
