import SwiftUI

enum BrakeTheme {
    static let background = Color(red: 0.035, green: 0.048, blue: 0.06)
    static let panel = Color(red: 0.072, green: 0.09, blue: 0.105)
    static let accent = Color(red: 0.63, green: 0.98, blue: 0.78)
    static let muted = Color(red: 0.55, green: 0.63, blue: 0.64)
}
struct BrakePanel: ViewModifier {
    func body(content: Content) -> some View {
        content.padding(16).background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 20))
            .overlay(RoundedRectangle(cornerRadius: 20).stroke(.white.opacity(0.06), lineWidth: 1))
    }
}
extension View { func brakePanel() -> some View { modifier(BrakePanel()) } }
