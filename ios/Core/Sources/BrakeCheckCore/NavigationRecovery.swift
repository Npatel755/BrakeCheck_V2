import Foundation

/// Bounded retry cadence; the caller must still require foreground/fresh GPS.
public struct NavigationRecovery {
    public private(set) var retryAt: TimeInterval?
    private var failures = 0
    public init() {}
    public mutating func failed(at now: TimeInterval) {
        guard now.isFinite else { return }
        failures = min(failures + 1, 5)
        retryAt = now + min(60, 5 * pow(2, Double(failures - 1)))
    }
    public func canAttempt(at now: TimeInterval) -> Bool {
        now.isFinite && (retryAt.map { now >= $0 } ?? true)
    }
    public mutating func reset() { failures = 0; retryAt = nil }
}

public enum NavigationManeuver {
    public static func symbol(for instruction: String) -> String {
        let text = instruction.lowercased()
        if text.contains("u-turn") || text.contains("u turn") { return "arrow.uturn.left" }
        if text.contains("roundabout") { return "arrow.clockwise" }
        if text.hasPrefix("merge") { return "arrow.merge" }
        // Road names can contain Left/Right. Only interpret a turn phrase.
        let isTurn = ["turn", "keep", "bear", "slight", "take", "exit"].contains { text.hasPrefix($0) }
        if isTurn {
            let action = text.components(separatedBy: " onto ")[0]
            if action.contains("left") { return "arrow.turn.up.left" }
            if action.contains("right") { return "arrow.turn.up.right" }
        }
        return "arrow.up"
    }
}
