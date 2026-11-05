import Foundation

/// How early the brake warning fires. Standard is the critical closing-time gate alone;
/// Earlier also promotes a high-risk closing lead whose estimated collision time is under
/// `earlierSeconds`. The option can only bring warnings forward, never delay them.
public enum BrakeWarningTiming: String, CaseIterable, Sendable {
    case standard, earlier

    public static let defaultsKey = "safety.brakeWarningTiming"
    public static let earlierSeconds = 2.6

    public var title: String {
        switch self {
        case .standard: return "Standard"
        case .earlier: return "Earlier"
        }
    }

    public func warrantsBrakeWarning(level: RiskLevel, timeToCollision: Double?) -> Bool {
        if level == .critical { return true }
        guard self == .earlier, level == .high, let ttc = timeToCollision, ttc.isFinite, ttc > 0 else { return false }
        return ttc < Self.earlierSeconds
    }
}
