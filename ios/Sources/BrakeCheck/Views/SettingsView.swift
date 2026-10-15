import AVFoundation
import SwiftUI
import UIKit

private enum SettingsCategory: String, CaseIterable, Identifiable {
    case appearance, camera, recordings, connection, voice, safety, permissions, demo, about
    var id: String { rawValue }
    var title: String {
        switch self {
        case .appearance: return "Appearance"
        case .camera: return "Camera display"
        case .recordings: return "Recordings & storage"
        case .connection: return "Connection"
        case .voice: return "Voice"
        case .safety: return "Safety"
        case .permissions: return "Permissions"
        case .demo: return "Try BrakeCheck"
        case .about: return "About & licences"
        }
    }
    var icon: String {
        switch self {
        case .appearance: return "paintbrush"
        case .camera: return "viewfinder"
        case .recordings: return "externaldrive"
        case .connection: return "network"
        case .voice: return "speaker.wave.2"
        case .safety: return "shield"
        case .permissions: return "lock.shield"
        case .demo: return "play.rectangle"
        case .about: return "info.circle"
        }
    }
    var detail: String {
        switch self {
        case .appearance: return "Choose the look that feels right."
        case .camera: return "Units and overlays for your road view."
        case .recordings: return "Manage video space, speed logs, and optional automatic cleanup."
        case .connection: return "Connect your private BrakeCheck service."
        case .voice: return AppScope.connectedServices ? "Spoken alerts, language, and check-in replies." : "Spoken alerts and language."
        case .safety: return AppScope.connectedServices ? "Impact detection and emergency check-ins." : "Impact detection and saved footage."
        case .permissions: return "Manage access to your iPhone's sensors."
        case .demo: return AppScope.connectedServices ? "Explore sample driving and check-in features." : "Explore a simulated drive."
        case .about: return "How BrakeCheck works, its limits, and open-source licences."
        }
    }
}

struct SettingsView: View {
    @ObservedObject var model: DriveModel
    @ObservedObject var service: SafetyService
    @ObservedObject var settings: ConnectionSettings
    @State private var busy = false
    @State private var pairing = false
    @State private var showingPairingScanner = false
    @State private var selectedCategory: SettingsCategory = .appearance
    @Binding var requestDemo: Bool
    @AppStorage("display.appearance") private var appearance = "dark"
    @AppStorage("display.accent") private var accentChoice = "blue"
    @AppStorage("voice.selectedVoice") private var selectedVoice = ""
    @AppStorage("voice.delivery") private var voiceDelivery = "original"
    @AppStorage("voice.backendAlerts") private var backendAlerts = true
    @AppStorage("display.mph") private var useMPH = true
    @AppStorage("display.detectionBoxes") private var showDetections = true
    @AppStorage("display.lanes") private var showLanes = true
    @AppStorage("display.fullCameraFrame") private var showFullFrame = true
    @AppStorage("recording.limitGB") private var recordingLimitGB = 0
    @AppStorage("recording.retentionDays") private var recordingRetentionDays = 0
    @State private var pendingRetentionDays = 0
    @State private var confirmRecordingCleanup = false
    @State private var confirmSpeedLogDelete = false
    @State private var speedLogCount = SpeedLogReader.savedLogs().count

    private var groups: [(String, [SettingsCategory])] {
        let services: (String, [SettingsCategory]) = AppScope.connectedServices
            ? ("Connected services", [.connection, .voice]) : ("Sound", [.voice])
        return [("Your app", [.appearance, .camera, .recordings]), services,
                ("Safety", [.safety, .permissions]), ("Explore", [.demo, .about])]
    }

