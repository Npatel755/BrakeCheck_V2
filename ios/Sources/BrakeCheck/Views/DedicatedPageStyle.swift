import SwiftUI

/// Shared appearance for pages reached from Home, outside the swipe tabs.
struct DedicatedPageStyle: ViewModifier {
    @AppStorage("display.accent") private var accentChoice = BrakeTheme.defaultAccent
    func body(content: Content) -> some View {
        content
            .scrollContentBackground(.hidden)
            .background(BrakeTheme.background)
            .foregroundStyle(BrakeTheme.text)
            .tint(BrakeTheme.accentColor(named: accentChoice))
            .toolbar(.visible, for: .navigationBar)
            .toolbarBackground(BrakeTheme.background, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
            .statusBarHidden(false)
    }
}
