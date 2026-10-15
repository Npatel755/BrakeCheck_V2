import BrakeCheckCore
import SwiftUI

@main
@MainActor
struct BrakeCheckApp: App {
    @StateObject private var model = DriveModel()
    @AppStorage("display.appearance") private var appearance = "dark"
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            ContentView(model: model)
                // Red Hat Display for any text without its own font (forms, labels, buttons).
                .font(.brake(.body))
                .preferredColorScheme(appearance == "light" ? .light : .dark)
                .onChange(of: scenePhase) { _, phase in model.sceneChanged(phase) }
        }
    }
}
