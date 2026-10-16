import AVFoundation
import BrakeCheckCore
import Foundation

@MainActor enum SpeechVoicePreferences {
    static var usesBackendAlerts: Bool {
        UserDefaults.standard.object(forKey: "voice.backendAlerts") as? Bool ?? false
    }
    static func restoreOriginalDefaultsIfNeeded() {
        let defaults = UserDefaults.standard
        guard defaults.integer(forKey: "voice.originalDefaultsVersion") < 30 else { return }
        // Keep an explicit preference made before this migration.
        let savedBackendAlerts = defaults.object(forKey: "voice.backendAlerts") as? Bool
        restoreOriginalDefaults()
        if let savedBackendAlerts {
            defaults.set(savedBackendAlerts, forKey: "voice.backendAlerts")
        }
        defaults.set(30, forKey: "voice.originalDefaultsVersion")
    }
    static func restoreOriginalDefaults() {
        let defaults = UserDefaults.standard
        defaults.set("", forKey: "voice.selectedVoice")
        defaults.set("original", forKey: "voice.delivery")
        defaults.set(false, forKey: "voice.backendAlerts")
    }
    static func availableVoices(language: String) -> [AVSpeechSynthesisVoice] {
        let prefix = language.split(separator: "-").first.map(String.init) ?? "en"
        return AVSpeechSynthesisVoice.speechVoices().filter { $0.language.hasPrefix(prefix + "-") }
            .sorted {
                NaturalVoiceSelection.prefers(
                    SpeechVoiceCandidate(name: $0.name, quality: Int($0.quality.rawValue), isFemale: $0.gender == .female),
                    over: SpeechVoiceCandidate(name: $1.name, quality: Int($1.quality.rawValue), isFemale: $1.gender == .female))
            }
    }
    static func configure(_ utterance: AVSpeechUtterance, language: String, urgent: Bool = false) {
        let chosen = UserDefaults.standard.string(forKey: "voice.selectedVoice") ?? ""
        let originalLanguage = language == "es" ? "es-ES" : language == "en" ? "en-US" : language
        let chosenVoice = chosen.isEmpty ? nil : AVSpeechSynthesisVoice(identifier: chosen)
        let prefix = language.split(separator: "-").first.map(String.init) ?? "en"
        utterance.voice = chosenVoice.flatMap { $0.language.hasPrefix(prefix + "-") ? $0 : nil }
            ?? AVSpeechSynthesisVoice(language: originalLanguage)
        let delivery = UserDefaults.standard.string(forKey: "voice.delivery") ?? "original"
        utterance.rate = urgent || delivery == "original" ? AVSpeechUtteranceDefaultSpeechRate
            : delivery == "gentle" ? 0.48 : delivery == "clear" ? 0.53 : 0.5
        utterance.pitchMultiplier = 1
        utterance.volume = urgent || delivery == "original" ? 1 : delivery == "gentle" ? 0.9 : 0.95
    }
    static func label(_ voice: AVSpeechSynthesisVoice) -> String {
        let quality = voice.quality == .premium ? "Premium" : voice.quality == .enhanced ? "Enhanced" : "Standard"
        return "\(voice.name) · \(voice.language) · \(quality)"
    }
}
