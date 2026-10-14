import Foundation
#if canImport(CoreGraphics)
import CoreGraphics
#endif

/// Original UFLD ResNet18 CULane classifier, not a pixel segmentation head.
/// Rows and x positions refer to the stretched full camera frame. The last
/// grid cell is the explicit no-lane class. Softmax is stabilized before use.
public enum UFLDLaneDecoder {
    public static let anchors = [121, 131, 141, 150, 160, 170, 180, 189, 199,
                                 209, 219, 228, 238, 248, 258, 267, 277, 287]
    public static func decode(excluding boxes: [CGRect] = [],
                              logits: (Int, Int, Int) -> Double) -> [LaneMarkingRow] {
        anchors.enumerated().map { row, anchor in
            let y = 1 - Double(anchor) / 288
            var peaks: [LanePeak] = []
            for lane in 0..<4 {
                let values = (0...200).map { logits($0, row, lane) }
                guard values.allSatisfy(\.isFinite), let maximum = values.max(),
                      let winner = values.firstIndex(of: maximum), winner != 200 else { continue }
                let weights = values.map { exp($0 - maximum) }
                let total = weights.reduce(0, +)
                let present = 1 - weights[200] / total
                guard present >= 0.55 else { continue }
                let gridTotal = weights.prefix(200).reduce(0, +)
                guard gridTotal > 0 else { continue }
                let expectation = (0..<200).reduce(0.0) { $0 + Double($1 + 1) * weights[$1] } / gridTotal
                let x = (expectation * 799 / 199 - 1) / 800
                guard (0.01...0.99).contains(x), !boxes.contains(where: {
                    $0.insetBy(dx: -0.008, dy: -0.008).contains(CGPoint(x: x, y: y))
                }) else { continue }
                peaks.append(LanePeak(x: x, strength: present))
            }
            return LaneMarkingRow(y: y, peaks: peaks)
        }.sorted { $0.y < $1.y }
    }
}
