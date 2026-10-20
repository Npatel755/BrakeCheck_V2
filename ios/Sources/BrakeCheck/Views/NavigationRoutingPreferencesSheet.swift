import SwiftUI

struct NavigationRoutingPreferencesSheet: View {
    @ObservedObject var navigation: NavigationService
    @Environment(\.dismiss) private var dismiss
    @State private var avoidTolls: Bool
    @State private var avoidHighways: Bool
    @State private var departLater: Bool
    @State private var departure: Date
    @State private var preferOffline: Bool

    init(navigation: NavigationService) {
        self.navigation = navigation
        _avoidTolls = State(initialValue: navigation.avoidTolls)
        _avoidHighways = State(initialValue: navigation.avoidHighways)
        _departLater = State(initialValue: navigation.departureDate != nil)
        _departure = State(initialValue: navigation.departureDate ?? Date().addingTimeInterval(3600))
        _preferOffline = State(initialValue: navigation.preferOfflineRouting)
    }

    var body: some View {
        SwiftUI.NavigationStack {
            Form {
                Section("Driving preferences") {
                    Toggle("Avoid toll roads", isOn: $avoidTolls)
                    Toggle("Avoid highways", isOn: $avoidHighways)
                }
                Section("Departure") {
                    Toggle("Leave later", isOn: $departLater)
                    if departLater { DatePicker("Leave at", selection: $departure, in: Date()..., displayedComponents: [.date, .hourAndMinute]) }
                }
                Section("Offline routing") {
                    Toggle("Use offline road graph", isOn: $preferOffline).disabled(!navigation.offlineGraphReady)
                    Text(navigation.offlineGraphName ?? "Import offline roads from the Offline tab before planning your trip.")
                        .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                }
                Section {
                    Text("Changes recalculate the route. Travel times are estimates; road restrictions and conditions can change.")
                        .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                }
            }
            .scrollContentBackground(.hidden)
            .background(BrakeTheme.background)
            .foregroundStyle(BrakeTheme.text)
            .navigationTitle("Route options")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Apply") {
                        navigation.preferOfflineRouting = preferOffline
                        navigation.updateRoutingPreferences(avoidTolls: avoidTolls, avoidHighways: avoidHighways,
                            departureDate: departLater ? max(Date(), departure) : nil)
                        dismiss()
                    }.fontWeight(.semibold)
                }
            }
        }
        .font(.brake(.body))
        .tint(BrakeTheme.accent)
    }
}
