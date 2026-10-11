import BrakeCheckCore
import SwiftUI

enum BrakeTheme {
    static let background = Color(red: 0.035, green: 0.048, blue: 0.06)
    static let panel = Color(red: 0.072, green: 0.09, blue: 0.105)
    static let accent = Color(red: 0.63, green: 0.98, blue: 0.78)
    static let muted = Color(red: 0.55, green: 0.63, blue: 0.64)
    static let caution = Color(red: 1.0, green: 0.84, blue: 0.3)
    static let warning = Color(red: 1.0, green: 0.58, blue: 0.2)
    static let danger = Color(red: 1.0, green: 0.27, blue: 0.23)
    /// Logo amber (the colour of a dashboard caution lamp); red stays reserved for the BRAKE alert.
    static let amber = Color(red: 1.0, green: 0.69, blue: 0.125)

    /// Plain-language names for drivers; the risk rules and their raw values are unchanged.
    static func name(for level: RiskLevel) -> String {
        switch level {
        case .unknown: return "No reading"
        case .low: return "Low risk"
        case .elevated: return "Moderate risk"
        case .high: return "High risk"
        case .critical: return "Brake"
        }
    }

    static func color(for level: RiskLevel) -> Color {
        switch level {
        case .unknown: return muted
        case .low: return accent
        case .elevated: return caution
        case .high: return warning
        case .critical: return danger
        }
    }
}
struct BrakePanel: ViewModifier {
    func body(content: Content) -> some View {
        content.padding(16).background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 20))
            .overlay(RoundedRectangle(cornerRadius: 20).stroke(.white.opacity(0.06), lineWidth: 1))
    }
}
extension View { func brakePanel() -> some View { modifier(BrakePanel()) } }
