import Foundation
import Combine
import UIKit
import BrakeCheckCore

@MainActor
final class SafetyService: ObservableObject {
    let settings = ConnectionSettings()
    let audio = VoiceAudio()
    private let replyRecognizer = LocalCheckinSpeechRecognizer()
    /// Safety audio takes ownership before asynchronous playback can begin.
    var beforePriorityAudio: (() -> Void)?
    @Published var connectionStatus = "Connect your backend"
    @Published private(set) var deliveryServiceStatus = "Automatic alerts need a connected service and confirmed contacts."
    @Published var voiceStatus = "Voice not prepared"
    @Published var contacts: [EmergencyContact] = []
    @Published var error: String?
    @Published var checkinPresented = false
    @Published var resolved = false
    @Published var resultNeedsAttention = false
    @Published var checkinTitle = "Are you okay?"
    @Published var checkinDetail = ""
    @Published var remaining = 20
    @Published var isDryRun = true
    @Published var sending = false
    @Published var preparing = false
    @Published var connectionLost = false
    private var brakeAudio: Data?
    private var preparedSignature = ""
    private var photonConfigured: Bool?
    private var voiceRefreshQueued = false
    private var connectionGeneration = UUID()
    private var flow: Task<Void, Never>?
    private var polling: Task<Void, Never>?
    private var crashID: String?
    private var activeClient: BackendClient?
    private var lastBrake: TimeInterval = -.infinity
    private var sessionGeneration = UUID()
    private var awaitingAlertDelivery = false

    var canRespond: Bool { checkinPresented && !resolved && !sending && (isDryRun || crashID != nil) }

    func connectIfPaired() async {
        guard settings.isPaired else { return }
        await connect()
    }

    func connect() async {
        let generation = UUID()
        connectionGeneration = generation
        error = nil
        connectionStatus = "Checking connection…"
        deliveryServiceStatus = "Checking automatic alert setup…"
        do {
            if !settings.isPaired { try settings.saveKey() }
            var client = settings.client
            if client == nil && settings.isPaired {
                let discovered = try await BonjourServiceDiscovery().discoverBackend()
                guard connectionGeneration == generation else { return }
                settings.address = discovered.absoluteString
                client = settings.client
            }
            guard let currentClient = client else {
                throw APIError.message("Pair with the computer or enter a local backend URL and app key.")
            }
            let result: BackendHealth
            do {
                result = try await currentClient.request("health", timeout: 6)
            } catch {
                guard settings.isPaired, currentClient.baseURL.scheme?.lowercased() == "http" else { throw error }
                let discovered = try await BonjourServiceDiscovery().discoverBackend()
                guard connectionGeneration == generation else { return }
                settings.address = discovered.absoluteString
                guard let refreshedClient = settings.client else { throw error }
                result = try await refreshedClient.request("health", timeout: 8)
            }
            guard connectionGeneration == generation else { return }
            guard result.ok else { throw APIError.message("The backend health check did not report ready.") }
            photonConfigured = result.photon_configured
            connectionStatus = "Backend connected"
            updateDeliveryServiceStatus(confirmedContacts: result.confirmed_contacts)
            voiceStatus = !settings.voiceEnabled ? "Spoken alerts off" : SpeechVoicePreferences.usesBackendAlerts
                ? (result.voice_configured ? "ElevenLabs brake voice configured" : "Device voice ready · ElevenLabs unavailable")
                : "Device voice ready"
            await refreshContacts()
            guard connectionGeneration == generation else { return }
            if settings.voiceEnabled && SpeechVoicePreferences.usesBackendAlerts {
                Task { await self.prepareVoice() }
            }
        } catch {
            guard connectionGeneration == generation else { return }
            photonConfigured = nil
            connectionStatus = "Connection unavailable"
            deliveryServiceStatus = "Automatic alert setup cannot be verified until the service connects."
            brakeAudio = nil; preparedSignature = ""
            voiceStatus = settings.voiceEnabled ? "Device voice fallback · backend unavailable" : "Spoken alerts off"
            self.error = error.localizedDescription
        }
    }

