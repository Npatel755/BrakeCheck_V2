import AVFoundation
import Foundation

enum LocalCheckinAcknowledgement {
    case okay, help, noResponse, demo, failed
}

/// Playback completion precedes recording so the prompt cannot transcribe itself.
@MainActor
final class VoiceAudio: NSObject, AVAudioPlayerDelegate, AVSpeechSynthesizerDelegate {
    private var player: AVAudioPlayer?
    private var playbackID: UUID?
    private var recorder: AVAudioRecorder?
    private var completion: CheckedContinuation<Void, Error>?
    private let speech = AVSpeechSynthesizer()
    private var activeUtterance: AVSpeechUtterance?
    private var speechCompletion: CheckedContinuation<Void, Error>?

    /// Ownership stays busy until the matching delegate callback releases it.
    var isBusy: Bool { player != nil || recorder?.isRecording == true || activeUtterance != nil }

    override init() {
        super.init()
        speech.delegate = self
    }

    func requestMicrophone() async -> Bool {
        await withCheckedContinuation { continuation in
            AVAudioSession.sharedInstance().requestRecordPermission { granted in
                continuation.resume(returning: granted)
            }
        }
    }

    func play(_ data: Data) async throws {
        stop()
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playback, mode: .spokenAudio, options: [.duckOthers])
        try session.setActive(true)
        let audio = try AVAudioPlayer(data: data)
        audio.delegate = self
        player = audio
        let id = UUID()
        playbackID = id
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                completion = continuation
                if !audio.play() { finish(APIError.message("Audio playback failed.")) }
            }
        } onCancel: {
            Task { @MainActor [weak self] in if self?.playbackID == id { self?.stop() } }
        }
    }

    func speakBrake(language: String) {
        speak(language == "es" ? "¡Frena ya!" : "Brake now!", language: language, urgent: true)
    }

    func speakPreview(language: String) {
        speak(language == "es" ? "Conduce con calma. Te guiaré durante tu viaje." : "Drive comfortably. I'll guide you along the way.",
              language: language, urgent: false)
    }

    /// Returns only after the device voice stops, so recording cannot capture the prompt.
    func speakCheckinPrompt(language: String) async throws {
        let prompt = language == "es"
            ? "Detecté un posible impacto. ¿Estás bien? Di estoy bien, o di ayuda."
            : "I detected a possible impact. Are you okay? Say I'm okay, or say help."
        try await speakAndWait(prompt, language: language)
    }

    /// These phrases describe the local result and do not claim a message was sent.
    func speakCheckinAcknowledgement(status: LocalCheckinAcknowledgement, language: String) async throws {
        let spanish = language == "es"
        let message: String
        switch status {
        case .okay:
            message = spanish ? "Me alegra que estés bien." : "Glad you're okay."
        case .help:
            message = spanish
                ? "Tu solicitud de ayuda fue registrada. Consulta la pantalla para ver el estado de la alerta."
                : "Your help request was recorded. Check the screen for alert status."
        case .noResponse:
            message = spanish
                ? "No escuché una respuesta. Consulta la pantalla para ver el estado de la alerta."
                : "I did not hear a reply. Check the screen for alert status."
        case .demo:
            message = spanish ? "Demostración completa. No se enviaron mensajes de emergencia."
                : "Demo complete. No emergency messages were sent."
        case .failed:
            message = spanish
                ? "No pude confirmar el envío de una alerta. Si necesitas ayuda urgente, llama a emergencias."
                : "I could not confirm an alert was sent. If you need urgent help, call emergency services."
        }
        try await speakAndWait(message, language: language)
    }

    private func speakAndWait(_ text: String, language: String) async throws {
        stop()
        try Task.checkCancellation()
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playback, mode: .spokenAudio, options: [.duckOthers])
        try session.setActive(true)
        let utterance = AVSpeechUtterance(string: text)
        SpeechVoicePreferences.configure(utterance, language: language)
        activeUtterance = utterance
        do {
            try await withTaskCancellationHandler {
                try Task.checkCancellation()
                try await withCheckedThrowingContinuation { continuation in
                    speechCompletion = continuation
                    speech.speak(utterance)
                }
            } onCancel: {
                Task { @MainActor [weak self] in
                    if self?.activeUtterance === utterance { self?.stop() }
                }
            }
        } catch {
            if activeUtterance === utterance { stop() }
            throw error
        }
    }

    private func speak(_ text: String, language: String, urgent: Bool) {
        stop()
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .spokenAudio, options: [.duckOthers])
        try? session.setActive(true)
        let utterance = AVSpeechUtterance(string: text)
        SpeechVoicePreferences.configure(utterance, language: language, urgent: urgent)
        activeUtterance = utterance
        speech.speak(utterance)
    }

    func recordReply() async throws -> Data {
        guard AVAudioSession.sharedInstance().recordPermission == .granted else {
            throw APIError.message("Microphone unavailable. Use the buttons below.")
        }
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker])
        try session.setActive(true)
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("reply-\(UUID().uuidString).m4a")
        defer {
            recorder?.stop(); recorder = nil
            try? FileManager.default.removeItem(at: url)
            try? session.setActive(false, options: [.notifyOthersOnDeactivation])
        }
        let recording = try AVAudioRecorder(url: url, settings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 16000,
            AVNumberOfChannelsKey: 1, AVEncoderAudioQualityKey: AVAudioQuality.medium.rawValue
        ])
        recorder = recording
        guard recording.record() else { throw APIError.message("Microphone could not start. Use the buttons below.") }
        try await Task.sleep(nanoseconds: 6_000_000_000)
        try Task.checkCancellation()
        recording.stop()
        return try Data(contentsOf: url)
    }

    func stop() {
        player?.stop(); recorder?.stop()
        activeUtterance = nil
        let pendingSpeech = speechCompletion
        speechCompletion = nil
        speech.stopSpeaking(at: .immediate)
        pendingSpeech?.resume(throwing: CancellationError())
        finish(CancellationError())
        try? AVAudioSession.sharedInstance().setActive(false, options: [.notifyOthersOnDeactivation])
    }

    private func finishSpeech(_ utterance: AVSpeechUtterance, error: Error? = nil) {
        guard activeUtterance === utterance else { return }
        activeUtterance = nil
        let pending = speechCompletion
        speechCompletion = nil
        if let error { pending?.resume(throwing: error) } else { pending?.resume() }
        if player == nil && recorder?.isRecording != true {
            try? AVAudioSession.sharedInstance().setActive(false, options: [.notifyOthersOnDeactivation])
        }
    }

    private func finish(_ error: Error? = nil) {
        let saved = completion
        completion = nil
        player = nil
        playbackID = nil
        if let error { saved?.resume(throwing: error) } else { saved?.resume() }
        try? AVAudioSession.sharedInstance().setActive(false, options: [.notifyOthersOnDeactivation])
    }

    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in if self.player === player { self.finish(flag ? nil : APIError.message("Playback interrupted.")) } }
    }
    nonisolated func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        Task { @MainActor in if self.player === player { self.finish(error ?? APIError.message("Audio could not be decoded.")) } }
    }
    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor in self.finishSpeech(utterance) }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        Task { @MainActor in self.finishSpeech(utterance, error: CancellationError()) }
    }

}
