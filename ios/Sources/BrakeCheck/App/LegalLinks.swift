import Foundation

/// Links App Review expects inside the app (Settings → About and the Plus paywall).
enum LegalLinks {
    /// Apple's standard licence agreement, which covers subscriptions until we publish our own terms.
    static let termsOfUse = URL(string: "https://www.apple.com/legal/internet-services/itunes/dev/stdeula/")!
    /// Set when the website is live. App Review needs it before submission.
    static let privacyPolicy: URL? = nil
    /// Set when the website is live.
    static let support: URL? = nil
}