    func prepareVoice() async {
        if preparing { voiceRefreshQueued = true; return }
        guard settings.voiceEnabled else {
            brakeAudio = nil; preparedSignature = ""; voiceStatus = "Spoken alerts off"; return
        }
        guard SpeechVoicePreferences.usesBackendAlerts else {
            brakeAudio = nil; preparedSignature = ""; voiceStatus = "Device voice ready"; return
        }
        guard let client = settings.client else {
            brakeAudio = nil; preparedSignature = ""
            voiceStatus = "Device voice fallback · backend unavailable"; return
        }
        let signature = settings.address + settings.language + settings.credentialForSignature
        let language = settings.language
        preparing = true
        defer {
            preparing = false
            if voiceRefreshQueued {
                voiceRefreshQueued = false
                Task { await self.prepareVoice() }
            }
        }
        do {
            let data = try await client.data("voice/alert", method: "POST", json: ["kind": "brake", "lang": language], timeout: 40)
            guard settings.voiceEnabled, SpeechVoicePreferences.usesBackendAlerts,
                  signature == settings.address + settings.language + settings.credentialForSignature else { return }
            brakeAudio = data
            preparedSignature = signature
            voiceStatus = "ElevenLabs voice ready"
        } catch {
            guard settings.voiceEnabled,
                  signature == settings.address + settings.language + settings.credentialForSignature else { return }
            brakeAudio = nil; preparedSignature = ""
            voiceStatus = "Device voice fallback · backend unavailable"
        }
    }

    private func updateDeliveryServiceStatus(confirmedContacts: Int) {
        if photonConfigured == nil {
            deliveryServiceStatus = "Test the service connection to verify automatic alert setup."
        } else if photonConfigured == false {
            deliveryServiceStatus = "Automatic alerts unavailable: configure the Photon delivery service."
        } else if confirmedContacts == 0 {
            deliveryServiceStatus = "Automatic alerts need at least one confirmed contact."
        } else {
            deliveryServiceStatus = "Automatic alerts configured for \(confirmedContacts) confirmed contact(s). Delivery is not guaranteed."
        }
    }

    func requestMicrophone() async {
        let microphoneGranted = await audio.requestMicrophone()
        let speechGranted = await replyRecognizer.requestSpeechPermission()
        if !microphoneGranted || !speechGranted {
            error = "Spoken replies need microphone and speech permissions. Check-ins still offer on-screen buttons."
        } else if !LocalCheckinSpeechRecognizer.supportsOnDeviceRecognition(language: settings.language) {
            voiceStatus = "Device voice ready · spoken replies unavailable for this language"
        } else {
            error = nil
            voiceStatus = "Device voice and spoken replies ready"
        }
    }

    func brakeWarning() {
        let now = ProcessInfo.processInfo.systemUptime
        guard !checkinPresented, now - lastBrake > 8 else { return }
        beforePriorityAudio?()
        lastBrake = now
        // Use the voice cooldown so a persistent critical state does not
        // vibrate on every camera frame or add a second warning timer.
        UINotificationFeedbackGenerator().notificationOccurred(.warning)
        guard settings.voiceEnabled else { return }
        if SpeechVoicePreferences.usesBackendAlerts, let data = brakeAudio,
           preparedSignature == settings.address + settings.language + settings.credentialForSignature {
            Task { try? await audio.play(data) }
        } else {
            audio.speakBrake(language: settings.language)
        }
    }

    func previewVoice() {
        guard !checkinPresented, !audio.isBusy else { return }
        beforePriorityAudio?()
        audio.speakPreview(language: settings.language)
    }

    func refreshContacts() async {
        guard let client = settings.client else { return }
        let signature = settings.address + settings.credentialForSignature
        do {
            let result: [EmergencyContact] = try await client.request("contacts")
            guard signature == settings.address + settings.credentialForSignature else { return }
            contacts = result
            updateDeliveryServiceStatus(confirmedContacts: result.filter(\.confirmed).count)
        } catch {
            guard signature == settings.address + settings.credentialForSignature else { return }
            deliveryServiceStatus = "Could not refresh contacts; automatic alert setup is unverified."
            self.error = error.localizedDescription
        }
    }

