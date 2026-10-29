import BrakeCheckCore
import SwiftUI

struct DrivingAssistControls: View {
    @ObservedObject private var premium = PremiumStore.shared
    @AppStorage("premium.drivingAssist.trafficStart") private var trafficStart = false
    @AppStorage("premium.drivingAssist.greenLight") private var greenLight = false
    var offerUpgrade = true

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Label("Go alerts", systemImage: "sparkles")
                .font(.brake(.headline))
            Text("Optional camera-based reminders while you're stopped. Each one starts off, including after you subscribe. Turn them on while parked.")
                .font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
            ForEach(DrivingAssistFeature.offered) { feature in
                Toggle(isOn: binding(feature)) {
                    VStack(alignment: .leading, spacing: 4) {
                        Label(feature.title, systemImage: feature.icon)
                            .font(.brake(.subheadline, weight: .semibold))
                        Text(feature.detail).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                    }
                }
                .disabled(!premium.isSubscribed)
                .accessibilityIdentifier(feature.preferenceKey)
                .accessibilityHint(premium.isSubscribed ? "Optional driving alert" : "Requires BrakeSense Premium")
            }
            if !premium.isSubscribed {
                Label("Premium required · alerts are off", systemImage: "lock.fill")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                if offerUpgrade {
                    NavigationLink("View Premium") { PremiumView() }
                }
            }
            Text("These reminders can miss a change or fire when nothing changed. Green Light only reads a single, clear vertical signal and can't tell arrows apart. Check that the signal is yours and look at the road before you move.")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
        }
    }

    private func binding(_ feature: DrivingAssistFeature) -> Binding<Bool> {
        let stored: Binding<Bool>
        switch feature {
        case .trafficStart: stored = $trafficStart
        case .greenLight: stored = $greenLight
        // Not offered in v1 (DrivingAssistFeature.offered); nothing can switch them on.
        case .laneDeparture, .cutIn: return .constant(false)
        }
        return Binding(get: { premium.isSubscribed && stored.wrappedValue }, set: { value in
            guard premium.isSubscribed else { return }
            stored.wrappedValue = value
        })
    }
}
