import BrakeCheckCore
import SwiftUI
import UIKit

enum AccentChoice: String, CaseIterable, Identifiable {
    case polar, blue
    var id: String { rawValue }
    var title: String { self == .polar ? "Polar night" : rawValue.capitalized }
    func color(for style: UIUserInterfaceStyle) -> UIColor {
        let dark = style == .dark
        switch self {
        // Ice-white buttons on navy at night; deep navy buttons on pale blue by day.
        case .polar: return dark ? BrakeTheme.hex(0xCFE6FF) : BrakeTheme.hex(0x0D2A52)
        case .blue: return dark ? UIColor(red: 0.32, green: 0.65, blue: 1, alpha: 1) : UIColor(red: 0.10, green: 0.47, blue: 0.78, alpha: 1)
        }
    }
}

/// Polar night is the default theme: it recolors the surfaces and text, not just the accent.
/// The other choices keep the original black/white surfaces with a colored accent.
enum BrakeTheme {
    static func hex(_ value: UInt32, alpha: CGFloat = 1) -> UIColor {
        UIColor(red: CGFloat((value >> 16) & 0xFF) / 255, green: CGFloat((value >> 8) & 0xFF) / 255,
                blue: CGFloat(value & 0xFF) / 255, alpha: alpha)
    }
    private static func adaptive(light: UIColor, dark: UIColor) -> Color {
        Color(UIColor { $0.userInterfaceStyle == .dark ? dark : light })
    }
    static let defaultAccent = AccentChoice.polar.rawValue
    static var accentName: String { UserDefaults.standard.string(forKey: "display.accent") ?? defaultAccent }
    static var isPolar: Bool { (AccentChoice(rawValue: accentName) ?? .polar) == .polar }

    static var background: Color {
        isPolar ? adaptive(light: hex(0xEAF2FB), dark: hex(0x050A14)) : adaptive(light: .white, dark: .black)
    }
    static var panel: Color {
        if isPolar { return adaptive(light: .white, dark: hex(0x0B1424)) }
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
    /// A step up from panel: chips, inactive tracks, map placeholders.
    static var raised: Color {
        isPolar ? adaptive(light: hex(0xD6E4F4), dark: hex(0x121E35)) : text.opacity(0.08)
    }
    static var accent: Color { accentColor(named: accentName) }
    /// Accent for text and small marks. Polar's ice-white accent reads as plain white in text,
    /// so text uses a light blue there.
    static var accentText: Color {
        isPolar ? adaptive(light: hex(0x0D2A52), dark: hex(0x9FCBFF)) : accent
    }
    static func accentColor(named name: String) -> Color {
        let choice = AccentChoice(rawValue: name) ?? .polar
        return Color(UIColor { choice.color(for: $0.userInterfaceStyle) })
    }
    static func accentUIColor(for style: UIUserInterfaceStyle) -> UIColor {
        (AccentChoice(rawValue: accentName) ?? .polar).color(for: style)
    }
    /// Shadow under accent-filled buttons: a blue glow normally, a plain drop shadow under Polar's
    /// near-white buttons (a glow there looks like a lamp).
    static var buttonShadow: Color {
        isPolar ? Color(UIColor { $0.userInterfaceStyle == .dark ? UIColor.black.withAlphaComponent(0.5) : hex(0x0D2A52, alpha: 0.25) })
            : accent.opacity(0.35)
    }
    static var cameraSurface: Color {
        isPolar ? adaptive(light: UIColor.white.withAlphaComponent(0.92), dark: hex(0x050A14, alpha: 0.7))
            : adaptive(light: UIColor.white.withAlphaComponent(0.92), dark: UIColor.black.withAlphaComponent(0.65))
    }
    static var cameraScrim: Color { isPolar ? adaptive(light: .white, dark: hex(0x050A14)) : adaptive(light: .white, dark: .black) }
    static var muted: Color {
        isPolar ? adaptive(light: hex(0x4A5D78), dark: hex(0x8AA0BC))
            : adaptive(light: UIColor(red: 0.35, green: 0.43, blue: 0.54, alpha: 1), dark: UIColor(red: 0.56, green: 0.63, blue: 0.73, alpha: 1))
    }
    static var text: Color { isPolar ? adaptive(light: hex(0x08162B), dark: hex(0xE4F1FF)) : Color(UIColor.label) }
    static var caution: Color {
        isPolar ? adaptive(light: hex(0x8F5A00), dark: hex(0xFFB547))
            : adaptive(light: UIColor(red: 0.60, green: 0.40, blue: 0.02, alpha: 1), dark: UIColor(red: 1, green: 0.84, blue: 0.3, alpha: 1))
    }
    static var warning: Color {
        isPolar ? adaptive(light: hex(0xB4470A), dark: hex(0xFF7A3D))
            : adaptive(light: UIColor(red: 0.72, green: 0.30, blue: 0.02, alpha: 1), dark: UIColor(red: 1, green: 0.58, blue: 0.2, alpha: 1))
    }
    static var danger: Color { isPolar ? adaptive(light: hex(0xC02626), dark: hex(0xFF4D5E)) : Color(red: 1.0, green: 0.27, blue: 0.23) }
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
        case .low: return accentText
        case .elevated: return caution
        case .high: return warning
        case .critical: return danger
        }
    }
}
struct BrakePanel: ViewModifier {
    @AppStorage("display.accent") private var accentChoice = BrakeTheme.defaultAccent
    func body(content: Content) -> some View {
        content.padding(16).background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 20))
            .overlay(RoundedRectangle(cornerRadius: 20).stroke(BrakeTheme.text.opacity(0.06), lineWidth: 1))
            .tint(BrakeTheme.accentColor(named: accentChoice))
    }
}
extension View { func brakePanel() -> some View { modifier(BrakePanel()) } }
