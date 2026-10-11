import Foundation
#if canImport(CoreGraphics)
import CoreGraphics
#endif

public struct LaneBand {
    public let y: Double
    public let left: Double
    public let right: Double
    public init(y: Double, left: Double, right: Double) { self.y = y; self.left = left; self.right = right }
}
public enum LaneSource: Equatable {
    case markings
    case neural
    case hybrid
}
public struct LanePath {
    public let bands: [LaneBand]
    public let confidence: Double
    public let capturedAt: TimeInterval
    public let source: LaneSource
    public init(bands: [LaneBand], confidence: Double, capturedAt: TimeInterval,
                source: LaneSource = .markings) {
        self.bands = bands.sorted { $0.y < $1.y }
        self.confidence = confidence; self.capturedAt = capturedAt; self.source = source
    }
    public func isUsable(at time: TimeInterval) -> Bool {
        time.isFinite && capturedAt.isFinite && confidence.isFinite && confidence >= 0.45
            && time >= capturedAt && time - capturedAt <= 0.50 && bands.count >= 5
            && bands.allSatisfy { $0.y.isFinite && $0.left.isFinite && $0.right.isFinite
                && (0...1).contains($0.y) && $0.left >= 0 && $0.right <= 1 && $0.right - $0.left > 0.04 }
            && zip(bands, bands.dropFirst()).allSatisfy { $0.y < $1.y }
    }
    public func bounds(at y: Double) -> (left: Double, right: Double)? {
        guard y.isFinite, let first = bands.first, let last = bands.last,
              y >= first.y, y <= last.y else { return nil }
        for index in 1..<bands.count {
            let a = bands[index - 1], b = bands[index]
            if y <= b.y {
                guard b.y > a.y else { return nil }
                let t = (y - a.y) / (b.y - a.y)
                return (a.left + (b.left - a.left) * t, a.right + (b.right - a.right) * t)
            }
        }
        return (last.left, last.right)
    }
}
public struct LanePeak {
    public let x: Double
    public let strength: Double
    public init(x: Double, strength: Double) { self.x = x; self.strength = strength }
}
public struct LaneMarkingRow {
    public let y: Double
    public let peaks: [LanePeak]
    public init(y: Double, peaks: [LanePeak]) { self.y = y; self.peaks = peaks }
}

/// Samples narrow bright/white/yellow ridges in NV12 camera data.
/// This is classical image processing, not a trained lane network.
public enum LaneMarkingScanner {
    public static func scan(width: Int, height: Int, excluding boxes: [CGRect],
                            luma: (Int, Int) -> UInt8,
                            chroma: (Int, Int) -> (UInt8, UInt8)) -> [LaneMarkingRow] {
        guard width >= 64, height >= 64 else { return [] }
        let columns = 320
        let shoulder = max(3, Int(Double(width) * 0.012))
        var rows: [LaneMarkingRow] = []
        for index in 0..<24 {
            let groundY = 0.06 + Double(index) * 0.024
            let rawY = min(height - 3, max(2, Int((1 - groundY) * Double(height - 1))))
            var values = [Double](repeating: 0, count: columns)
            for column in 2..<(columns - 2) {
                let nx = Double(column) / Double(columns - 1)
                // Vehicle paint, headlights and plates are not lane markings.
                let point = CGPoint(x: CGFloat(nx), y: CGFloat(groundY))
                if boxes.contains(where: { box in
                    box.insetBy(dx: -0.008, dy: -0.008).contains(point)
                }) { continue }
                let x = Int(nx * Double(width - 1))
                var score = 0.0
                for dy in [-2, 0, 2] {
                    let y = rawY + dy
                    let light = Double(luma(x, y))
                    let background = (Double(luma(max(0, x - shoulder), y)) +
                                      Double(luma(min(width - 1, x + shoulder), y))) / 2
                    let (cb, cr) = chroma(x, y)
                    let white = abs(Int(cb) - 128) < 28 && abs(Int(cr) - 128) < 28 && light > 145
                    let yellow = cb < 120 && cr > 130 && light > 100
                    let contrast = light - background
                    if (white || yellow) && contrast > 18 {
                        score += min(1, contrast / 80) * (yellow ? 1.05 : 1)
                    }
                }
                values[column] = score / 3
            }
            var peaks: [LanePeak] = []
            var start: Int?
            for column in 0...columns {
                let active = column < columns && values[column] >= 0.24
                if active, start == nil { start = column }
                if !active, let first = start {
                    let end = column - 1
                    if end - first <= 20 {
                        let weight = (first...end).reduce(0.0) { $0 + values[$1] }
                        let position = (first...end).reduce(0.0) { $0 + Double($1) * values[$1] } / weight
                        let strength = (first...end).map { values[$0] }.max() ?? 0
                        peaks.append(LanePeak(x: position / Double(columns - 1), strength: strength))
                    }
                    start = nil
                }
            }
            rows.append(LaneMarkingRow(y: groundY, peaks: Array(peaks.sorted { $0.strength > $1.strength }.prefix(14))))
        }
        return rows
    }
}

