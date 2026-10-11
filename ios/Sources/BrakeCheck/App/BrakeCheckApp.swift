import BrakeCheckCore
import SwiftUI

@main
@MainActor
struct BrakeCheckApp: App {
    @StateObject private var model = DriveModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            ContentView(model: model)
                .preferredColorScheme(.dark)
                .onChange(of: scenePhase) { _, phase in model.sceneChanged(phase) }
        }
    }
}
