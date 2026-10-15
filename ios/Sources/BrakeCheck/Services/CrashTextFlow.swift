import AVFoundation
import Foundation
import UIKit

/// The phone-only crash check-in: "Are you okay?" with a countdown, then a text to the
/// emergency contacts. It sends through the "BrakeCheck SOS" Shortcut when that's set up and
/// verified, and otherwise (or if the Shortcut never picks the message up) opens the one-tap
/// Messages composer. iOS never lets an app send a text by itself.
@MainActor
final class CrashTextFlow: ObservableObject {
    enum Phase: Equatable {
        case idle
        case asking
        /// The Shortcut was opened; waiting for it to call back or for the app to return.
        case waitingForShortcut
        /// The one-tap composer is (or should be) on screen.
        case composing
        case finished(title: String, detail: String)
    }

    static let countdownSeconds = 30

    @Published var presented = false
    @Published private(set) var phase: Phase = .idle
    @Published private(set) var remaining = countdownSeconds
    @Published private(set) var practice = false
    @Published var composerShown = false
    @Published private(set) var draft = ""
    @Published private(set) var recipients: [String] = []
    /// Result of the last setup check, shown on the setup page.
    @Published private(set) var checkStatus: String?
    @Published private(set) var checking = false

    private let store = CrashTextStore.shared
    private let speech = AVSpeechSynthesizer()
    private var countdown: Task<Void, Never>?
    private var returnCheck: Task<Void, Never>?
    private var location: CrashLocation?
    private var stagedID: UUID?
    private var checkID: UUID?
    private var activeObserver: NSObjectProtocol?

    init() {
        activeObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.appBecameActive() }
        }
    }

    var hasContacts: Bool { !store.contacts.isEmpty }
    var shortcutReady: Bool { store.shortcutVerifiedAt != nil }

    // MARK: - Check-in

    /// Starts after a possible impact (or as a practice run that never texts anyone).
    func begin(location: CrashLocation?, practice: Bool = false) {
        guard !presented else { return }
        guard practice || hasContacts else { return }
        self.location = location
        self.practice = practice
        remaining = Self.countdownSeconds
        phase = .asking
        presented = true
        UINotificationFeedbackGenerator().notificationOccurred(.error)
        speak(practice
            ? "Practice check-in. Are you okay?"
            : "Possible crash detected. Are you okay? If you don't answer in \(Self.countdownSeconds) seconds, BrakeCheck will text your emergency contacts.")
        countdown = Task { [weak self] in
            while let self, self.remaining > 0, self.phase == .asking {
                do { try await Task.sleep(nanoseconds: 1_000_000_000) } catch { return }
                guard self.phase == .asking else { return }
                self.remaining -= 1
            }
            guard let self, self.phase == .asking else { return }
            self.send(answered: false)
        }
    }

    func imOkay() {
        guard phase == .asking else { return }
        stop()
        phase = .finished(title: "Glad you're okay", detail: "Nobody was texted. Any video around the impact is saved in Incidents.")
    }

    func sendHelp() {
        guard phase == .asking else { return }
        send(answered: true)
    }

    func close() {
        stop()
        composerShown = false
        presented = false
        phase = .idle
    }

    private func send(answered: Bool) {
        stop()
        draft = store.message(location: location, answered: answered)
        recipients = store.contacts.map(\.phone)
        if practice {
            phase = .finished(title: "Practice finished",
                detail: "In a real crash, your contacts would get this text:\n\n\(draft)")
            return
        }
        guard shortcutReady else { openComposer(); return }
        let id = store.stage(message: draft, test: false)
        stagedID = id
        phase = .waitingForShortcut
        runShortcut { [weak self] opened in
            if !opened { self?.openComposer() }
        }
    }

    private func openComposer() {
        phase = .composing
        composerShown = true
    }

    /// From the composer's delegate.
    func composerFinished(sent: Bool) {
        composerShown = false
        phase = sent
            ? .finished(title: "Text sent", detail: "Messages sent your text. Call 911 if you need emergency help.")
            : .finished(title: "Text not sent", detail: "You closed Messages without sending. Tap below to try again.")
    }

    func retryComposer() { openComposer() }

    // MARK: - Shortcut

    /// Opens the user's Shortcut by name. x-success and x-error bring Shortcuts back to the
    /// app through the brakecheck:// URL scheme.
    private func runShortcut(completion: @escaping (Bool) -> Void) {
        var components = URLComponents(string: "shortcuts://x-callback-url/run-shortcut")!
        components.queryItems = [
            URLQueryItem(name: "name", value: CrashTextStore.shortcutName),
            URLQueryItem(name: "x-success", value: "brakecheck://sos/done"),
            URLQueryItem(name: "x-cancel", value: "brakecheck://sos/cancel"),
            URLQueryItem(name: "x-error", value: "brakecheck://sos/error"),
        ]
        guard let url = components.url else { completion(false); return }
        UIApplication.shared.open(url) { opened in completion(opened) }
    }

    /// brakecheck://sos/... from Shortcuts.
    func handle(_ url: URL) -> Bool {
        guard url.scheme == "brakecheck", url.host == "sos" else { return false }
        resolveShortcutReturn()
        return true
    }

    /// Whichever comes first, the callback URL or the app becoming active, decides the result.
    /// A short wait lets the callback land before the "app is active again" path judges.
    private func appBecameActive() {
        guard phase == .waitingForShortcut || checking else { return }
        returnCheck?.cancel()
        returnCheck = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            self?.resolveShortcutReturn()
        }
    }

    private func resolveShortcutReturn() {
        returnCheck?.cancel()
        if checking, let id = checkID {
            checking = false
            checkID = nil
            if store.wasPickedUp(id) {
                store.shortcutVerifiedAt = Date()
                checkStatus = "Working. Crash texts will send automatically through the Shortcut."
            } else {
                store.shortcutVerifiedAt = nil
                checkStatus = "BrakeCheck didn't hear from the Shortcut. Add it again (keep the name \"\(CrashTextStore.shortcutName)\"), then check again. Until then, crash texts use the one-tap fallback."
            }
            store.clearStaged()
            return
        }
        guard phase == .waitingForShortcut, let id = stagedID else { return }
        stagedID = nil
        if store.wasPickedUp(id) {
            phase = .finished(title: "Text sent by your Shortcut",
                detail: "Your Shortcut sent the crash text. Check Messages to confirm it went out, and call 911 if you need emergency help.")
        } else {
            // Never picked up: the Shortcut is missing, renamed or failed. Fall back to one tap.
            store.clearStaged()
            openComposer()
        }
    }

    // MARK: - Setup check

    /// Runs the Shortcut end to end with no recipients, so nothing is sent. It proves the
    /// Shortcut is installed, named correctly and can reach BrakeCheck's actions.
    func checkShortcut() {
        guard !checking else { return }
        checking = true
        checkStatus = "Opening Shortcuts…"
        checkID = store.stage(message: "BrakeCheck setup check. Nothing is sent.", test: true)
        runShortcut { [weak self] opened in
            guard let self, !opened else { return }
            self.checking = false
            self.checkID = nil
            self.checkStatus = "Couldn't open the Shortcuts app. Install Shortcuts from the App Store, then try again."
        }
    }

    // MARK: - Helpers

    private func stop() {
        countdown?.cancel()
        countdown = nil
        speech.stopSpeaking(at: .immediate)
    }

    private func speak(_ text: String) {
        let utterance = AVSpeechUtterance(string: text)
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate
        speech.speak(utterance)
    }
}
