import Foundation

/// Suggests a break once a trip has run two hours, then once more each hour. It only
/// speaks up while the car is measurably moving, so a driver already parked is not
/// told to stop, and each threshold fires once per trip.
public struct BreakReminder: Equatable, Sendable {
    public static let firstAfterSeconds: TimeInterval = 2 * 3600
    public static let repeatEverySeconds: TimeInterval = 3600

    private var nextThreshold: TimeInterval = BreakReminder.firstAfterSeconds

    public init() {}

    public mutating func reset() { nextThreshold = Self.firstAfterSeconds }

    /// The whole hours on the road when a reminder is due now, else nil.
    public mutating func update(elapsedSeconds: TimeInterval, moving: Bool) -> Int? {
        guard elapsedSeconds.isFinite, elapsedSeconds >= nextThreshold else { return nil }
        guard moving else { return nil }
        let hours = Int((elapsedSeconds / 3600).rounded(.down))
        nextThreshold = (Double(hours) + 1) * 3600
        return hours
    }

    public static func text(hours: Int) -> String {
        "\(hours) hours on the road · take a break when you can"
    }
}