    var body: some View {
        GeometryReader { geometry in
            if geometry.size.width >= 720 {
                HStack(alignment: .top, spacing: 28) {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 24) {
                            hubIdentity
                            ForEach(groups, id: \.0) { group in
                                VStack(alignment: .leading, spacing: 5) {
                                    groupLabel(group.0)
                                    ForEach(group.1) { category in
                                        Button { selectedCategory = category } label: {
                                            Label(category.title, systemImage: category.icon)
                                                .font(.brake(.subheadline, weight: selectedCategory == category ? .semibold : .regular))
                                                .frame(maxWidth: .infinity, alignment: .leading)
                                                .padding(.horizontal, 14).frame(minHeight: 48)
                                                .foregroundStyle(selectedCategory == category ? BrakeTheme.accent : BrakeTheme.text)
                                                .background(selectedCategory == category ? BrakeTheme.accent.opacity(0.12) : .clear,
                                                            in: RoundedRectangle(cornerRadius: 10))
                                                .overlay(alignment: .leading) {
                                                    if selectedCategory == category {
                                                        Capsule().fill(BrakeTheme.accent).frame(width: 3, height: 28)
                                                    }
                                                }
                                        }
                                        .buttonStyle(.plain)
                                        .accessibilityValue(selectedCategory == category ? "Selected" : "")
                                    }
                                }
                            }
                        }
                        .padding(.vertical, 24)
                    }
                    .frame(width: 230)
                    categoryPage(selectedCategory)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
                .padding(.horizontal, 24)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 24) {
                        hubIdentity
                        ForEach(groups, id: \.0) { group in
                            VStack(alignment: .leading, spacing: 12) {
                                groupLabel(group.0)
                                VStack(spacing: 0) {
                                    ForEach(group.1) { category in
                                        NavigationLink {
                                            categoryPage(category)
                                                .modifier(DedicatedPageStyle())
                                        } label: {
                                            categoryRow(category)
                                        }
                                        .buttonStyle(.plain)
                                        if category != group.1.last {
                                            Divider().padding(.leading, 56)
                                        }
                                    }
                                }
                                .padding(.horizontal, 16)
                                .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 20))
                            }
                        }
                    }
                    .padding(20).frame(maxWidth: 640).frame(maxWidth: .infinity)
                }
                .scrollIndicators(.hidden)
            }
        }
        .background(BrakeTheme.background)
        .navigationTitle("Settings")
        .navigationBarTitleDisplayMode(.inline)
        .onChange(of: settings.language) { _, _ in selectedVoice = "" }
        .onChange(of: backendAlerts) { _, enabled in
            if enabled { Task { await service.prepareVoice() } }
        }
        .tint(BrakeTheme.accentColor(named: accentChoice))
    }

    private var hubIdentity: some View {
        HStack(spacing: 14) {
            Image(systemName: "gearshape.fill").font(.brake(size: 25))
                .foregroundStyle(BrakeTheme.accent).frame(width: 52, height: 52)
                .background(BrakeTheme.accent.opacity(0.12), in: RoundedRectangle(cornerRadius: 16))
            VStack(alignment: .leading, spacing: 5) {
                Text("Make BrakeCheck yours.").font(.brake(size: 23, weight: .semibold))
                Text("Your preferences, in one place.").font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func groupLabel(_ title: String) -> some View {
        Text(title).font(.brake(.caption, weight: .semibold)).foregroundStyle(BrakeTheme.muted)
            .padding(.horizontal, 4)
    }

    private func categoryRow(_ category: SettingsCategory) -> some View {
        HStack(spacing: 14) {
            Image(systemName: category.icon).font(.brake(size: 19)).foregroundStyle(BrakeTheme.accent)
                .frame(width: 26).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(category.title).font(.brake(.subheadline, weight: .semibold)).foregroundStyle(BrakeTheme.text)
                Text(category.detail).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 4)
            Image(systemName: "chevron.right").font(.brake(.caption, weight: .semibold)).foregroundStyle(BrakeTheme.muted)
        }
        .padding(.vertical, 16).frame(minHeight: 64)
        .contentShape(Rectangle())
    }

    private func categoryPage(_ category: SettingsCategory) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(category.title).font(.brake(size: 28, weight: .semibold))
                    Text(category.detail).font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
                }
                Divider()
                VStack(alignment: .leading, spacing: 20) {
                    controls(category)
                }
                .font(.brake(.subheadline))
                .textFieldStyle(.roundedBorder)
                .buttonStyle(.bordered)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
                .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 20))
            }
            .padding(.vertical, 24).padding(.horizontal, 20)
            .frame(maxWidth: 780).frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollIndicators(.hidden)
        .background(BrakeTheme.background)
        .foregroundStyle(BrakeTheme.text)
        .navigationTitle(category.title)
        .navigationBarTitleDisplayMode(.inline)
        .fullScreenCover(isPresented: $showingPairingScanner) {
            PairingScannerSheet { beginPairing($0) }
        }
        .alert("Automatically delete old incident videos?", isPresented: $confirmRecordingCleanup) {
            Button("Enable cleanup", role: .destructive) {
                recordingRetentionDays = pendingRetentionDays
                model.applyRecordingPreferences()
            }
            Button("Cancel", role: .cancel) { }
        } message: {
            Text("Saved incidents older than \(pendingRetentionDays) days will be permanently deleted when recording is idle. Export anything you want to keep first. Trip summaries are kept.")
        }
    }

    @ViewBuilder private func controls(_ category: SettingsCategory) -> some View {
        switch category {
        case .appearance:
            Picker("Theme", selection: $appearance) {
                Text("Light").tag("light")
                Text("Dark").tag("dark")
            }.pickerStyle(.segmented)
            Text("Personalize your accent").font(.brake(.headline))
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 80))], spacing: 12) {
                ForEach(AccentChoice.allCases) { choice in
                    Button { accentChoice = choice.rawValue } label: {
                        VStack(spacing: 8) {
                            Circle().fill(BrakeTheme.accentColor(named: choice.rawValue))
                                .frame(width: 34, height: 34)
                                .overlay {
                                    if accentChoice == choice.rawValue {
                                        Image(systemName: "checkmark").font(.brake(.caption, weight: .bold)).foregroundStyle(.white)
                                    }
                                }
                            Text(choice.title).font(.brake(.caption)).foregroundStyle(BrakeTheme.text)
                        }.frame(maxWidth: .infinity, minHeight: 72)
                    }.buttonStyle(.plain)
                        .accessibilityLabel("\(choice.title) accent")
                        .accessibilityValue(accentChoice == choice.rawValue ? "Selected" : "")
                }
            }
            Text("Appearance applies to Home, Camera, Navigation, and these settings.")
                .foregroundStyle(BrakeTheme.muted)
        case .camera:
            Picker("Units", selection: $useMPH) {
                Text("mph · ft").tag(true)
                Text("km/h · m").tag(false)
            }.pickerStyle(.segmented)
            Toggle("Show vehicle boxes", isOn: $showDetections)
            Toggle("Show lane and road overlay", isOn: $showLanes)
            Toggle("Show full camera frame", isOn: $showFullFrame)
            Text("These preferences also apply to replayed drives.").foregroundStyle(BrakeTheme.muted)
        case .recordings:
            LabeledContent("Video space used", value: model.recordingStorageBytes.map {
                ByteCountFormatter.string(fromByteCount: $0, countStyle: .file)
            } ?? "Checking…")
            Picker("Recording storage limit", selection: $recordingLimitGB) {
                Text("No app limit").tag(0)
                ForEach([1, 2, 5, 10], id: \.self) { value in Text("\(value) GB").tag(value) }
            }
            .onChange(of: recordingLimitGB) { _, _ in model.applyRecordingPreferences() }
            Text("At the limit, recording pauses and saved incident footage is preserved. Camera detection can continue. Recording also stops if the iPhone has less than 500 MB free.")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            Text("Free space or increase the limit, then restart the trip to resume recording.")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            Picker("Automatically delete saved incidents", selection: Binding(
                get: { recordingRetentionDays },
                set: { value in
                    if value == 0 {
                        recordingRetentionDays = 0
                        model.applyRecordingPreferences()
                    } else if value != recordingRetentionDays {
                        pendingRetentionDays = value
                        confirmRecordingCleanup = true
                    }
                })) {
                Text("Off · keep saved incidents").tag(0)
                ForEach([7, 30, 90], id: \.self) { days in Text("After \(days) days").tag(days) }
            }
            Text("Cleanup runs between trips. Active incident footage is protected. Exported copies and trip metrics are not deleted.")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            Button("Refresh storage usage") { model.camera.refreshIncidents() }
            Divider()
            LabeledContent("Saved speed logs", value: "\(speedLogCount)")
            Text("Each trip saves a small log of your GPS speed (no location) so the drive can be replayed with its video.")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            Button("Delete speed logs", role: .destructive) { confirmSpeedLogDelete = true }
                .disabled(speedLogCount == 0)
                .alert("Delete all saved speed logs?", isPresented: $confirmSpeedLogDelete) {
                    Button("Delete", role: .destructive) {
                        model.deleteSpeedLogs()
                        speedLogCount = SpeedLogReader.savedLogs().count
                    }
                    Button("Cancel", role: .cancel) { }
                } message: {
                    Text("Replays of past drives will use an assumed speed instead. The log for a trip in progress is kept.")
                }
        case .connection:
            Button(pairing ? "Pairing…" : "Scan pairing QR") { showingPairingScanner = true }
                .disabled(pairing || busy)
            if settings.isPaired {
                Label("Paired · \(settings.address)", systemImage: "checkmark.shield.fill")
                    .foregroundStyle(BrakeTheme.muted)
            } else {
                Text("Scan the pairing code from your BrakeCheck service, or enter its address below.")
                    .foregroundStyle(BrakeTheme.muted)
            }
            Button(busy ? "Connecting…" : "Test saved connection") {
                busy = true
                Task { await service.connect(); busy = false }
            }.disabled(busy || pairing)
            Label(service.connectionStatus, systemImage: "network")
            if let error = service.error { Text(error).foregroundStyle(BrakeTheme.warning) }
            DisclosureGroup("Manual connection") {
                VStack(alignment: .leading, spacing: 16) {
                    TextField("Backend URL", text: $settings.address)
                        .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                    SecureField("Backend app key", text: $settings.appKey)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    Button(busy ? "Connecting…" : "Save and test connection") {
                        busy = true
                        Task {
                            do { try settings.saveKey(); await service.connect() }
                            catch { service.error = error.localizedDescription }
                            busy = false
                        }
                    }.disabled(busy || pairing)
                    Text("Keep your private app key out of screenshots and messages.")
                        .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                }.padding(.top, 14)
            }
        case .voice:
            Toggle("Spoken alerts and crash check-in", isOn: $settings.voiceEnabled)
            Picker("Device voice", selection: $selectedVoice) {
                Text("Original BrakeCheck voice").tag("")
                ForEach(SpeechVoicePreferences.availableVoices(language: settings.language), id: \.identifier) { voice in
                    Text(SpeechVoicePreferences.label(voice)).tag(voice.identifier)
                }
            }
            Picker("Delivery", selection: $voiceDelivery) {
                Text("Original").tag("original")
                Text("Gentle").tag("gentle")
                Text("Balanced").tag("balanced")
                Text("Clear").tag("clear")
            }.pickerStyle(.segmented)
            Button("Restore original voice") {
                SpeechVoicePreferences.restoreOriginalDefaults()
            }
            Button("Preview voice") { service.previewVoice() }
                .disabled(model.tripActive || service.checkinPresented)
            Text("Device voices apply to directions and brake alerts. Preview while parked. Brake warnings remain clear and at full volume.")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            if AppScope.connectedServices {
                Toggle("Use ElevenLabs for brake alerts", isOn: $backendAlerts)
                Text("ElevenLabs check-in audio uses the voice configured on your connected service.")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            }
            Picker("Language", selection: $settings.language) {
                Text("English").tag("en")
                Text("Español").tag("es")
            }
            if AppScope.connectedServices {
                Button(service.preparing ? "Preparing…" : "Prepare voice for this trip") {
                    Task { await service.prepareVoice() }
                }.disabled(service.preparing || settings.client == nil || !backendAlerts)
                Button("Allow microphone for spoken replies") {
                    Task { await service.requestMicrophone() }
                }
                Text(service.voiceStatus).foregroundStyle(BrakeTheme.muted)
                Text("The microphone records a 6-second reply after the check-in prompt ends. Your backend sends that reply to ElevenLabs.")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            }
        case .safety:
            Toggle("Experimental impact detection", isOn: $model.impactEnabled).disabled(model.demo)
            if AppScope.connectedServices {
                Toggle("Start an emergency check-in on impact", isOn: $settings.autoCheckin)
                    .disabled(!model.impactEnabled || settings.client == nil)
                Text("An enabled check-in can message confirmed contacts if you ask for help or do not respond. A phone drop can trigger it. No emergency services are called automatically.")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            } else {
                Text("A possible impact saves the available video around it to Incidents. With Crash texts set up (Home › Crash texts), BrakeCheck also asks if you're okay and can text your contacts. A phone drop can trigger it. BrakeCheck never calls emergency services.")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            }
        case .permissions:
            Button("Open iPhone settings") {
                if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
            }
            Text("Keep BrakeCheck visible for camera recording. Video pauses when the phone locks or the app goes into the background.")
                .foregroundStyle(BrakeTheme.muted)
        case .demo:
            Toggle("Simulated driving metrics", isOn: Binding(get: { model.demo }, set: { model.setDemo($0) }))
                .disabled(model.tripActive || model.stopping)
            if AppScope.connectedServices {
                Button("Run crash check-in demo") { requestDemo = true }
                    .disabled(settings.client == nil)
                Text("The check-in demo can use ElevenLabs credits but never sends emergency messages.")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            }
        case .about:
            Text("A driver aid, not a safety system").font(.brake(.headline))
            Text("BrakeCheck estimates the gap and closing speed to the car ahead from the camera and GPS. Its warnings can be late, missed, or wrong, especially at night, in rain, or with a loose mount. Keep your eyes on the road, and set up the phone before you drive.")
                .foregroundStyle(BrakeTheme.muted)
            Text("Open-source models").font(.brake(.headline))
            Text("Vehicle detection: RTMDet-tiny and RTMDet-small, © OpenMMLab, repackaged by LibreYOLO. Apache License 2.0.")
            Text("Vehicle direction: Car orientation model, fort-cyber/Car-orientation-image. Apache License 2.0.")
            if AppScope.neuralLanes {
                Text("Lane detection: YOLOP, hustvl. MIT License.")
            }
            ForEach(Self.licenceFiles, id: \.title) { file in
                if let text = Self.bundledText(folder: file.folder, name: file.name) {
                    DisclosureGroup(file.title) {
                        Text(text).font(.caption.monospaced()).foregroundStyle(BrakeTheme.muted)
                            .textSelection(.enabled).padding(.top, 8)
                    }
                }
            }
            Text(Self.versionText).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
        }
    }

    private struct LicenceFile {
        let title: String
        let folder: String
        let name: String
    }

    private static var licenceFiles: [LicenceFile] {
        var files = [LicenceFile(title: "Detector notice", folder: "ModelLicenses", name: "NOTICE.md"),
                     LicenceFile(title: "Apache License 2.0", folder: "ModelLicenses", name: "Apache-2.0.txt"),
                     LicenceFile(title: "LibreYOLO (MIT)", folder: "ModelLicenses", name: "LibreYOLO-MIT.txt"),
                     LicenceFile(title: "Car orientation model (Apache-2.0)", folder: "ViewpointModelData", name: "CFV-LICENSE.txt"),
                     LicenceFile(title: "Red Hat Display font (OFL-1.1)", folder: "Fonts", name: "OFL.txt")]
        if AppScope.neuralLanes {
            files.append(LicenceFile(title: "YOLOP (MIT)", folder: "ModelLicenses", name: "YOLOP-MIT.txt"))
        }
        return files
    }

    private static func bundledText(folder: String, name: String) -> String? {
        for bundle in [Bundle.module, Bundle.main] {
            guard let directory = bundle.url(forResource: folder, withExtension: nil) else { continue }
            if let text = try? String(contentsOf: directory.appendingPathComponent(name), encoding: .utf8) { return text }
        }
        return nil
    }

    private static var versionText: String {
        let info = Bundle.main.infoDictionary
        let version = info?["CFBundleShortVersionString"] as? String ?? "?"
        let build = info?["CFBundleVersion"] as? String ?? "?"
        return "BrakeCheck \(version) (\(build))"
    }

    private func beginPairing(_ qrText: String) {
        guard !pairing else { return }
        pairing = true; service.error = nil
        Task {
            defer { pairing = false }
            do {
                try await settings.pair(qrString: qrText, deviceName: UIDevice.current.name)
                await service.connect()
            } catch { service.error = error.localizedDescription }
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
                        .font(.brake(.caption))
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
            .font(.brake(.subheadline))
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
