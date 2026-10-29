import Foundation
#if canImport(CoreGraphics)
import CoreGraphics
#endif

/// The trained network resizes the full camera frame, then keeps its bottom
/// 320 rows. Output anchors already describe the full frame: do not apply this
/// transform to decoded lane coordinates a second time.
public enum UFLDv2InputTransform {
    public static let inputWidth = 1600
    public static let inputHeight = 320
    public static let resizedHeight = 533
    public static let cropOffset = 213
    public static let cropTopFraction = Double(cropOffset) / Double(resizedHeight)
    public static let cropHeightFraction = Double(inputHeight) / Double(resizedHeight)

    public static func fullFrameTopY(fromInputTopY y: Double) -> Double {
        (Double(cropOffset) + y * Double(inputHeight)) / Double(resizedHeight)
    }
    public static func inputTopY(fromFullFrameTopY y: Double) -> Double {
        (y * Double(resizedHeight) - Double(cropOffset)) / Double(inputHeight)
    }
}

/// UFLDv2 hybrid row/column decoder matching the frozen training evaluator.
/// Input subscripts are (axis, bin/class, anchor, zero-based lane slot).
/// Returned points use full-frame normalized coordinates with bottom-left y.
public enum UFLDv2LaneDecoder {
    public enum Axis: Equatable, Sendable { case row, column }
    public struct Point: Equatable, Sendable {
        public let x: Double
        public let y: Double
        public let strength: Double
        public let anchorIndex: Int
    }
    public struct Candidate: Equatable, Sendable {
        /// Slots 1 and 2 are ego left/right; 0 and 3 are their outer neighbors.
        public let laneSlot: Int
        public let axis: Axis
        public let positiveAnchors: Int
        public let meanExistenceProbability: Double
        public let points: [Point]
    }

    public static let rowBinCount = 200
    public static let columnBinCount = 100
    public static let rowAnchorCount = 72
    public static let columnAnchorCount = 81
    public static let rowMinimumAnchors = 37
    public static let columnMinimumAnchors = 21
    public static let rowAnchors = (0..<72).map { 0.42 + 0.58 * Double($0) / 71 }
    public static let columnAnchors = (0..<81).map { Double($0) / 80 }

    /// Selects only each slot's trained inference head. Gates count positive
    /// existence anchors before coordinate filtering, as in the evaluator.
    /// No relaxed threshold from exploratory validation is promoted here.
    public static func decodeCandidates(egoOnly: Bool = false,
        location: (Axis, Int, Int, Int) -> Double,
        existence: (Axis, Int, Int, Int) -> Double
    ) -> [Candidate] {
        candidates(egoOnly: egoOnly, rowMinimum: rowMinimumAnchors,
            columnMinimum: columnMinimumAnchors, minimumPoints: 2, minimumProbability: 0,
            location: location, existence: existence)
    }

    /// Recall-budget v4 display policy. These individual painted-line
    /// predictions are not a driving corridor, calibration input, or safety
    /// evidence. Preserve the stock gates above for every such consumer.
    public static func displayCandidates(
        location: (Axis, Int, Int, Int) -> Double,
        existence: (Axis, Int, Int, Int) -> Double
    ) -> [Candidate] {
        candidates(egoOnly: false, rowMinimum: 2, columnMinimum: 2,
            minimumPoints: 2, minimumProbability: 0,
            location: location, existence: existence).filter(acceptsRecallDisplay)
    }

    /// Decode each trained head once for the independent camera consumers.
    /// Ego safety candidates retain the exact stock span gates; display uses
    /// its separate short-span confidence policy. No relaxed evidence leaks.
    public static func cameraCandidates(
        location: (Axis, Int, Int, Int) -> Double,
        existence: (Axis, Int, Int, Int) -> Double
    ) -> (ego: [Candidate], display: [Candidate]) {
        let all = candidates(egoOnly: false, rowMinimum: 1, columnMinimum: 1,
            minimumPoints: 2, minimumProbability: 0, location: location, existence: existence)
        let ego = all.filter { ($0.laneSlot == 1 || $0.laneSlot == 2)
            && $0.positiveAnchors >= rowMinimumAnchors }
        let display = all.filter(acceptsRecallDisplay)
        return (ego, display)
    }

    /// Same normalized geometry as Python recall_budget_v4. This gate runs on
    /// the complete observed candidate before presentation splits/masks it.
    /// It never supplies the independent stock-gated driving corridor.
    public static func acceptsRecallDisplay(_ candidate: Candidate) -> Bool {
        let primaryHead = candidate.axis == .row
            ? (candidate.laneSlot == 1 || candidate.laneSlot == 2)
            : (candidate.laneSlot == 0 || candidate.laneSlot == 3)
        guard primaryHead, candidate.positiveAnchors >= 2, candidate.points.count >= 4,
              candidate.meanExistenceProbability.isFinite,
              (LanePredictionDisplay.minimumConfidence...1).contains(candidate.meanExistenceProbability),
              candidate.points.allSatisfy({ $0.x.isFinite && $0.y.isFinite }) else { return false }
        let points = candidate.points
        let chord = hypot(points.last!.x - points.first!.x, points.last!.y - points.first!.y)
        guard chord > 1e-6 else { return false }
        var length = 0.0
        var previousVector: (x: Double, y: Double, length: Double)?
        for (a, b) in zip(points, points.dropFirst()) {
            let dx = b.x - a.x, dy = b.y - a.y
            let step = hypot(dx, dy)
            guard step <= 0.15 else { return false }
            length += step
            if step > 1e-6 {
                if let previousVector {
                    let cosine = (dx * previousVector.x + dy * previousVector.y) / (step * previousVector.length)
                    let turn = acos(max(-1, min(1, cosine))) * 180 / Double.pi
                    guard turn <= 150 else { return false }
                }
                previousVector = (dx, dy, step)
            }
        }
        return length / chord <= 2
    }

