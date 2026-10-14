import BrakeCheckCore
import SwiftUI
import UIKit

enum AccentChoice: String, CaseIterable, Identifiable {
    case blue, teal, violet, rose, amber
    var id: String { rawValue }
    var title: String { rawValue.capitalized }
    func color(for style: UIUserInterfaceStyle) -> UIColor {
        let dark = style == .dark
        switch self {
        case .blue: return dark ? UIColor(red: 0.32, green: 0.65, blue: 1, alpha: 1) : UIColor(red: 0.10, green: 0.47, blue: 0.78, alpha: 1)
        case .teal: return dark ? UIColor(red: 0.2, green: 0.8, blue: 0.7, alpha: 1) : UIColor(red: 0.02, green: 0.45, blue: 0.43, alpha: 1)
        case .violet: return dark ? UIColor(red: 0.69, green: 0.57, blue: 1, alpha: 1) : UIColor(red: 0.45, green: 0.32, blue: 0.77, alpha: 1)
        case .rose: return dark ? UIColor(red: 1, green: 0.51, blue: 0.72, alpha: 1) : UIColor(red: 0.68, green: 0.25, blue: 0.49, alpha: 1)
        case .amber: return dark ? UIColor(red: 1, green: 0.75, blue: 0.34, alpha: 1) : UIColor(red: 0.59, green: 0.37, blue: 0.03, alpha: 1)
        }
    }
}

enum BrakeTheme {
    private static func adaptive(light: UIColor, dark: UIColor) -> Color {
        Color(UIColor { $0.userInterfaceStyle == .dark ? dark : light })
    }
    static let background = adaptive(light: .white, dark: .black)
    static var accentName: String { UserDefaults.standard.string(forKey: "display.accent") ?? "blue" }
    static var panel: Color {
        let choice = AccentChoice(rawValue: accentName) ?? .blue
        return Color(UIColor { traits in
            var red: CGFloat = 0, green: CGFloat = 0, blue: CGFloat = 0, alpha: CGFloat = 0
            choice.color(for: traits.userInterfaceStyle).getRed(&red, green: &green, blue: &blue, alpha: &alpha)
            let dark = traits.userInterfaceStyle == .dark
            let base: CGFloat = dark ? 0.04 : 0.94
            let mix: CGFloat = dark ? 0.08 : 0.06
            return UIColor(red: base + red * mix, green: base + green * mix, blue: base + blue * mix, alpha: 1)
        })
    }
    static var accent: Color { accentColor(named: accentName) }
    static func accentColor(named name: String) -> Color {
        let choice = AccentChoice(rawValue: name) ?? .blue
        return Color(UIColor { choice.color(for: $0.userInterfaceStyle) })
    }
    static func accentUIColor(for style: UIUserInterfaceStyle) -> UIColor {
        (AccentChoice(rawValue: accentName) ?? .blue).color(for: style)
    }
    static let cameraSurface = adaptive(light: UIColor.white.withAlphaComponent(0.92),
        dark: UIColor.black.withAlphaComponent(0.65))
    static let cameraScrim = adaptive(light: .white, dark: .black)
    static let muted = adaptive(light: UIColor(red: 0.35, green: 0.43, blue: 0.54, alpha: 1),
        dark: UIColor(red: 0.56, green: 0.63, blue: 0.73, alpha: 1))
    static let text = Color(UIColor.label)
    static let caution = adaptive(light: UIColor(red: 0.60, green: 0.40, blue: 0.02, alpha: 1),
        dark: UIColor(red: 1, green: 0.84, blue: 0.3, alpha: 1))
    static let warning = adaptive(light: UIColor(red: 0.72, green: 0.30, blue: 0.02, alpha: 1),
        dark: UIColor(red: 1, green: 0.58, blue: 0.2, alpha: 1))
    static let danger = Color(red: 1.0, green: 0.27, blue: 0.23)
    /// Legacy decorative accent name; warning colors retain their separate meaning.
    static var amber: Color { accent }

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
    @AppStorage("display.accent") private var accentChoice = "blue"
    func body(content: Content) -> some View {
        content.padding(16).background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 20))
            .overlay(RoundedRectangle(cornerRadius: 20).stroke(BrakeTheme.text.opacity(0.06), lineWidth: 1))
            .tint(BrakeTheme.accentColor(named: accentChoice))
    }
}
extension View { func brakePanel() -> some View { modifier(BrakePanel()) } }
