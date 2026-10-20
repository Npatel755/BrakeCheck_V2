import CoreLocation
import MapKit
import SwiftUI

struct NavigationMapOptions: View {
    @Environment(\.dismiss) private var dismiss
    @AppStorage("navigation.map.satellite") private var satellite = false
    @AppStorage("navigation.map.traffic") private var traffic = true
    @AppStorage("navigation.map.3d") private var threeDimensional = true
    @AppStorage("navigation.map.heading") private var headingUp = true
    @AppStorage("navigation.map.night") private var nightMode = "system"
    @AppStorage("navigation.map.routeColor") private var routeColor = "accent"

    var body: some View {
        SwiftUI.NavigationStack {
            Form {
                Section("Map") {
                    Toggle("Satellite imagery", isOn: $satellite)
                    Toggle("Live traffic", isOn: $traffic)
                    Toggle("3D buildings and camera", isOn: $threeDimensional)
                    Toggle("Travel direction up", isOn: $headingUp)
                    Picker("Map appearance", selection: $nightMode) {
                        Text("Match app").tag("system")
                        Text("Day").tag("light")
                        Text("Night").tag("dark")
                    }
                }
                Section("Guidance") {
                    Picker("Route color", selection: $routeColor) {
                        Text("App accent").tag("accent")
                        ForEach(AccentChoice.allCases) { choice in Text(choice.title).tag(choice.rawValue) }
                    }
                    Text("Route colors and orientation apply to the navigation map. Camera guidance uses the camera page settings.")
                        .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                }
                Section {
                    Text("Traffic, imagery, 3D detail and Look Around depend on Apple Maps coverage and a network connection.")
                        .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                }
            }
            .scrollContentBackground(.hidden)
            .background(BrakeTheme.background)
            .foregroundStyle(BrakeTheme.text)
            .navigationTitle("Map & guidance")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        }
        .font(.brake(.body))
        .tint(BrakeTheme.accent)
    }
}

struct NavigationLookAroundView: View {
    var coordinate: CLLocationCoordinate2D
    var title: String
    @Environment(\.dismiss) private var dismiss
    @State private var scene: MKLookAroundScene?
    @State private var loading = true
    @State private var failed = false

    var body: some View {
        SwiftUI.NavigationStack {
            Group {
                if let scene { NativeLookAround(scene: scene) }
                else if loading { ProgressView("Finding street imagery…") }
                else {
                    ContentUnavailableView(failed ? "Couldn’t load Look Around" : "Look Around unavailable",
                        systemImage: "binoculars",
                        description: Text(failed ? "Check your connection and try again." : "Apple Maps has no street imagery for this destination yet."))
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .scrollContentBackground(.hidden)
            .background(BrakeTheme.background)
            .foregroundStyle(BrakeTheme.text)
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
                if failed { ToolbarItem(placement: .bottomBar) { Button("Try again") { Task { await loadScene() } } } }
            }
            .task { await loadScene() }
        }
    }

    @MainActor private func loadScene() async {
        loading = true
        failed = false
        do { scene = try await MKLookAroundSceneRequest(coordinate: coordinate).scene }
        catch { failed = true }
        loading = false
    }
}

private struct NativeLookAround: UIViewControllerRepresentable {
    var scene: MKLookAroundScene
    func makeUIViewController(context: Context) -> MKLookAroundViewController {
        let controller = MKLookAroundViewController(scene: scene)
        controller.isNavigationEnabled = true
        controller.showsRoadLabels = true
        return controller
    }
    func updateUIViewController(_ controller: MKLookAroundViewController, context: Context) {
        if controller.scene !== scene { controller.scene = scene }
    }
}
