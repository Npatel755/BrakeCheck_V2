import BrakeCheckCore
import SwiftUI

struct DrivingAssistControls: View {
    @ObservedObject private var premium = PremiumStore.shared
    @AppStorage("premium.drivingAssist.trafficStart") private var trafficStart = false
    @AppStorage("premium.drivingAssist.laneDeparture") private var laneDeparture = false
    @AppStorage("premium.drivingAssist.cutIn") private var cutIn = false
    @AppStorage("premium.drivingAssist.greenLight") private var greenLight = false
    var offerUpgrade = true

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Label("Premium driving alerts", systemImage: "sparkles")
                .font(.brake(.headline))
            Text("Optional camera-based previews. All alerts start off, including after subscribing. Enable each one while parked.")
                .font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
            ForEach(DrivingAssistFeature.allCases) { feature in
                Toggle(isOn: binding(feature)) {
                    VStack(alignment: .leading, spacing: 4) {
                        Label(feature.title, systemImage: feature.icon)
                            .font(.brake(.subheadline, weight: .semibold))
                        Text(feature.detail).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                    }
                }
                .disabled(!premium.isSubscribed)
                .accessibilityIdentifier(feature.preferenceKey)
                .accessibilityHint(premium.isSubscribed ? "Optional driving alert" : "Requires an active Premium subscription")
            }
            if !premium.isSubscribed {
                Label("Premium required · alerts are off", systemImage: "lock.fill")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                if offerUpgrade {
                    NavigationLink("View Premium") { PremiumView() }
                }
            }
            Text("These previews can miss events or give false reminders. Lane Departure cannot identify intentional lane changes. Green-light reminders only support a single clear vertical signal and cannot distinguish arrows; confirm the signal applies to your lane and check the road yourself.")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
        }
    }

    private func binding(_ feature: DrivingAssistFeature) -> Binding<Bool> {
        let stored: Binding<Bool>
        switch feature {
        case .trafficStart: stored = $trafficStart
        case .laneDeparture: stored = $laneDeparture
        case .cutIn: stored = $cutIn
        case .greenLight: stored = $greenLight
        }
        return Binding(get: { premium.isSubscribed && stored.wrappedValue }, set: { value in
            guard premium.isSubscribed else { return }
            stored.wrappedValue = value
        })
    }
}
