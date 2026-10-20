import Foundation

/// Links App Review expects beside any subscription offer and inside the app (Settings > About).
enum LegalLinks {
    /// Apple's standard licence agreement, which covers apps that don't publish their own EULA.
    static let termsOfUse = URL(string: "https://www.apple.com/legal/internet-services/itunes/dev/stdeula/")!
    /// Still to come: the BrakeSense website's privacy page. App Review needs it before submission.
    static let privacyPolicy: URL? = nil
}
