import Foundation

/// Debounces explanatory HUD text only. Confidence and rendering keep their
/// immediate freshness downgrades, and collision alerts never use this filter.
public struct ARStatusPresentation {
    public private(set) var text: String
    private var pending: String?
    private var pendingSince: TimeInterval?
    private var lastTime: TimeInterval?
    public init(initial: String = "Waiting for road tracking") { text = initial }
    public mutating func update(_ proposed: String, at time: TimeInterval) -> String {
        guard time.isFinite else { return text }
        if let lastTime, time < lastTime { pending = nil; pendingSince = nil; return text }
        lastTime = time
        guard proposed != text else { pending = nil; pendingSince = nil; return text }
        if pending != proposed { pending = proposed; pendingSince = time }
        if time - (pendingSince ?? time) >= 0.6 { text = proposed; pending = nil; pendingSince = nil }
        return text
    }
}
