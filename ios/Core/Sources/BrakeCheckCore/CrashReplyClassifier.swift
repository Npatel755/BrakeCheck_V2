import Foundation

/// An interpretation of a short answer to "Are you okay?". An unclear answer
/// must leave the check-in open; it is not permission to suppress an alert.
public enum CrashReplyDecision: String, Equatable, Sendable {
    case ok
    case help
    case unrecognized
}

/// A small, offline classifier for the app's English and Spanish check-in.
/// It intentionally favors a request for help over an affirmative answer when
/// both occur in the same transcript. It does not decide when a check-in times out.
public enum CrashReplyClassifier {
    private static let helpWords: Set<String> = [
        "help", "hurt", "injured", "injury", "ambulance", "call", "911",
        "bleeding", "pain", "no", "ayuda", "ayudame", "herido",
        "herida", "lesionado", "lesionada", "ambulancia", "socorro"
    ]
    private static let explicitOkayPhrases: [[String]] = [
        ["i", "m", "okay"], ["im", "okay"], ["i", "am", "okay"],
        ["i", "m", "ok"], ["im", "ok"], ["i", "am", "ok"],
        ["i", "m", "fine"], ["im", "fine"], ["i", "am", "fine"],
        ["i", "feel", "fine"], ["i", "m", "alright"], ["i", "am", "alright"],
        ["i", "m", "all", "right"], ["i", "am", "all", "right"],
        ["estoy", "bien"], ["me", "encuentro", "bien"]
    ]
    private static let negations: Set<String> = ["not", "never", "cannot", "cant", "dont", "wasnt", "isnt"]
    private static let positiveWords: Set<String> = ["okay", "ok", "fine", "good", "alright", "bien"]
    private static let preambleWords: Set<String> = ["yes", "yeah", "yep", "si", "uh", "um"]
    private static let closingWords: Set<String> = ["now", "thanks", "thank", "you"]

    public static func classify(_ transcript: String) -> CrashReplyDecision {
        let folded = transcript.replacingOccurrences(of: "’", with: "'")
            .folding(options: [.caseInsensitive, .diacriticInsensitive],
                     locale: Locale(identifier: "en_US_POSIX"))
            .replacingOccurrences(of: "don't", with: "do not")
            .replacingOccurrences(of: "can't", with: "cannot")
            .replacingOccurrences(of: "isn't", with: "is not")
            .replacingOccurrences(of: "wasn't", with: "was not")
        let words = folded.split(whereSeparator: { !$0.isLetter && !$0.isNumber }).map(String.init)
        guard !words.isEmpty else { return .unrecognized }

        if words.contains(where: { helpWords.contains($0) }) { return .help }
        if words.contains(where: { negations.contains($0) }) &&
            words.contains(where: { positiveWords.contains($0) }) { return .help }
        let saysOkay = explicitOkayPhrases.contains { phrase in
            words.count >= phrase.count && (0...(words.count - phrase.count)).contains { offset in
                words[offset..<(offset + phrase.count)].elementsEqual(phrase) &&
                    words[..<offset].allSatisfy { preambleWords.contains($0) } &&
                    words[(offset + phrase.count)...].allSatisfy { closingWords.contains($0) }
            }
        }
        guard saysOkay else { return .unrecognized }
        // Any negation in a mixed answer means it is unsafe to cancel the alert.
        return words.contains(where: { negations.contains($0) }) ? .help : .ok
    }
}
