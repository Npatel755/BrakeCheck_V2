import AVFoundation
import BrakeCheckCore
import Foundation

@MainActor enum SpeechVoicePreferences {
    static var usesBackendAlerts: Bool {
        AppScope.connectedServices && (UserDefaults.standard.object(forKey: "voice.backendAlerts") as? Bool ?? true)
    }
    static func restoreOriginalDefaultsIfNeeded() {
        let defaults = UserDefaults.standard
        guard defaults.integer(forKey: "voice.originalDefaultsVersion") < 30 else { return }
        restoreOriginalDefaults()
        defaults.set(30, forKey: "voice.originalDefaultsVersion")
    }
    static func restoreOriginalDefaults() {
        let defaults = UserDefaults.standard
        defaults.set("", forKey: "voice.selectedVoice")
        defaults.set("original", forKey: "voice.delivery")
        defaults.set(true, forKey: "voice.backendAlerts")
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
        let voices = availableVoices(language: language)
        let originalLanguage = language == "es" ? "es-ES" : language == "en" ? "en-US" : language
        utterance.voice = voices.first { !chosen.isEmpty && $0.identifier == chosen }
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
