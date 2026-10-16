import Foundation
#if canImport(CoreGraphics)
import CoreGraphics
#endif

/// Measured road membership, not an ego lane or a direction-of-travel label.
/// In particular, a two-way road region can contain approaching traffic.
public struct RoadRegion {
    public let bands: [LaneBand]
    public let confidence: Double
    public let capturedAt: TimeInterval
    public let visibility: RoadVisibilityMask?
    public init(bands: [LaneBand], confidence: Double, capturedAt: TimeInterval, visibility: RoadVisibilityMask? = nil) {
        self.bands = bands.sorted { $0.y < $1.y }
        self.confidence = confidence; self.capturedAt = capturedAt
        self.visibility = visibility
    }
    public func isUsable(at time: TimeInterval) -> Bool {
        guard time.isFinite, capturedAt.isFinite, confidence.isFinite,
              time >= capturedAt, time - capturedAt <= 0.50, confidence >= 0.65,
              bands.count >= 8, let first = bands.first, let last = bands.last,
              last.y - first.y >= 0.15 else { return false }
        return bands.allSatisfy { $0.y.isFinite && $0.left.isFinite && $0.right.isFinite
            && (0...1).contains($0.y) && $0.left >= 0 && $0.right <= 1
            && $0.right - $0.left >= 0.06 }
            && zip(bands, bands.dropFirst()).allSatisfy { $0.y < $1.y }
    }
    public func bounds(at y: Double) -> (left: Double, right: Double)? {
        LanePath(bands: bands, confidence: confidence, capturedAt: capturedAt).bounds(at: y)
    }
    public func overlap(with box: CGRect, at time: TimeInterval) -> Double? {
        guard isUsable(at: time), LeadVehicle.validBox(box) else { return nil }
        // A segmentation mask excludes the vehicle itself. Sample the small
        // pavement strip immediately below its foot when the exact contact
        // row is occluded, without extending the measured road boundaries.
        guard let interval = bounds(at: Double(box.minY))
            ?? bounds(at: Double(box.minY) - min(0.016, Double(box.height) * 0.12)) else { return nil }
        return max(0, min(Double(box.maxX), interval.right) - max(Double(box.minX), interval.left))
            / Double(box.width)
    }
    /// Reject clearly off-road marking peaks only where this mask observes road.
    /// Unobserved rows and low-quality masks never impose a camera-center crop.
    public func filter(_ rows: [LaneMarkingRow], at time: TimeInterval) -> [LaneMarkingRow] {
        guard isUsable(at: time), confidence >= 0.75 else { return rows }
        return rows.map { row in
            guard let interval = bounds(at: row.y) else { return row }
            return LaneMarkingRow(y: row.y, peaks: row.peaks.filter {
                $0.x >= interval.left - 0.04 && $0.x <= interval.right + 0.04
            })
        }
    }
}

/// Decode the trained road head in original camera coordinates. Short mask
/// holes can be bridged; a large hole requires an observed vehicle occlusion.
/// Follow connected rows rather than inventing a centered trapezoid.
public enum RoadSegmentationScanner {
    private struct Span { let left: Double; let right: Double; let quality: Double }
    public static func scan(transform: LaneImageTransform, excluding boxes: [CGRect] = [],
                            at time: TimeInterval,
                            scores: (Int, Int) -> (background: Double, road: Double)) -> RoadRegion? {
        guard time.isFinite else { return nil }
        let columns = max(64, min(320, Int(transform.contentWidth.rounded())))
        var bands: [LaneBand] = []
        var qualities: [Double] = []
        var previous: Span?
        var missing = 0
        var regions: [RoadRegion] = []
        func finishSection() {
            if !qualities.isEmpty {
                let section = RoadRegion(bands: bands,
                    confidence: min(0.95, qualities.reduce(0, +) / Double(qualities.count)), capturedAt: time)
                if section.isUsable(at: time) { regions.append(section) }
            }
            bands = []; qualities = []; previous = nil; missing = 0
        }
        for row in 0..<40 {
            let y = 0.04 + Double(row) * 0.016
            var foreground = [Bool](repeating: false, count: columns)
            var quality = [Double](repeating: 0, count: columns)
            for column in 0..<columns {
                let x = Double(column) / Double(columns - 1)
                guard let pixel = transform.pixel(x: x, y: y) else { continue }
                let value = scores(pixel.x, pixel.y)
                guard value.background.isFinite, value.road.isFinite,
                      (0...1).contains(value.background), (0...1).contains(value.road),
                      value.road >= 0.55, value.road - value.background >= 0.10 else { continue }
                foreground[column] = true; quality[column] = value.road
            }
            // Close a small stripe or a vehicle-shaped hole only between two
            // observed road runs. This does not extend into unobserved edges.
            var column = 0
            while column < columns {
                if foreground[column] { column += 1; continue }
                let start = column
                while column < columns && !foreground[column] { column += 1 }
                guard start > 0, column < columns else { continue }
                let left = Double(start) / Double(columns - 1), right = Double(column) / Double(columns - 1)
                let occluded = boxes.contains { box in
                    LeadVehicle.validBox(box) && y >= Double(box.minY) && y <= Double(box.maxY)
                        && left >= Double(box.minX) - 0.008 && right <= Double(box.maxX) + 0.008
                        && right - left <= 0.35
                }
                if column - start <= max(3, Int(Double(columns) * 0.025)) || occluded {
                    for index in start..<column { foreground[index] = true }
                }
            }
            var spans: [Span] = []
            column = 0
            while column < columns {
                if !foreground[column] { column += 1; continue }
                let start = column
                while column < columns && foreground[column] { column += 1 }
                let width = Double(column - start) / Double(columns)
                let observed = quality[start..<column].filter { $0 > 0 }
                if width >= 0.06, observed.count >= max(4, (column - start) / 2) {
                    spans.append(Span(left: Double(start) / Double(columns - 1),
                        right: Double(column - 1) / Double(columns - 1),
                        quality: observed.reduce(0, +) / Double(observed.count)))
                }
            }
            let choices = spans.filter { span in
                guard let previous else { return true }
                let overlap = min(span.right, previous.right) - max(span.left, previous.left)
                return overlap > 0.025 && abs(span.left - previous.left) < 0.16
                    && abs(span.right - previous.right) < 0.16
            }
            func bestSpan(_ candidates: [Span]) -> Span? {
                candidates.max { a, b in
                    func rank(_ span: Span) -> Double {
                        let anchor = previous.map { ($0.left + $0.right) / 2 } ?? 0.5
                        return span.right - span.left - 0.35 * abs((span.left + span.right) / 2 - anchor)
                    }
                    return rank(a) < rank(b)
                }
            }
            var best = bestSpan(choices)
            if best == nil, !spans.isEmpty {
                // An isolated hood/paint fragment must not stop scanning the
                // rest of the image. Start a separate connected section; never
                // interpolate between the disconnected components.
                finishSection()
                best = bestSpan(spans)
            }
            guard let best else {
                if previous != nil { missing += 1; if missing > 2 { finishSection() } }
                continue
            }
            missing = 0; previous = best
            bands.append(LaneBand(y: y, left: best.left, right: best.right)); qualities.append(best.quality)
        }
        finishSection()
        // Prefer the strongest observed connected area, not the first blob.
        return regions.max { a, b in
            func support(_ region: RoadRegion) -> Double {
                region.bands.reduce(0) { $0 + ($1.right - $1.left) } * region.confidence
            }
            return support(a) < support(b)
        }
    }
}
