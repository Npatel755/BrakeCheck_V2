import AVFoundation
import Foundation

/// Playback completion precedes recording so the prompt cannot transcribe itself.
@MainActor
final class VoiceAudio: NSObject, AVAudioPlayerDelegate, AVSpeechSynthesizerDelegate {
    private var player: AVAudioPlayer?
    private var playbackID: UUID?
    private var recorder: AVAudioRecorder?
    private var completion: CheckedContinuation<Void, Error>?
    private let speech = AVSpeechSynthesizer()

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
        stop()
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .spokenAudio, options: [.duckOthers])
        try? session.setActive(true)
        let utterance = AVSpeechUtterance(string: language == "es" ? "¡Frena ya!" : "Brake now!")
        utterance.voice = AVSpeechSynthesisVoice(language: language == "es" ? "es-ES" : "en-US")
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
        speech.stopSpeaking(at: .immediate)
        finish(CancellationError())
        try? AVAudioSession.sharedInstance().setActive(false, options: [.notifyOthersOnDeactivation])
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
        Task { @MainActor in
            if self.player == nil && self.recorder?.isRecording != true {
                try? AVAudioSession.sharedInstance().setActive(false, options: [.notifyOthersOnDeactivation])
            }
        }
    }

}
