import Foundation
import Combine
import UIKit

@MainActor
final class SafetyService: ObservableObject {
    let settings = ConnectionSettings()
    let audio = VoiceAudio()
    @Published var connectionStatus = "Connect your backend"
    @Published var voiceStatus = "Voice not prepared"
    @Published var contacts: [EmergencyContact] = []
    @Published var error: String?
    @Published var checkinPresented = false
    @Published var resolved = false
    @Published var checkinTitle = "Are you okay?"
    @Published var checkinDetail = ""
    @Published var remaining = 20
    @Published var isDryRun = true
    @Published var sending = false
    @Published var preparing = false
    @Published var connectionLost = false
    private var brakeAudio: Data?
    private var preparedSignature = ""
    private var flow: Task<Void, Never>?
    private var polling: Task<Void, Never>?
    private var crashID: String?
    private var activeClient: BackendClient?
    private var lastBrake: TimeInterval = -.infinity

    var canRespond: Bool { crashID != nil && !resolved && !sending }

    func connectIfPaired() async {
        guard settings.isPaired else { return }
        await connect()
    }

    func connect() async {
        error = nil
        do {
            if !settings.isPaired { try settings.saveKey() }
            var client = settings.client
            if client == nil && settings.isPaired {
                settings.address = try await BonjourServiceDiscovery().discoverBackend().absoluteString
                client = settings.client
            }
            guard let currentClient = client else {
                throw APIError.message("Pair with the computer or enter a local backend URL and app key.")
            }
            let result: BackendHealth
            do {
                result = try await currentClient.request("health", timeout: 6)
            } catch {
                guard settings.isPaired else { throw error }
                settings.address = try await BonjourServiceDiscovery().discoverBackend().absoluteString
                guard let refreshedClient = settings.client else { throw error }
                result = try await refreshedClient.request("health", timeout: 8)
            }
            connectionStatus = "Connected · \(result.confirmed_contacts) contacts ready"
            voiceStatus = result.voice_configured ? "ElevenLabs configured" : "Add the ElevenLabs key on the backend"
            await refreshContacts()
        } catch { connectionStatus = "Connection unavailable"; self.error = error.localizedDescription }
    }

    func prepareVoice() async {
        guard settings.voiceEnabled, let client = settings.client, !preparing else { return }
        preparing = true
        defer { preparing = false }
        do {
            brakeAudio = try await client.data("voice/alert", method: "POST", json: ["kind": "brake", "lang": settings.language], timeout: 40)
            preparedSignature = settings.address + settings.language + settings.credentialForSignature
            voiceStatus = "ElevenLabs voice ready"
        } catch { brakeAudio = nil; voiceStatus = "Device voice fallback · backend unavailable" }
    }

    func requestMicrophone() async {
        let granted = await audio.requestMicrophone()
        if !granted { error = "Microphone permission is off. Check-ins still offer on-screen buttons." }
    }

    func brakeWarning() {
        let now = ProcessInfo.processInfo.systemUptime
        guard settings.voiceEnabled, !checkinPresented, now - lastBrake > 8 else { return }
        lastBrake = now
        // Use the voice cooldown so a persistent critical state does not
        // vibrate on every camera frame or add a second warning timer.
        UINotificationFeedbackGenerator().notificationOccurred(.warning)
        if let data = brakeAudio, preparedSignature == settings.address + settings.language + settings.credentialForSignature {
            Task { try? await audio.play(data) }
        } else {
            audio.speakBrake(language: settings.language)
        }
    }

    func refreshContacts() async {
        guard let client = settings.client else { return }
        do { contacts = try await client.request("contacts") }
        catch { self.error = error.localizedDescription }
    }

    func addContact(name: String, phone: String) async -> EmergencyContact? {
        guard let client = settings.client else { error = "Connect your backend first."; return nil }
        do {
            let contact: EmergencyContact = try await client.request("contacts", method: "POST", json: ["first_name": name, "phone": phone])
            await refreshContacts()
            return contact
        } catch { self.error = error.localizedDescription; return nil }
    }