    func addContact(name: String, phone: String) async -> EmergencyContact? {
        guard let client = settings.client else { error = "Connect your backend first."; return nil }
        error = nil
        guard phone.range(of: #"^\+[1-9][0-9]{7,14}$"#, options: .regularExpression) != nil else {
            error = "Include the country code, for example +14045550101."; return nil
        }
        guard !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, name.count <= 60 else {
            error = "Enter a first name of 1–60 characters."; return nil
        }
        do {
            let contact: EmergencyContact = try await client.request("contacts", method: "POST", json: ["first_name": name, "phone": phone])
            await refreshContacts()
            return contact
        } catch { self.error = error.localizedDescription; return nil }
    }

    func removeContact(_ contact: EmergencyContact) async {
        guard let client = settings.client else { return }
        error = nil
        do { _ = try await client.data("contacts/\(contact.phone)", method: "DELETE"); await refreshContacts() }
        catch {
            let failure = error.localizedDescription
            await refreshContacts()
            self.error = failure
        }
    }

    func sendTestMessage(to contact: EmergencyContact) async -> ContactTestOutcome {
        guard contact.confirmed else {
            return ContactTestOutcome(accepted: false, detail: "This contact must reply YES before receiving messages.")
        }
        guard let client = settings.client else {
            return ContactTestOutcome(accepted: false, detail: "Connect BrakeCheck to its backend before sending a test.")
        }
        do {
            let response: ContactTestResponse = try await client.request(
                "contacts/\(contact.phone)/test", method: "POST", timeout: 30)
            guard response.accepted else {
                return ContactTestOutcome(accepted: false, detail: "Photon did not accept the test message.")
            }
            return ContactTestOutcome(
                accepted: true,
                detail: "Photon accepted the test message for \(contact.first_name). Ask them to confirm it arrived; the app cannot verify delivery or reading.")
        } catch {
            return ContactTestOutcome(
                accepted: false,
                detail: "BrakeCheck could not confirm Photon accepted the test message. Ask \(contact.first_name) to check their messages before retrying. A one-minute cooldown applies after each attempt. \(error.localizedDescription)")
        }
    }