    private static func candidates(egoOnly: Bool, rowMinimum: Int, columnMinimum: Int,
        minimumPoints: Int, minimumProbability: Double,
        location: (Axis, Int, Int, Int) -> Double,
        existence: (Axis, Int, Int, Int) -> Double
    ) -> [Candidate] {
        var result: [Candidate] = []
        for slot in 0..<4 {
            if egoOnly && slot != 1 && slot != 2 { continue }
            let axis: Axis = (slot == 1 || slot == 2) ? .row : .column
            let bins = axis == .row ? rowBinCount : columnBinCount
            let anchors = axis == .row ? rowAnchors : columnAnchors
            let minimum = axis == .row ? rowMinimum : columnMinimum
            var positiveAnchors = 0
            var probabilitySum = 0.0
            var points: [Point] = []
            for (anchorIndex, anchor) in anchors.enumerated() {
                let absent = existence(axis, 0, anchorIndex, slot)
                let present = existence(axis, 1, anchorIndex, slot)
                // argmax([absent,present]) resolves a tie to class 0.
                guard absent.isFinite, present.isFinite, present > absent else { continue }
                positiveAnchors += 1
                let probability = 1 / (1 + exp(-(present - absent)))
                probabilitySum += probability
                var values: [Double] = []
                values.reserveCapacity(bins)
                var winner = 0
                var maximum = -Double.infinity
                var finite = true
                for bin in 0..<bins {
                    let value = location(axis, bin, anchorIndex, slot)
                    if !value.isFinite { finite = false; break }
                    values.append(value)
                    // Strict comparison retains the first maximum on ties.
                    if value > maximum { maximum = value; winner = bin }
                }
                guard finite else { continue }
                var numerator = 0.0
                var denominator = 0.0
                for bin in max(0, winner - 1)...min(bins - 1, winner + 1) {
                    let weight = exp(values[bin] - maximum)
                    numerator += Double(bin) * weight
                    denominator += weight
                }
                guard denominator.isFinite, denominator > 0 else { continue }
                let coordinate = (numerator / denominator + 0.5) / Double(bins - 1)
                let x = axis == .row ? coordinate : anchor
                let topY = axis == .row ? anchor : coordinate
                // The last row/column anchor, and overflowing half-bin
                // coordinates at the image edge, are deliberately omitted.
                guard x.isFinite, topY.isFinite, x >= 0, x < 1, topY >= 0, topY < 1 else { continue }
                points.append(Point(x: x, y: 1 - topY, strength: probability, anchorIndex: anchorIndex))
            }
            if positiveAnchors >= minimum, points.count >= minimumPoints,
               probabilitySum / Double(positiveAnchors) >= minimumProbability {
                result.append(Candidate(laneSlot: slot, axis: axis, positiveAnchors: positiveAnchors,
                    meanExistenceProbability: probabilitySum / Double(positiveAnchors), points: points))
            }
        }
        return result
    }

    /// Emits all 72 common row anchors, including empty rows. Row heads retain
    /// their observed anchor samples; column heads are intersected with each
    /// row without extrapolating beyond their polyline or choosing an arbitrary
    /// branch of a curve with multiple intersections.
    public static func decode(excluding boxes: [CGRect] = [], egoOnly: Bool = false,
                              location: (Axis, Int, Int, Int) -> Double,
                              existence: (Axis, Int, Int, Int) -> Double) -> [LaneMarkingRow] {
        let candidates = decodeCandidates(egoOnly: egoOnly, location: location, existence: existence)
        let expandedBoxes = boxes.map { $0.insetBy(dx: -0.008, dy: -0.008) }
        return rowAnchors.enumerated().map { index, topY in
            let y = 1 - topY
            var peaks: [LanePeak] = []
            for candidate in candidates {
                let point: Point?
                if candidate.axis == .row {
                    point = candidate.points.first { $0.anchorIndex == index }
                } else {
                    point = crossing(candidate.points, y: y)
                }
                guard let point, !expandedBoxes.contains(where: {
                    $0.contains(CGPoint(x: point.x, y: y))
                }) else { continue }
                peaks.append(LanePeak(x: point.x, strength: point.strength))
            }
            return LaneMarkingRow(y: y, peaks: peaks.sorted { $0.x < $1.x })
        }.sorted { $0.y < $1.y }
    }

    private static func crossing(_ points: [Point], y: Double) -> Point? {
        var result: Point?
        let tolerance = 1e-10
        for (a, b) in zip(points, points.dropFirst()) {
            guard y >= min(a.y, b.y) - tolerance, y <= max(a.y, b.y) + tolerance else { continue }
            let deltaY = b.y - a.y
            // A horizontal segment coincident with this row has infinitely
            // many crossings. It does not identify one lane position.
            if abs(deltaY) <= tolerance {
                if abs(y - a.y) <= tolerance { return nil }
                continue
            }
            let t = (y - a.y) / deltaY
            guard t >= -tolerance, t <= 1 + tolerance else { continue }
            let x = a.x + t * (b.x - a.x)
            let strength = a.strength + t * (b.strength - a.strength)
            guard x.isFinite, x >= 0, x < 1, strength.isFinite, strength >= 0.5 else { return nil }
            let point = Point(x: x, y: y, strength: strength, anchorIndex: -1)
            if let previous = result {
                // Adjacent segments share one vertex; that is one crossing.
                guard abs(previous.x - x) <= tolerance else { return nil }
            } else {
                result = point
            }
        }
        return result
    }
}
