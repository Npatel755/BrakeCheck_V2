import BrakeCheckCore
import SwiftUI

@main
@MainActor
struct BrakeCheckApp: App {
    @StateObject private var model = DriveModel()
    @AppStorage("display.appearance") private var appearance = "dark"
    /// Version of the driver-aid notice the driver accepted; raise it when the notice changes.
    @AppStorage("onboarding.driverAidNotice") private var acceptedNotice = 0
    @Environment(\.scenePhase) private var scenePhase

    init() {
        BrakeFont.applyToSystemControls()
    }

    private var showingNotice: Binding<Bool> {
        Binding(get: { acceptedNotice < 1 }, set: { _ in })
    }

    var body: some Scene {
        WindowGroup {
            ContentView(model: model)
                .font(.brake(.body))
                .preferredColorScheme(appearance == "light" ? .light : .dark)
                .onChange(of: scenePhase) { _, phase in model.sceneChanged(phase) }
                .fullScreenCover(isPresented: showingNotice) {
                    DriverAidNoticeView { acceptedNotice = 1 }
                        .font(.brake(.body))
                        .preferredColorScheme(appearance == "light" ? .light : .dark)
                }
        }
    }
}