    func beginCheckin(location: CrashLocation?, speedMPH: Double?, dryRun: Bool) {
        guard !checkinPresented else { return }
        if dryRun { beginLocalDemo(); return }
        guard let client = settings.client else { error = "Connect the backend before starting a check-in."; return }
        beforePriorityAudio?()
        sessionGeneration = UUID()
        let generation = sessionGeneration
        awaitingAlertDelivery = false
        isDryRun = false; resolved = false; resultNeedsAttention = false
        sending = false; remaining = 60; connectionLost = false
        checkinTitle = "Possible impact detected"
        checkinDetail = "Connecting…"
        checkinPresented = true
        activeClient = client
        crashID = nil
        var payload: [String: Any] = ["lang": settings.language, "dry_run": false, "client_event_id": UUID().uuidString]
        if let location, Date().timeIntervalSince(location.measuredAt) < 15 {
            payload["lat"] = location.latitude; payload["lon"] = location.longitude
        }
        if let speedMPH { payload["speed_mph"] = speedMPH }
        flow = Task { [weak self] in
            guard let self else { return }
            do {
                let started: CrashStartResponse = try await client.request("crash/start", method: "POST", json: payload)
                guard self.sessionGeneration == generation, self.checkinPresented, !Task.isCancelled else { return }
                self.crashID = started.crash_id
                self.startPolling(client: client, id: started.crash_id, generation: generation)
                self.checkinTitle = "Are you okay?"
                self.checkinDetail = "Say I am okay or help, or use a button."
                var promptSpoken = false
                if self.settings.voiceEnabled {
                    do {
                        // The spoken question finishes before the microphone opens.
                        try await self.audio.speakCheckinPrompt(language: self.settings.language)
                        promptSpoken = true
                    } catch is CancellationError { return }
                    catch {
                        guard self.sessionGeneration == generation, self.checkinPresented,
                              !Task.isCancelled else { return }
                        self.checkinDetail = "Voice prompt unavailable. Use the check-in buttons."
                    }
                }
                try Task.checkCancellation()
                if promptSpoken && self.replyRecognizer.canRecognizeWithoutPrompt(language: self.settings.language) {
                    do {
                        self.checkinDetail = "Listening for 6 seconds. Use a button at any time."
                        let transcript = try await self.replyRecognizer.recognizeReply(
                            using: self.audio, language: self.settings.language)
                        try Task.checkCancellation()
                        guard self.sessionGeneration == generation, self.checkinPresented,
                              !self.resolved else { return }
                        let decision = CrashReplyClassifier.classify(transcript ?? "")
                        if decision != .unrecognized {
                            self.sending = true
                            self.checkinDetail = "Confirming your spoken reply."
                            do {
                                let result: CrashResponse = try await client.request(
                                    "crash/\(started.crash_id)/respond", method: "POST",
                                    json: ["status": decision.rawValue], timeout: 8)
                                guard self.sessionGeneration == generation, self.checkinPresented,
                                      !Task.isCancelled else { return }
                                self.finish(result)
                                return
                            } catch is CancellationError { return }
                            catch {
                                guard self.sessionGeneration == generation, self.checkinPresented,
                                      !Task.isCancelled else { return }
                                self.sending = false
                                self.checkinDetail = "Spoken reply not confirmed. Use a button before the countdown ends."
                            }
                        } else {
                            self.checkinDetail = "Could not confirm your words. Use a button before the countdown ends."
                        }
                    } catch is CancellationError { return }
                    catch {
                        guard self.sessionGeneration == generation, self.checkinPresented,
                              !Task.isCancelled, !self.awaitingAlertDelivery, !self.resolved else { return }
                        self.checkinDetail = "Spoken reply unavailable. Use a button before the countdown ends."
                    }
                } else if promptSpoken {
                    self.checkinDetail = "Spoken replies need permissions and on-device support. Use a button before the countdown ends."
                } else if !self.settings.voiceEnabled {
                    self.checkinDetail = "Choose below before the countdown ends."
                }
                // The backend keeps its 60-second fallback while device speech
                // runs. Arm only afterward so the buttons get a full 20 seconds.
                let armed: CrashResponse = try await client.request(
                    "crash/\(started.crash_id)/arm", method: "POST")
                guard self.sessionGeneration == generation, self.checkinPresented,
                      !Task.isCancelled else { return }
                if armed.status != "pending" { self.finish(armed); return }
                self.remaining = 20
            } catch is CancellationError { return }
            catch {
                guard self.sessionGeneration == generation, self.checkinPresented, !Task.isCancelled else { return }
                self.connectionLost = true
                self.checkinDetail = error.localizedDescription + " No alert delivery has been confirmed."
                // A lost start response might still have created a server session. Keep its ID
                // and attempt recovery so the user can cancel before its deadline.
                if self.crashID == nil, let id = payload["client_event_id"] as? String {
                    self.crashID = id
                    self.startPolling(client: client, id: id, generation: generation)
                }
            }
        }
    }

    private func beginLocalDemo() {
        beforePriorityAudio?()
        sessionGeneration = UUID()
        let generation = sessionGeneration
        awaitingAlertDelivery = false
        isDryRun = true; resolved = false; resultNeedsAttention = false
        sending = false; remaining = 20; connectionLost = false
        checkinTitle = "Demo check-in"
        checkinDetail = "No messages will be sent. Say I am okay or help, or use a button."
        activeClient = nil; crashID = nil; checkinPresented = true
        flow = Task { [weak self] in
            guard let self else { return }
            var promptSpoken = false
            if self.settings.voiceEnabled {
                do {
                    try await self.audio.speakCheckinPrompt(language: self.settings.language)
                    promptSpoken = true
                } catch is CancellationError { return }
                catch { self.checkinDetail = "Voice prompt unavailable. Use the check-in buttons." }
            }
            guard self.sessionGeneration == generation, self.checkinPresented,
                  !self.resolved, !Task.isCancelled else { return }
            self.startLocalDemoCountdown(generation: generation)
            guard promptSpoken else { return }
            do {
                self.checkinDetail = "Listening for 6 seconds. No messages will be sent."
                let transcript = try await self.replyRecognizer.recognizeReply(
                    using: self.audio, language: self.settings.language)
                guard self.sessionGeneration == generation, self.checkinPresented,
                      !self.resolved, !Task.isCancelled else { return }
                switch CrashReplyClassifier.classify(transcript ?? "") {
                case .ok: self.finishLocalDemo(status: "ok")
                case .help: self.finishLocalDemo(status: "help")
                case .unrecognized:
                    self.checkinDetail = "Could not confirm your words. Use a button before the countdown ends."
                }
            } catch is CancellationError { return }
            catch {
                guard self.sessionGeneration == generation, self.checkinPresented,
                      !self.resolved, !Task.isCancelled else { return }
                self.checkinDetail = "Spoken reply unavailable. Use a button before the countdown ends."
            }
        }
    }

