import Foundation
#if canImport(CoreGraphics)
import CoreGraphics
#endif

/// Display-only observations of individual painted lines. This type deliberately
/// cannot stand in for LanePath: one line never implies an opposite boundary,
/// a drivable corridor, camera calibration, or collision/AR evidence.
public struct LanePredictionObservation: Equatable, Sendable {
    public struct Point: Equatable, Sendable {
        /// Full-frame normalized coordinates, with a bottom-left origin.
        public let x: Double
        public let y: Double
        public init(x: Double, y: Double) { self.x = x; self.y = y }
    }
    public struct Boundary: Equatable, Sendable {
        /// Zero-based trained slot. A slot may have several disconnected pieces.
        public let laneSlot: Int
        public let confidence: Double
        public let points: [Point]
        public init(laneSlot: Int, confidence: Double, points: [Point]) {
            self.laneSlot = laneSlot; self.confidence = confidence; self.points = points
        }
    }
    public let capturedAt: TimeInterval
    public let boundaries: [Boundary]
    public init(capturedAt: TimeInterval, boundaries: [Boundary]) {
        self.capturedAt = capturedAt; self.boundaries = boundaries
    }
}

/// Conservative rendering of the experimental recall_budget_v4 model output.
/// It never extends endpoints, joins long unsupported gaps, or carries an old
/// prediction forward by renewing its capture timestamp.
public enum LanePredictionDisplay {
    /// Guides stay visible across one stretched inference interval; a longer
    /// silence hides them rather than carrying an old prediction forward.
    public static let maximumAge: TimeInterval = 0.75
    public static let minimumConfidence = 0.70
    /// Three missing anchors may occur between neighboring observed samples.
    public static let maximumAnchorStep = 4
    private static let vehicleMargin = 0.008

    public static func observation(candidates: [UFLDv2LaneDecoder.Candidate],
                                   at time: TimeInterval) -> LanePredictionObservation? {
        guard time.isFinite else { return nil }
        var boundaries: [LanePredictionObservation.Boundary] = []
        for candidate in candidates {
            let anchorCount = candidate.axis == .row
                ? UFLDv2LaneDecoder.rowAnchorCount : UFLDv2LaneDecoder.columnAnchorCount
            guard (0..<4).contains(candidate.laneSlot), candidate.positiveAnchors >= 2,
                  validConfidence(candidate.meanExistenceProbability),
                  candidate.points.filter({ valid($0) && (0..<anchorCount).contains($0.anchorIndex) }).count >= 4 else { continue }
            var points: [LanePredictionObservation.Point] = []
            var previousAnchor: Int?
            func finish() {
                if points.count >= 2 {
                    boundaries.append(.init(laneSlot: candidate.laneSlot,
                        confidence: candidate.meanExistenceProbability, points: points))
                }
                points = []; previousAnchor = nil
            }
            for point in candidate.points {
                guard valid(point), (0..<anchorCount).contains(point.anchorIndex) else {
                    finish(); continue
                }
                if let previousAnchor,
                   point.anchorIndex <= previousAnchor || point.anchorIndex - previousAnchor > maximumAnchorStep {
                    finish()
                }
                points.append(.init(x: point.x, y: point.y)); previousAnchor = point.anchorIndex
            }
            finish()
        }
        guard !boundaries.isEmpty else { return nil }
        return .init(capturedAt: time, boundaries: boundaries)
    }

    /// Recheck freshness and current vehicle masks when presenting a prediction.
    /// Whole intersecting segments are omitted, using only observed endpoints;
    /// no interpolated points are invented at a vehicle's silhouette.
    public static func visible(_ observation: LanePredictionObservation?, at time: TimeInterval,
                               excluding boxes: [CGRect] = []) -> LanePredictionObservation? {
        guard let observation, time.isFinite, observation.capturedAt.isFinite,
              time >= observation.capturedAt,
              time - observation.capturedAt <= maximumAge + 1e-9 else { return nil }
        let masks = boxes.filter {
            $0.origin.x.isFinite && $0.origin.y.isFinite && $0.width.isFinite && $0.height.isFinite
                && $0.width > 0 && $0.height > 0
        }.map { $0.insetBy(dx: -vehicleMargin, dy: -vehicleMargin) }
        var boundaries: [LanePredictionObservation.Boundary] = []
        for boundary in observation.boundaries {
            guard (0..<4).contains(boundary.laneSlot), validConfidence(boundary.confidence) else { continue }
            var points: [LanePredictionObservation.Point] = []
            func finish() {
                if points.count >= 2 {
                    boundaries.append(.init(laneSlot: boundary.laneSlot,
                        confidence: boundary.confidence, points: points))
                }
                points = []
            }
            for point in boundary.points {
                guard valid(point), !masks.contains(where: { contains($0, point) }) else {
                    finish(); continue
                }
                if let previous = points.last,
                   masks.contains(where: { intersects($0, from: previous, to: point) }) {
                    finish()
                }
                points.append(point)
            }
            finish()
        }
        guard !boundaries.isEmpty else { return nil }
        return .init(capturedAt: observation.capturedAt, boundaries: boundaries)
    }

    private static func validConfidence(_ confidence: Double) -> Bool {
        confidence.isFinite && (minimumConfidence...1).contains(confidence)
    }
    private static func valid(_ point: UFLDv2LaneDecoder.Point) -> Bool {
        valid(.init(x: point.x, y: point.y)) && point.strength.isFinite && (0.5...1).contains(point.strength)
    }
    private static func valid(_ point: LanePredictionObservation.Point) -> Bool {
        point.x.isFinite && point.y.isFinite && (0...1).contains(point.x) && (0...1).contains(point.y)
    }
    private static func contains(_ rect: CGRect, _ point: LanePredictionObservation.Point) -> Bool {
        point.x >= Double(rect.minX) && point.x <= Double(rect.maxX)
            && point.y >= Double(rect.minY) && point.y <= Double(rect.maxY)
    }
    /// Slab intersection also detects a box crossed by two outside endpoints.
    private static func intersects(_ rect: CGRect, from a: LanePredictionObservation.Point,
                                   to b: LanePredictionObservation.Point) -> Bool {
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
