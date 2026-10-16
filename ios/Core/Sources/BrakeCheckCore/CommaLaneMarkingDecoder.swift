import Foundation
#if canImport(CoreGraphics)
import CoreGraphics
#endif

/// Extract observed paint runs from comma10k-segnet class 1 (lane markings).
/// The source dataset's class ordering was checked against its original masks.
public enum CommaLaneMarkingDecoder {
    public static func decode(excluding boxes: [CGRect] = [],
                              laneProbability: (Int, Int) -> Double) -> [LaneMarkingRow] {
        let width = 512
        return (0..<24).map { row in
            let y = 0.06 + Double(row) * 0.024
            let pixelY = min(383, max(0, Int((1 - y) * 383)))
            var peaks: [LanePeak] = []
            var start: Int?
            var maximum = 0.0
            for x in 0...width {
                let normalizedX = Double(x) / Double(width - 1)
                let blocked = boxes.contains { box in
                    box.insetBy(dx: -0.008, dy: -0.008).contains(CGPoint(x: normalizedX, y: y))
                }
                let probability = x < width ? laneProbability(x, pixelY) : 0
                let active = !blocked && probability.isFinite && probability >= 0.55
                if active {
                    if start == nil { start = x }
                    maximum = max(maximum, probability)
                } else if let first = start {
                    let end = x - 1
                    if end >= first, end - first <= 18 {
                        let center = Double(first + end) / (2 * Double(width - 1))
                        if (0.01...0.99).contains(center) {
                            peaks.append(LanePeak(x: center, strength: min(1, maximum)))
                        }
                    }
                    start = nil; maximum = 0
                }
            }
            return LaneMarkingRow(y: y, peaks: Array(peaks.sorted { $0.strength > $1.strength }.prefix(14)))
        }
    }
}