    func removeContact(_ contact: EmergencyContact) async {
        guard let client = settings.client else { return }
        do { _ = try await client.data("contacts/\(contact.phone)", method: "DELETE"); await refreshContacts() }
        catch { self.error = error.localizedDescription }
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
        guard let client = settings.client else { error = "Connect the backend before starting a check-in."; return }
        isDryRun = dryRun; resolved = false; sending = false; remaining = 60; connectionLost = false
        checkinTitle = dryRun ? "Demo check-in" : "Possible impact detected"
        checkinDetail = "Connecting…"
        checkinPresented = true
        activeClient = client
        crashID = nil
        var payload: [String: Any] = ["lang": settings.language, "dry_run": dryRun, "client_event_id": UUID().uuidString]
        if let location, Date().timeIntervalSince(location.measuredAt) < 15 {
            payload["lat"] = location.latitude; payload["lon"] = location.longitude
        }
        if let speedMPH { payload["speed_mph"] = speedMPH }
        flow = Task { [weak self] in
            guard let self else { return }
            do {
                let started: CrashStartResponse = try await client.request("crash/start", method: "POST", json: payload)
                self.crashID = started.crash_id
                self.startPolling(client: client, id: started.crash_id)
                self.checkinTitle = "Are you okay?"
                self.checkinDetail = "Listen to the prompt, then say “I'm okay” or “help”."
                if self.settings.voiceEnabled {
                    do {
                        let data = try await client.data("crash/\(started.crash_id)/checkin.mp3", timeout: 10)
                        try Task.checkCancellation()
                        try await self.audio.play(data)
                    } catch is CancellationError { return }
                    catch { self.checkinDetail = "Voice prompt unavailable. Use the check-in buttons." }
                }
                try Task.checkCancellation()
                let armed: CrashResponse = try await client.request("crash/\(started.crash_id)/arm", method: "POST")
                if armed.status != "pending" { self.finish(armed); return }
                self.remaining = 20
                if self.settings.voiceEnabled {
                    do {
                        // Playback has completely finished before microphone capture starts.
                        try await Task.sleep(nanoseconds: 350_000_000)
                        self.checkinDetail = "Listening for 6 seconds… Say “I'm okay” or “help”."
                        let recording = try await self.audio.recordReply()
                        try Task.checkCancellation()
                        self.checkinDetail = "Checking your reply… You can still use a button."
                        let result = try await client.reply(crashID: started.crash_id, audio: recording)
                        self.finish(result)
                    } catch is CancellationError { return }
                    catch { self.checkinDetail = error.localizedDescription }
                } else { self.checkinDetail = "Choose below before the countdown ends." }
            } catch is CancellationError { return }
            catch {
                self.connectionLost = true
                self.checkinDetail = error.localizedDescription + " No alert delivery has been confirmed."
                // A lost start response might still have created a server session. Keep its ID
                // and attempt recovery so the user can cancel before its deadline.
                if self.crashID == nil, let id = payload["client_event_id"] as? String {
                    self.crashID = id
                    self.startPolling(client: client, id: id)
                }
            }
        }
    }

    private func startPolling(client: BackendClient, id: String) {
        polling?.cancel()
        polling = Task { [weak self] in
            while !Task.isCancelled {
                do {
                    let state: CrashResponse = try await client.request("crash/\(id)")
                    guard let self else { return }
                    self.connectionLost = false
                    if state.status != "pending" { self.finish(state); return }
                    self.remaining = max(0, Int(ceil((state.deadline ?? Date().timeIntervalSince1970) - Date().timeIntervalSince1970)))
                } catch {
                    guard let self else { return }
                    self.connectionLost = true
                    self.checkinDetail = "Connection interrupted. The backend countdown may still be running. Reconnect to confirm the result."
                }
                do { try await Task.sleep(nanoseconds: 1_000_000_000) } catch { return }
            }
        }
    }

    func respond(ok: Bool) {
        guard canRespond, let id = crashID, let client = activeClient else { return }
        flow?.cancel(); audio.stop(); sending = true
        flow = Task {
            defer { sending = false }
            do {
                let response: CrashResponse = try await client.request("crash/\(id)/respond", method: "POST", json: ["status": ok ? "ok" : "help"])
                finish(response)
            } catch { checkinDetail = "Response not confirmed. \(error.localizedDescription) Tap again to retry." }
        }
    }

    private func finish(_ response: CrashResponse) {
        guard !resolved else { return }
        resolved = true; sending = false
        polling?.cancel(); flow?.cancel(); audio.stop()
        if response.dry_run {
            checkinTitle = "Demo complete"
            checkinDetail = "Result: \(response.status.replacingOccurrences(of: "_", with: " ")). No emergency messages were sent."
        } else if response.status == "ok" {
            checkinTitle = "You're checked in"
            checkinDetail = "No emergency alert was sent. Available video remains in Incidents."
        } else if response.alerted {
            checkinTitle = "Alert sent"
            checkinDetail = "Photon accepted messages for \(response.sent ?? 0) contact(s). This does not confirm they have read them."
        } else {
            checkinTitle = "Alert not confirmed"
            checkinDetail = response.alert_error ?? "No emergency contact was notified."
        }
        if settings.voiceEnabled, let path = response.ack_audio_url, let client = activeClient {
            flow = Task {
                if let data = try? await client.data(path, timeout: 8), !Task.isCancelled {
                    try? await audio.play(data)
                }
            }
        }
    }

    func closeUnconfirmed() {
        flow?.cancel(); polling?.cancel(); audio.stop(); checkinPresented = false
        crashID = nil; activeClient = nil
    }

    func closeCheckin() {
        guard resolved else { return }
        flow?.cancel(); polling?.cancel(); audio.stop(); checkinPresented = false
        crashID = nil; activeClient = nil
    }

    func pauseAudio() {
        // The backend owns the timeout even if iOS suspends the app.
        flow?.cancel(); audio.stop()
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