/// The paint fallback uses the same observed-boundary association as the
/// neural mask, without claiming that these pixels came from a trained model.
/// Two complete, matching observations are required; timestamps stay honest.
public struct LaneEstimator {
    private var fitter = NeuralLaneEstimator()
    private var latest: LanePath?
    private var lastUpdate: TimeInterval?
    public init() {}
    public mutating func reset() { fitter.reset(); latest = nil; lastUpdate = nil }

    public mutating func update(_ rows: [LaneMarkingRow], at time: TimeInterval) -> LanePath? {
        guard time.isFinite else { reset(); return nil }
        if let prior = lastUpdate, time <= prior { reset(); return nil }
        lastUpdate = time
        if let latest, time - latest.capturedAt > 0.50 { fitter.reset(); self.latest = nil }
        let observations = rows.map { row in
            LaneMarkingRow(y: row.y, peaks: row.peaks.filter {
                $0.strength.isFinite && (0.24...1.1).contains($0.strength)
            }.map { LanePeak(x: $0.x, strength: 0.50 + 0.50 * min(1, $0.strength)) })
        }
        guard let path = fitter.update(observations, at: time),
              let near = path.bands.first, let far = path.bands.last,
              far.y - near.y >= 0.22 else { return nil }
        let age = max(0, time - path.capturedAt)
        let result = LanePath(bands: path.bands, confidence: min(0.90, path.confidence) * (1 - age / 0.80),
                              capturedAt: path.capturedAt, source: .markings)
        latest = result
        return result.isUsable(at: time) ? result : nil
    }
}

/// The full-screen camera uses aspect fill. Overlays must use this SAME rectangle.
public enum CameraGeometry {
    /// Shows the entire camera frame. Preview, gestures and overlays must all
    /// use the returned rectangle; letterboxing does not discard pixels.
    public static func aspectFitRect(in size: CGSize, imageAspect: Double = 16.0 / 9.0) -> CGRect {
        guard size.width > 0, size.height > 0, imageAspect.isFinite, imageAspect > 0 else { return .zero }
        let aspect = CGFloat(imageAspect)
        let width = min(size.width, size.height * aspect)
        let height = width / aspect
        return CGRect(x: (size.width - width) / 2, y: (size.height - height) / 2,
                      width: width, height: height)
    }

    public static func aspectFillRect(in size: CGSize, imageAspect: Double = 16.0 / 9.0) -> CGRect {
        guard size.width > 0, size.height > 0, imageAspect.isFinite, imageAspect > 0 else { return .zero }
        let aspect = CGFloat(imageAspect)
        let width: CGFloat = max(size.width, size.height * aspect)
        let height: CGFloat = width / aspect
        let x: CGFloat = (size.width - width) / 2
        let y: CGFloat = (size.height - height) / 2
        return CGRect(x: x, y: y, width: width, height: height)
    }
}
