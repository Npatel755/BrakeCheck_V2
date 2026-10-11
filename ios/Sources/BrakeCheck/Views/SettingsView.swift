import SwiftUI
import UIKit

struct SettingsView: View {
    @ObservedObject var model: DriveModel
    @ObservedObject var service: SafetyService
    @ObservedObject var settings: ConnectionSettings
    @Environment(\.dismiss) private var dismiss
    @State private var busy = false
    @State private var pairing = false
    @State private var showingPairingScanner = false
    @Binding var requestDemo: Bool

    var body: some View {
        NavigationStack {
            Form {
                Section("Connection") {
                    Button(pairing ? "Pairing…" : "Scan pairing QR") {
                        showingPairingScanner = true
                    }
                    .disabled(pairing || busy)

                    if settings.isPaired {
                        Label("Paired · \(settings.address)", systemImage: "checkmark.shield.fill")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    } else {
                        Text("Run `python pair.py` beside a local backend, or add `--backend-url https://your-service.koyeb.app` for a hosted backend.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }

                    Button(busy ? "Connecting…" : "Test saved connection") {
                        busy = true
                        Task {
                            await service.connect()
                            busy = false
                        }
                    }
                    .disabled(busy || pairing)

                    Label(service.connectionStatus, systemImage: "network")
                    if let error = service.error {
                        Text(error)
                            .font(.caption)
                            .foregroundStyle(.orange)
                    }

                    DisclosureGroup("Advanced · manual connection") {
                        TextField("Backend URL · https://… or http://PC:8000", text: $settings.address)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .keyboardType(.URL)

                        SecureField("Backend app key", text: $settings.appKey)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()

                        Button(busy ? "Connecting…" : "Save & test manual connection") {
                            busy = true
                            Task {
                                do {
                                    try settings.saveKey()
                                    await service.connect()
                                } catch {
                                    service.error = error.localizedDescription
                                }
                                busy = false
                            }
                        }
                        .disabled(busy || pairing)

                        Text("Use the BRAKECHECK_APP_KEY from your private backend .env. Keep it out of screenshots and messages.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

                Section("Voice · ElevenLabs") {
                    Toggle("Spoken alerts and crash check-in", isOn: $settings.voiceEnabled)

                    Picker("Language", selection: $settings.language) {
                        Text("English").tag("en")
                        Text("Español").tag("es")
                    }

                    Button(service.preparing ? "Preparing…" : "Prepare voice for this trip") {
                        Task { await service.prepareVoice() }
                    }
                    .disabled(service.preparing || settings.client == nil)

                    Button("Allow microphone for spoken replies") {
                        Task { await service.requestMicrophone() }
                    }

                    Text(service.voiceStatus)
                        .font(.caption)
                        .foregroundStyle(.secondary)

                    Text("The microphone records a 6-second reply after the check-in prompt ends. Your backend sends that reply to ElevenLabs.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Section("Possible impacts") {
                    Toggle("Experimental impact detection", isOn: $model.impactEnabled)
                        .disabled(model.demo)

                    Toggle("Start an emergency check-in on impact", isOn: $settings.autoCheckin)
                        .disabled(!model.impactEnabled || settings.client == nil)

                    Text("An enabled check-in can message confirmed contacts if you ask for help or do not respond. A phone drop can trigger it. No emergency services are called automatically.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Section("Try BrakeCheck") {
                    Toggle(
                        "Simulated driving metrics",
                        isOn: Binding(
                            get: { model.demo },
                            set: { model.setDemo($0) }
                        )
                    )
                    .disabled(model.tripActive || model.stopping)

                    Button("Run crash check-in demo") {
                        requestDemo = true
                        dismiss()
                    }
                    .disabled(settings.client == nil)

                    Text("The check-in demo can use ElevenLabs credits but never sends emergency messages.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Section("Permissions") {
                    Button("Open iPhone settings") {
                        if let url = URL(string: UIApplication.openSettingsURLString) {
                            UIApplication.shared.open(url)
                        }
                    }

                    Text("Keep BrakeCheck visible for camera recording. Video pauses when the phone locks or the app goes into the background.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
        .sheet(isPresented: $showingPairingScanner) {
            PairingScannerSheet { qrText in
                beginPairing(qrText)
            }
        }
        .tint(BrakeTheme.accent)
    }

    // The scanner passes the FULL JSON string, not just the temporary code.
    private func beginPairing(_ qrText: String) {
        guard !pairing else { return }

        pairing = true
        service.error = nil

        Task {
            defer { pairing = false }
            do {
                try await settings.pair(
                    qrString: qrText,
                    deviceName: UIDevice.current.name
                )
                await service.connect()
            } catch {
                service.error = error.localizedDescription
            }
        }
    }
}

struct DiagnosticsView: View {
    @ObservedObject var model: DriveModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section("Sensors") {
                    Label(model.gpsStatus, systemImage: "location")
                    Label(model.cameraStatus, systemImage: "video")
                    Label(model.recorderStatus, systemImage: "record.circle")
                    Label(model.detectorStatus, systemImage: "viewfinder")
                    Label(model.motionStatus, systemImage: "waveform.path")
                    Text(model.trackingStatus)
                }

                Section("Experimental fusion · display only") {
                    Text(model.fusedSpeed.map { String(format: "%.1f mph", $0 * 2.2369362921) } ?? "Unavailable")
                    Text(model.fusedSpeedUncertainty.map { String(format: "Estimated uncertainty ±%.1f mph", $0 * 2.2369362921) } ?? "No fresh estimate")
                    Text(model.fusedSpeedSource)
                    Text(model.fusionStatus)
                    Text("Collision estimates use GPS, not this experimental fused speed.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Section("This trip") {
                    LabeledContent("Duration", value: String(format: "%.1f min", model.tripMinutes))
                    LabeledContent("Estimated lead hard brakes", value: "\(model.hardBrakes)")
                    LabeledContent("Estimated TTC below 2 seconds", value: "\(model.closeCalls)")
                    LabeledContent("Maximum GPS speed", value: String(format: "%.0f mph", model.maxSpeedMPH))
                    LabeledContent("Your acceleration", value: model.egoAcceleration.map { String(format: "%.1f m/s²", $0) } ?? "—")
                    LabeledContent("Estimated lead acceleration", value: model.risk.leadAcceleration.map { String(format: "%.1f m/s²", $0) } ?? "—")
                }
            }
            .font(.subheadline)
            .navigationTitle("Diagnostics")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
        .tint(BrakeTheme.accent)
    }
}