    private func startLocalDemoCountdown(generation: UUID) {
        let deadline = Date().addingTimeInterval(20)
        polling?.cancel()
        polling = Task { [weak self] in
            while !Task.isCancelled {
                guard let self, self.sessionGeneration == generation,
                      self.checkinPresented, !self.resolved else { return }
                self.remaining = max(0, Int(ceil(deadline.timeIntervalSinceNow)))
                if self.remaining == 0 {
                    self.finishLocalDemo(status: "no_response")
                    return
                }
                do { try await Task.sleep(nanoseconds: 250_000_000) } catch { return }
            }
        }
    }

    private func finishLocalDemo(status: String, speakAcknowledgement: Bool = true) {
        guard isDryRun, checkinPresented, !resolved else { return }
        resolved = true; sending = false; remaining = 0
        flow?.cancel(); polling?.cancel(); replyRecognizer.cancel(); audio.stop()
        checkinTitle = "Demo complete"
        switch status {
        case "ok": checkinDetail = "I am okay was recorded for this demo. No messages were sent."
        case "help": checkinDetail = "Help was recorded for this demo. No messages were sent."
        case "interrupted": checkinDetail = "Demo stopped when the app left the foreground. No messages were sent."
        default: checkinDetail = "No clear response was received. No messages were sent."
        }
        if speakAcknowledgement && settings.voiceEnabled {
            flow = Task { [weak self] in
                guard let self else { return }
                try? await self.audio.speakCheckinAcknowledgement(
                    status: .demo, language: self.settings.language)
            }
        }
    }

    private func startPolling(client: BackendClient, id: String, generation: UUID) {
        polling?.cancel()
        polling = Task { [weak self] in
            while !Task.isCancelled {
                do {
                    let state: CrashResponse = try await client.request("crash/\(id)")
                    guard let self else { return }
                    guard self.sessionGeneration == generation, self.crashID == id,
                          self.checkinPresented, !Task.isCancelled else { return }
                    self.connectionLost = false
                    if state.status != "pending" {
                        self.finish(state)
                        if self.resolved { return }
                    } else {
                        self.remaining = max(0, Int(ceil((state.deadline ?? Date().timeIntervalSince1970) - Date().timeIntervalSince1970)))
                    }
                } catch {
                    guard let self else { return }
                    guard self.sessionGeneration == generation, self.checkinPresented,
                          !Task.isCancelled else { return }
                    self.connectionLost = true
                    self.checkinDetail = "Connection interrupted. The backend countdown may still be running. Reconnect to confirm the result."
                }
                do { try await Task.sleep(nanoseconds: 1_000_000_000) } catch { return }
            }
        }
    }

    func respond(ok: Bool) {
        guard canRespond else { return }
        if isDryRun { finishLocalDemo(status: ok ? "ok" : "help"); return }
        guard let id = crashID, let client = activeClient else { return }
        let generation = sessionGeneration
        flow?.cancel(); replyRecognizer.cancel(); audio.stop(); sending = true
        flow = Task {
            defer { if sessionGeneration == generation && !awaitingAlertDelivery { sending = false } }
            do {
                let response: CrashResponse = try await client.request("crash/\(id)/respond", method: "POST", json: ["status": ok ? "ok" : "help"], timeout: 45)
                guard sessionGeneration == generation, checkinPresented, !Task.isCancelled else { return }
                finish(response)
            } catch {
                guard sessionGeneration == generation, checkinPresented, !Task.isCancelled else { return }
                checkinDetail = "Response not confirmed. \(error.localizedDescription) Tap again to retry."
            }
        }
    }

