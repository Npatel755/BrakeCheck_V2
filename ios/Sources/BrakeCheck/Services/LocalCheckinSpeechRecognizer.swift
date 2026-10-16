import AVFoundation
import Foundation
import Speech

enum LocalCheckinSpeechError: LocalizedError {
    case microphoneDenied
    case speechDenied
    case onDeviceUnavailable
    case alreadyRecognizing
    case audioBusy
    case recordingTooLarge
    case timedOut

    var errorDescription: String? {
        switch self {
        case .microphoneDenied: "Microphone permission is off. Use the check-in buttons."
        case .speechDenied: "Speech recognition permission is off. Use the check-in buttons."
        case .onDeviceUnavailable: "On-device speech recognition is unavailable for this language. Use the check-in buttons."
        case .alreadyRecognizing: "A spoken reply is already being checked."
        case .audioBusy: "Wait for the check-in prompt to finish before speaking."
        case .recordingTooLarge: "The spoken reply is too large to process. Use the check-in buttons."
        case .timedOut: "On-device speech recognition timed out. Use the check-in buttons."
        }
    }
}

/// Recognizes a short recorded reply without ever permitting cloud transcription.
@MainActor
final class LocalCheckinSpeechRecognizer {
    private var recognitionID: UUID?
    private var recognitionTask: SFSpeechRecognitionTask?
    private var timeoutTask: Task<Void, Never>?
    private var completion: CheckedContinuation<String?, Error>?

    /// Prompt playback must have finished before this method starts the microphone.
    func recognizeReply(using audio: VoiceAudio, language: String) async throws -> String? {
        guard Self.supportsOnDeviceRecognition(language: language) else {
            throw LocalCheckinSpeechError.onDeviceUnavailable
        }
        guard await requestSpeechAuthorization() else { throw LocalCheckinSpeechError.speechDenied }
        guard await audio.requestMicrophone() else { throw LocalCheckinSpeechError.microphoneDenied }
        guard !audio.isBusy else { throw LocalCheckinSpeechError.audioBusy }
        let recording = try await audio.recordReply() // Six seconds; temporary source file is deleted by VoiceAudio.
        return try await transcribe(recording, language: language)
    }

    static func supportsOnDeviceRecognition(language: String) -> Bool {
        guard let recognizer = SFSpeechRecognizer(locale: locale(for: language)) else { return false }
        return recognizer.supportsOnDeviceRecognition && recognizer.isAvailable
    }

    /// Real impact checks can avoid permission dialogs that would consume the response deadline.
    func canRecognizeWithoutPrompt(language: String) -> Bool {
        Self.supportsOnDeviceRecognition(language: language)
            && SFSpeechRecognizer.authorizationStatus() == .authorized
            && AVAudioSession.sharedInstance().recordPermission == .granted
    }

    /// Settings can request this before a trip so the check-in is not interrupted by a prompt.
    func requestSpeechPermission() async -> Bool {
        await requestSpeechAuthorization()
    }

    /// The provided recording stays on this iPhone. Empty final text becomes nil.
    func transcribe(_ recording: Data, language: String) async throws -> String? {
        guard !recording.isEmpty else { return nil }
        guard recording.count <= 5 * 1024 * 1024 else { throw LocalCheckinSpeechError.recordingTooLarge }
        guard recognitionID == nil else { throw LocalCheckinSpeechError.alreadyRecognizing }
        guard await requestSpeechAuthorization() else { throw LocalCheckinSpeechError.speechDenied }
        guard let recognizer = SFSpeechRecognizer(locale: Self.locale(for: language)),
              recognizer.supportsOnDeviceRecognition, recognizer.isAvailable else {
            throw LocalCheckinSpeechError.onDeviceUnavailable
        }

        let url = FileManager.default.temporaryDirectory.appendingPathComponent("local-checkin-\(UUID().uuidString).m4a")
        try recording.write(to: url, options: .atomic)
        defer { try? FileManager.default.removeItem(at: url) }
        let request = SFSpeechURLRecognitionRequest(url: url)
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = false
        request.contextualStrings = language == "es" ? ["estoy bien", "ayuda"] : ["I'm okay", "help"]
        let id = UUID()

        return try await withTaskCancellationHandler {
            try Task.checkCancellation()
            return try await withCheckedThrowingContinuation { continuation in
                recognitionID = id
                completion = continuation
                recognitionTask = recognizer.recognitionTask(with: request) { [weak self] result, error in
                    Task { @MainActor [weak self] in
                        guard let self, self.recognitionID == id else { return }
                        if let error {
                            self.finish(id: id, result: .failure(error))
                        } else if let result, result.isFinal {
                            let text = result.bestTranscription.formattedString
                                .trimmingCharacters(in: .whitespacesAndNewlines)
                            self.finish(id: id, result: .success(text.isEmpty ? nil : text))
                        }
                    }
                }
                timeoutTask = Task { [weak self] in
                    try? await Task.sleep(nanoseconds: 8_000_000_000)
                    guard !Task.isCancelled else { return }
                    self?.finish(id: id, result: .failure(LocalCheckinSpeechError.timedOut))
                }
            }
        } onCancel: {
            Task { @MainActor [weak self] in
                self?.finish(id: id, result: .failure(CancellationError()))
            }
        }
    }

    func cancel() {
        guard let id = recognitionID else { return }
        finish(id: id, result: .failure(CancellationError()))
    }

    private func finish(id: UUID, result: Result<String?, Error>) {
        guard recognitionID == id else { return }
        recognitionID = nil
        timeoutTask?.cancel()
        timeoutTask = nil
        recognitionTask?.cancel()
        recognitionTask = nil
        let waiting = completion
        completion = nil
        waiting?.resume(with: result)
    }

    private func requestSpeechAuthorization() async -> Bool {
        if SFSpeechRecognizer.authorizationStatus() == .authorized { return true }
        if SFSpeechRecognizer.authorizationStatus() != .notDetermined { return false }
        let status = await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { continuation.resume(returning: $0) }
        }
        return status == .authorized
    }

    private static func locale(for language: String) -> Locale {
        Locale(identifier: language == "es" ? "es-ES" : "en-US")
    }
}
