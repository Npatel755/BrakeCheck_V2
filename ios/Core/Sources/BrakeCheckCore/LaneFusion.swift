import Foundation

/// Combines two independently observed lanes only when their shared section
/// agrees. Every output band is inside at least one measured section; a missing
/// boundary or a gap between unrelated lanes is never filled by extrapolation.
public enum LaneFusion {
    public static func combine(neural: LanePath?, markings: LanePath?, at time: TimeInterval) -> LanePath? {
        let model = neural.flatMap { $0.isUsable(at: time) ? $0 : nil }
        let paint = markings.flatMap { $0.isUsable(at: time) ? $0 : nil }
        guard let model else { return paint }
        guard let paint, model.source == .neural, paint.source == .markings,
              abs(model.capturedAt - paint.capturedAt) <= 0.25,
              let modelNear = model.bands.first, let modelFar = model.bands.last,
              let paintNear = paint.bands.first, let paintFar = paint.bands.last else { return model }
        let bottom = max(modelNear.y, paintNear.y), top = min(modelFar.y, paintFar.y)
        guard top - bottom >= 0.10 else { return model }
        for index in 0..<9 {
            let y = bottom + (top - bottom) * Double(index) / 8
            guard let a = model.bounds(at: y), let b = paint.bounds(at: y) else { return model }
            let width = min(a.right - a.left, b.right - b.left)
            let tolerance = min(0.055, 0.22 * width)
            guard max(abs(a.left - b.left), abs(a.right - b.right)) <= tolerance else { return model }
        }
        var levels: [Double] = []
        for y in (model.bands + paint.bands).map(\.y).sorted() {
            if levels.last.map({ y - $0 > 0.0001 }) ?? true { levels.append(y) }
        }
        let bands = levels.compactMap { y -> LaneBand? in
            switch (model.bounds(at: y), paint.bounds(at: y)) {
            case let (.some(a), .some(b)):
                return LaneBand(y: y, left: 0.65 * a.left + 0.35 * b.left,
                                right: 0.65 * a.right + 0.35 * b.right)
            case let (.some(a), .none): return LaneBand(y: y, left: a.left, right: a.right)
            case let (.none, .some(b)): return LaneBand(y: y, left: b.left, right: b.right)
            case (.none, .none): return nil
            }
        }
        let result = LanePath(bands: bands, confidence: min(model.confidence, paint.confidence),
                              capturedAt: min(model.capturedAt, paint.capturedAt), source: .hybrid)
        return result.isUsable(at: time) ? result : model
    }
}