    private func finish(_ response: CrashResponse) {
        guard !resolved, checkinPresented, response.id == crashID else { return }
        if response.delivery_pending == true {
            awaitingAlertDelivery = true; sending = true; remaining = 0
            checkinTitle = "Sending contact alert…"
            checkinDetail = "Your response is recorded. Waiting for confirmation that Photon accepted the messages."
            audio.stop()
            return
        }
        awaitingAlertDelivery = false
        resultNeedsAttention = !response.dry_run && response.status != "ok"
            && (!response.alerted || (response.failed_count ?? 0) > 0)
        resolved = true; sending = false
        polling?.cancel(); flow?.cancel(); replyRecognizer.cancel(); audio.stop()
        if response.dry_run {
            checkinTitle = "Demo complete"
            checkinDetail = "Result: \(response.status.replacingOccurrences(of: "_", with: " ")). No emergency messages were sent."
        } else if response.status == "ok" {
            checkinTitle = "You're checked in"
            checkinDetail = "No emergency alert was sent. Available video remains in Incidents."
        } else if response.alerted {
            let failed = response.failed_count ?? 0
            checkinTitle = failed > 0 ? "Some alerts accepted" : "Alert accepted"
            checkinDetail = "Photon accepted messages for \(response.sent ?? 0) contact(s). "
                + (failed > 0 ? "\(failed) contact message(s) were not confirmed. " : "")
                + "Delivery and reading are not confirmed."
        } else {
            checkinTitle = "Alert not confirmed"
            checkinDetail = response.alert_error ?? "No emergency contact was notified."
        }
        if settings.voiceEnabled {
            let acknowledgement: LocalCheckinAcknowledgement = response.dry_run ? .demo
                : response.status == "ok" ? .okay
                : response.alerted ? (response.status == "no_response" ? .noResponse : .help)
                : .failed
            flow = Task { [weak self] in
                guard let self else { return }
                try? await self.audio.speakCheckinAcknowledgement(
                    status: acknowledgement, language: self.settings.language)
            }
        }
    }

    func closeUnconfirmed() {
        sessionGeneration = UUID(); awaitingAlertDelivery = false
        flow?.cancel(); polling?.cancel(); replyRecognizer.cancel(); audio.stop(); checkinPresented = false
        crashID = nil; activeClient = nil
    }

    func closeCheckin() {
        guard resolved else { return }
        sessionGeneration = UUID(); awaitingAlertDelivery = false
        flow?.cancel(); polling?.cancel(); replyRecognizer.cancel(); audio.stop(); checkinPresented = false
        crashID = nil; activeClient = nil
    }

    func pauseAudio() {
        if isDryRun, checkinPresented, !resolved {
            finishLocalDemo(status: "interrupted", speakAcknowledgement: false)
            return
        }
        // The backend owns the timeout even if iOS suspends the app.
        flow?.cancel(); polling?.cancel(); polling = nil; replyRecognizer.cancel(); audio.stop()
    }

    func resumeForegroundPolling() {
        guard checkinPresented, !resolved, let client = activeClient, let id = crashID else { return }
        startPolling(client: client, id: id, generation: sessionGeneration)
    }

    func sendTrip(minutes: Double, hardBrakes: Int, closeCalls: Int, maxSpeedMPH: Double) {
        guard let client = settings.client else { return }
        Task {
            do {
                _ = try await client.data("trip", method: "POST", json: ["minutes": minutes, "hardBrakes": hardBrakes,
                    "closeCalls": closeCalls, "maxSpeedMph": maxSpeedMPH, "time": Date().timeIntervalSince1970])
            } catch { self.error = "Photon trip summary unavailable: \(error.localizedDescription)" }
        }
    }
}

struct ContactTestOutcome {
    let accepted: Bool
    let detail: String
}
