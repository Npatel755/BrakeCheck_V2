import AppIntents
import Foundation

/// Siri / Shortcuts / Action button entry points. The intents only hand a request to the
/// running UI; ContentView performs it through the same path as the Start/End buttons.
struct TripIntentRequest: Equatable, Sendable {
    enum Kind: Sendable { case start, end }
    let kind: Kind
    let id = UUID()
}

@MainActor @Observable
final class TripIntentBridge {
    static let shared = TripIntentBridge()
    private(set) var request: TripIntentRequest?

    func submit(_ kind: TripIntentRequest.Kind) { request = TripIntentRequest(kind: kind) }
    func clear() { request = nil }

    var noticeAccepted: Bool { UserDefaults.standard.integer(forKey: "onboarding.driverAidNotice") >= 1 }
}

struct StartTripIntent: AppIntent {
    static let title: LocalizedStringResource = "Start a trip"
    static let description = IntentDescription("Starts the BrakeSense camera trip with collision warnings.")
    static let openAppWhenRun = true

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard TripIntentBridge.shared.noticeAccepted else {
            return .result(dialog: "Open BrakeSense and accept the driver-aid notice first.")
        }
        TripIntentBridge.shared.submit(.start)
        return .result(dialog: "Starting your trip.")
    }
}

struct EndTripIntent: AppIntent {
    static let title: LocalizedStringResource = "End the trip"
    static let description = IntentDescription("Ends the current BrakeSense trip and saves it.")
    static let openAppWhenRun = true

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        TripIntentBridge.shared.submit(.end)
        return .result(dialog: "Ending your trip.")
    }
}

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

/// Lets an app target that links this package pick up the intents' metadata.
public struct BrakeCheckIntentsPackage: AppIntentsPackage {}
