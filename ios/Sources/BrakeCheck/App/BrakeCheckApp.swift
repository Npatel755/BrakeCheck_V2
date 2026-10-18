import BrakeCheckCore
import SwiftUI

@main
@MainActor
struct BrakeCheckApp: App {
    @StateObject private var model = DriveModel()
    @StateObject private var plus = PlusStore()
    @AppStorage("display.appearance") private var appearance = "dark"
    @AppStorage("onboarding.driverAidNotice") private var acceptedNotice = 0
    @Environment(\.scenePhase) private var scenePhase

    private var showingNotice: Binding<Bool> {
        Binding(get: { acceptedNotice < 1 }, set: { _ in })
    }

    var body: some Scene {
        WindowGroup {
            ContentView(model: model)
                .environmentObject(plus)
                // Red Hat Display for any text without its own font (forms, labels, buttons).
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
