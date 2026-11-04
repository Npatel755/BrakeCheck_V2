// The app's entry point (@main BrakeCheckApp) lives in the BrakeCheck package, the
// same code xtool builds. This file only gives the Xcode app target a source file.
import BrakeCheck
import AppIntents

/// The Siri/Shortcuts intents live in the package; this tells Xcode to extract their metadata.
struct BrakeCheckAppIntentsPackage: AppIntentsPackage {
    static var includedPackages: [any AppIntentsPackage.Type] { [BrakeCheckIntentsPackage.self] }
}

/// Siri phrases have to be declared in the app target; the intents themselves are in the package.
struct BrakeCheckShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: StartTripIntent(),
                    phrases: ["Start a trip in \(.applicationName)", "Start \(.applicationName)", "Start driving with \(.applicationName)"],
                    shortTitle: "Start trip", systemImageName: "car.fill")
        AppShortcut(intent: EndTripIntent(),
                    phrases: ["End the trip in \(.applicationName)", "Stop \(.applicationName)", "End my \(.applicationName) trip"],
                    shortTitle: "End trip", systemImageName: "stop.circle.fill")
    }
}
