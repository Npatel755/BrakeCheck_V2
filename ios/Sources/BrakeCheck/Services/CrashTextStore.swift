import Foundation

/// A person to text after a crash. Stored only on this iPhone.
public struct CrashTextContact: Codable, Identifiable, Hashable {
    public var id = UUID()
    public var name: String
    public var phone: String
}

/// Phone-only crash texts, the v1 replacement for the Photon check-in. Everything lives in
/// UserDefaults so the App Intents in the app target (ios/AppTarget/Entry/CrashTextIntents.swift)
/// can read it when the "BrakeCheck SOS" Shortcut runs, even with the app in the background.
///
/// Flow: the check-in stages a message here, then opens the user's Shortcut. The Shortcut asks
/// BrakeCheck for the message and recipients (which marks the message as picked up) and sends
/// it. If it was never picked up, the app falls back to the one-tap Messages composer.
public final class CrashTextStore: @unchecked Sendable {
    public static let shared = CrashTextStore()
    /// The name the shared Shortcut must keep; the app runs it by name.
    public static let shortcutName = "BrakeCheck SOS"
    /// iCloud link to the shared Shortcut. Empty until one is built on an iPhone and shared;
    /// until then the setup page explains automatic sending isn't available yet.
    public static let shortcutLink = ""
    /// A staged message older than this is never sent, so a stale crash can't go out later.
    static let stagedLifetime: TimeInterval = 10 * 60

    struct Staged: Codable {
        var id: UUID
        var message: String
        var recipients: [String]
        var createdAt: Date
        /// A setup check: the Shortcut runs end to end but gets no recipients, so nothing is sent.
        var test: Bool
        var pickedUpAt: Date?
    }

    private let defaults = UserDefaults.standard
    private let lock = NSLock()
    private enum Key {
        static let contacts = "crashText.contacts"
        static let senderName = "crashText.senderName"
        static let staged = "crashText.staged"
        static let shortcutVerifiedAt = "crashText.shortcutVerifiedAt"
        static let lastLatitude = "crashText.lastLatitude"
        static let lastLongitude = "crashText.lastLongitude"
        static let lastLocationAt = "crashText.lastLocationAt"
    }

    public var contacts: [CrashTextContact] {
        get { decode([CrashTextContact].self, Key.contacts) ?? [] }
        set {
            encode(newValue, Key.contacts)
            // Mirrored for @AppStorage on Home (the Crash texts row and the setup prompt).
            defaults.set(!newValue.isEmpty, forKey: "crashText.hasContacts")
        }
    }

    /// Optional, so the text can say who it is about.
    public var senderName: String {
        get { defaults.string(forKey: Key.senderName) ?? "" }
        set { defaults.set(newValue.trimmingCharacters(in: .whitespacesAndNewlines), forKey: Key.senderName) }
    }

    /// Set once a setup check proved the Shortcut is installed and talks to BrakeCheck.
    public var shortcutVerifiedAt: Date? {
        get { defaults.object(forKey: Key.shortcutVerifiedAt) as? Date }
        set { defaults.set(newValue, forKey: Key.shortcutVerifiedAt) }
    }

    // MARK: - Location for a manual "Hey Siri, BrakeCheck SOS"

    private var lastNoted = Date.distantPast

    /// Called on GPS fixes during trips; written at most every 20 seconds.
    func noteLocation(latitude: Double, longitude: Double, at date: Date) {
        lock.lock(); defer { lock.unlock() }
        guard date.timeIntervalSince(lastNoted) >= 20 else { return }
        lastNoted = date
        defaults.set(latitude, forKey: Key.lastLatitude)
        defaults.set(longitude, forKey: Key.lastLongitude)
        defaults.set(date, forKey: Key.lastLocationAt)
    }

    private var lastLocation: CrashLocation? {
        guard let at = defaults.object(forKey: Key.lastLocationAt) as? Date else { return nil }
        return CrashLocation(latitude: defaults.double(forKey: Key.lastLatitude),
                             longitude: defaults.double(forKey: Key.lastLongitude), measuredAt: at)
    }

    // MARK: - Message

    func message(location: CrashLocation?, answered: Bool) -> String {
        let who = senderName.isEmpty ? "The person who added you as their emergency contact" : senderName
        var text = answered
            ? "BrakeCheck: \(who) may have been in a car crash and asked for help."
            : "BrakeCheck: \(who) may have been in a car crash and didn't answer a check-in."
        if let location {
            let time = location.measuredAt.formatted(date: .omitted, time: .shortened)
            text += String(format: " Location as of %@: https://maps.apple.com/?ll=%.5f,%.5f", time,
                           location.latitude, location.longitude)
        } else {
            text += " Their location wasn't available."
        }
        return text + " Please call them. If you can't reach them, call 911."
    }

    // MARK: - Staging for the Shortcut

    @discardableResult
    func stage(message: String, test: Bool) -> UUID {
        let staged = Staged(id: UUID(), message: message, recipients: test ? [] : contacts.map(\.phone),
                            createdAt: Date(), test: test, pickedUpAt: nil)
        lock.lock(); defer { lock.unlock() }
        encode(staged, Key.staged)
        return staged.id
    }

    func wasPickedUp(_ id: UUID) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard let staged = decode(Staged.self, Key.staged), staged.id == id else { return false }
        return staged.pickedUpAt != nil
    }

    func clearStaged() {
        lock.lock(); defer { lock.unlock() }
        defaults.removeObject(forKey: Key.staged)
    }

    private func freshStaged() -> Staged? {
        guard let staged = decode(Staged.self, Key.staged),
              Date().timeIntervalSince(staged.createdAt) < Self.stagedLifetime else { return nil }
        return staged
    }

    /// The Shortcut's first action. With nothing staged (someone said "Hey Siri, BrakeCheck SOS"
    /// themselves), it describes a request for help at the last known location.
    public func takeMessage() -> String {
        lock.lock(); defer { lock.unlock() }
        guard var staged = freshStaged() else { return message(location: lastLocation, answered: true) }
        if staged.pickedUpAt == nil { staged.pickedUpAt = Date(); encode(staged, Key.staged) }
        return staged.message
    }

    /// The Shortcut's second action. Empty for a setup check, so its "If" step skips sending.
    public func takeRecipients() -> [String] {
        lock.lock(); defer { lock.unlock() }
        guard var staged = freshStaged() else { return contacts.map(\.phone) }
        if staged.pickedUpAt == nil { staged.pickedUpAt = Date(); encode(staged, Key.staged) }
        return staged.recipients
    }

    // MARK: - Storage

    private func decode<T: Decodable>(_ type: T.Type, _ key: String) -> T? {
        defaults.data(forKey: key).flatMap { try? JSONDecoder().decode(type, from: $0) }
    }

    private func encode<T: Encodable>(_ value: T, _ key: String) {
        if let data = try? JSONEncoder().encode(value) { defaults.set(data, forKey: key) }
    }
}
