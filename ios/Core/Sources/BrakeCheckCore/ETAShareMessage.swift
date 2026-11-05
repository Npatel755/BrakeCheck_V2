import Foundation

/// The text behind "Share ETA" while navigating, e.g.
/// "On my way to Piedmont Park — arriving around 5:42 PM (18 min). Sent from BrakeSense."
public enum ETAShareMessage {
    public static func text(destination: String?, remainingSeconds: Double, now: Date = Date(),
                            locale: Locale = .current, timeZone: TimeZone = .current) -> String? {
        guard remainingSeconds.isFinite, remainingSeconds >= 0 else { return nil }
        let arrival = now.addingTimeInterval(remainingSeconds)
        let formatter = DateFormatter()
        formatter.locale = locale; formatter.timeZone = timeZone
        formatter.timeStyle = .short; formatter.dateStyle = .none
        let minutes = Int((remainingSeconds / 60).rounded())
        let duration = minutes >= 60 ? "\(minutes / 60) h \(minutes % 60) min" : "\(max(minutes, 1)) min"
        let place = destination.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.flatMap { $0.isEmpty ? nil : $0 }
        let heading = place.map { "On my way to \($0)" } ?? "On my way"
        // Foundation pads "5:42 PM" with a narrow no-break space on recent systems; plain spaces travel better in messages.
        let time = formatter.string(from: arrival)
            .replacingOccurrences(of: "\u{202F}", with: " ").replacingOccurrences(of: "\u{00A0}", with: " ")
        return "\(heading) — arriving around \(time) (\(duration)). Sent from BrakeSense."
    }
}
