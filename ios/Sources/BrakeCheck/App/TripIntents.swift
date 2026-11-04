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

public struct StartTripIntent: AppIntent {
    public static let title: LocalizedStringResource = "Start a trip"
    public static let description = IntentDescription("Starts the BrakeSense camera trip with collision warnings.")
    public static let openAppWhenRun = true

    public init() {}

    @MainActor
    public func perform() async throws -> some IntentResult & ProvidesDialog {
        guard TripIntentBridge.shared.noticeAccepted else {
            return .result(dialog: "Open BrakeSense and accept the driver-aid notice first.")
        }
        TripIntentBridge.shared.submit(.start)
        return .result(dialog: "Starting your trip.")
    }
}

public struct EndTripIntent: AppIntent {
    public static let title: LocalizedStringResource = "End the trip"
    public static let description = IntentDescription("Ends the current BrakeSense trip and saves it.")
    public static let openAppWhenRun = true

    public init() {}

    @MainActor
    public func perform() async throws -> some IntentResult & ProvidesDialog {
        TripIntentBridge.shared.submit(.end)
        return .result(dialog: "Ending your trip.")
    }
}

/// Lets an app target that links this package pick up the intents' metadata. The Siri
/// phrases (`AppShortcutsProvider`) must be declared in the app target itself; see
/// ios/AppTarget/Entry/AppEntry.swift.
public struct BrakeCheckIntentsPackage: AppIntentsPackage {}
