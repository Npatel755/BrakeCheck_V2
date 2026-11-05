import BrakeCheckCore
import SwiftUI

struct DrivingAssistControls: View {
    var model: DriveModel? = nil
    @ObservedObject private var premium = PremiumStore.shared
    @AppStorage("premium.drivingAssist.trafficStart") private var trafficStart = false
    @AppStorage("premium.drivingAssist.greenLight") private var greenLight = false
    @AppStorage("premium.drivingAssist.laneDeparture") private var laneDeparture = false
    @AppStorage("premium.drivingAssist.cutIn") private var cutIn = false
    @AppStorage("premium.drivingAssist.followingDistance") private var followingDistance = false
    var offerUpgrade = true

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Label("Go alerts", systemImage: "sparkles")
                .font(.brake(.headline))
            Text("Optional camera-based reminders while you're stopped. Each one starts off, including after you subscribe. Turn them on while parked.")
                .font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
            ForEach(DrivingAssistFeature.stopped) { feature in toggle(feature) }
            Label("Lane alerts", systemImage: "road.lanes")
                .font(.brake(.headline))
            Text("Camera lane reminders while you're moving. They use the on-device lane model and only work after camera alignment, with reliable GPS speed and clearly visible lane lines.")
                .font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
            ForEach(DrivingAssistFeature.moving) { feature in toggle(feature) }
            if !premium.isSubscribed {
                Label("Premium required · alerts are off", systemImage: "lock.fill")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                if offerUpgrade {
                    NavigationLink("View Premium") { PremiumView(model: model) }
                }
            }
            Text("These reminders can miss a change or fire when nothing changed. Green Light only reads a single, clear vertical signal and can't tell arrows apart. Lane alerts pause during planned turns and can't see unpainted or worn lane lines. Check the signal and the road yourself before you move or steer.")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
        }
    }

    private func toggle(_ feature: DrivingAssistFeature) -> some View {
        Toggle(isOn: binding(feature)) {
            VStack(alignment: .leading, spacing: 4) {
                Label(feature.title, systemImage: feature.icon)
                    .font(.brake(.subheadline, weight: .semibold))
                Text(feature.detail).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                // Without Premium every row would repeat the same lock notice; the
                // single "Premium required" line below covers it.
                if let model, premium.isSubscribed {
                    Text(model.drivingAssistStatuses[feature] ?? "Checking alert status")
                        .font(.brake(.caption, weight: .semibold))
                        .foregroundStyle(BrakeTheme.muted)
                        .accessibilityIdentifier(feature.preferenceKey + ".status")
                }
            }
        }
        .disabled(!premium.isSubscribed)
        .accessibilityIdentifier(feature.preferenceKey)
        .accessibilityHint(premium.isSubscribed ? "Optional driving alert" : "Requires BrakeSense Premium")
    }

    private func binding(_ feature: DrivingAssistFeature) -> Binding<Bool> {
        let stored: Binding<Bool>
        switch feature {
        case .trafficStart: stored = $trafficStart
        case .greenLight: stored = $greenLight
        case .laneDeparture: stored = $laneDeparture
        case .cutIn: stored = $cutIn
        case .followingDistance: stored = $followingDistance
        }
        return Binding(get: { premium.isSubscribed && stored.wrappedValue }, set: { value in
            guard premium.isSubscribed else { return }
            stored.wrappedValue = value
        })
    }
}
