// Shortcuts actions for crash texts. They live in the Xcode app target, not the Swift
// package, because Xcode only extracts App Intents metadata for app targets; xtool
// builds don't include them, so the app falls back to the one-tap text there.
//
// The shared "BrakeCheck SOS" Shortcut is:
//   1. Get Crash Text            (this app)
//   2. Get Emergency Contacts    (this app)
//   3. If Emergency Contacts has any value
//   4.     Send Message: Crash Text to Emergency Contacts   ("Show When Run" off)
//   5. End If
// A setup check stages a message with no recipients, so step 3 skips sending.
import AppIntents
import BrakeCheck

struct GetCrashTextIntent: AppIntent {
    static let title: LocalizedStringResource = "Get Crash Text"
    static let description = IntentDescription(
        "The text BrakeCheck wants to send after a possible crash, with your location.")
    static let openAppWhenRun = false

    func perform() async throws -> some IntentResult & ReturnsValue<String> {
        .result(value: CrashTextStore.shared.takeMessage())
    }
}

struct GetEmergencyContactsIntent: AppIntent {
    static let title: LocalizedStringResource = "Get Emergency Contacts"
    static let description = IntentDescription(
        "The phone numbers to text after a possible crash. Empty during a setup check.")
    static let openAppWhenRun = false

    func perform() async throws -> some IntentResult & ReturnsValue<[String]> {
        .result(value: CrashTextStore.shared.takeRecipients())
    }
}
