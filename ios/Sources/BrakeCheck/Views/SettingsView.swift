import AVFoundation
import BrakeCheckCore
import SwiftUI
import UIKit

private enum SettingsCategory: String, CaseIterable, Identifiable {
    case appearance, camera, recordings, connection, voice, safety, permissions, diagnostics, demo, about
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
        case .diagnostics: return "Diagnostics"
        case .demo: return "Try BrakeSense"
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
        case .diagnostics: return "waveform.path.ecg"
        case .demo: return "play.rectangle"
        case .about: return "info.circle"
        }
    }
    var detail: String {
        switch self {
        case .appearance: return "Choose the look that feels right."
        case .camera: return "Units and overlays for your road view."
        case .recordings: return "Manage video space and optional automatic cleanup."
        case .connection: return "Connect your private BrakeSense service."
        case .voice: return "Spoken alerts, language, and check-in replies."
        case .safety: return "Impact detection and emergency check-ins."
        case .permissions: return "Manage access to your iPhone's sensors."
        case .diagnostics: return "Camera, GPS, and measurement details."
        case .demo: return "Explore sample driving and check-in features."
        case .about: return "How BrakeSense works, its limits, and model notices."
        }
    }
}

/// Prevent parent dashboard camera updates from rebuilding this destination.
/// The child still observes its own settings, connection and control state.
struct SettingsScreen: View, Equatable {
    let model: DriveModel
    let service: SafetyService
    let settings: ConnectionSettings
    @Binding var requestDemo: Bool
    var openCameraSetup: (() -> Void)? = nil
    static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.model === rhs.model && lhs.service === rhs.service && lhs.settings === rhs.settings
            && lhs.requestDemo == rhs.requestDemo
    }
    var body: some View {
        SettingsView(model: model, arPresentation: model.arPresentation, service: service, settings: settings, requestDemo: $requestDemo, openCameraSetup: openCameraSetup)
    }
}

struct SettingsView: View {
    let model: DriveModel
    let arPresentation: CameraARPresentation
    @ObservedObject var service: SafetyService
    @ObservedObject var settings: ConnectionSettings
    @State private var tripActive = false
    @State private var stopping = false
    @State private var demoMode = false
    @State private var impactEnabled = false
    @State private var storageBytes: Int64?
    @State private var voiceOptions: [AVSpeechSynthesisVoice] = []
    @State private var voicePageVisible = false
    @State private var voiceLanguage: String?
    @State private var busy = false
    @State private var pairing = false
    @State private var showingPairingScanner = false
    @State private var backendAddressDraft = ""
    @State private var backendKeyDraft = ""
    @State private var selectedCategory: SettingsCategory = .appearance
    @Binding var requestDemo: Bool
    var openCameraSetup: (() -> Void)? = nil
    @AppStorage("display.appearance") private var appearance = "dark"
    @AppStorage("display.accent") private var accentChoice = BrakeTheme.defaultAccent
    @AppStorage("voice.selectedVoice") private var selectedVoice = ""
    @AppStorage("voice.delivery") private var voiceDelivery = "original"
    @AppStorage("voice.backendAlerts") private var backendAlerts = false
    @AppStorage("display.mph") private var useMPH = true
    @AppStorage("display.detectionBoxes") private var showDetections = true
    @AppStorage("display.laneGuides") private var showLanes = false
    @AppStorage("display.fullCameraFrame") private var showFullFrame = true
    @AppStorage("navigation.cameraAR") private var cameraAR = true
    @AppStorage("recording.limitGB") private var recordingLimitGB = 0
    @AppStorage("recording.retentionDays") private var recordingRetentionDays = 0
    @AppStorage("checklist.skip") private var skipChecklist = false
    @AppStorage("diagnostics.developerOverlays") private var developerOverlays = false
    @State private var pendingRetentionDays = 0
    @State private var confirmRecordingCleanup = false
    @State private var confirmRouteDelete = false

    private var groups: [(String, [SettingsCategory])] {
        [("Your app", [.appearance, .camera, .recordings]), ("Connected services", [.connection, .voice]),
         ("Safety", [.safety, .permissions, .diagnostics]), ("Explore", [.demo, .about])]
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
                                                .foregroundStyle(selectedCategory == category ? BrakeTheme.accentText : BrakeTheme.text)
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
        .onReceive(model.$tripActive.removeDuplicates()) { tripActive = $0 }
        .onReceive(model.$stopping.removeDuplicates()) { stopping = $0 }
        .onReceive(model.$demo.removeDuplicates()) { demoMode = $0 }
        .onReceive(model.$impactEnabled.removeDuplicates()) { impactEnabled = $0 }
        .onReceive(model.$recordingStorageBytes.removeDuplicates()) { storageBytes = $0 }
        .onAppear {
            if AccentChoice(rawValue: accentChoice) == nil { accentChoice = BrakeTheme.defaultAccent }
        }
        .onChange(of: settings.language) { _, _ in
            selectedVoice = ""
            refreshVoiceOptions()
            Task { await service.prepareVoice() }
        }
        .onChange(of: settings.voiceEnabled) { _, _ in Task { await service.prepareVoice() } }
        .onChange(of: backendAlerts) { _, _ in Task { await service.prepareVoice() } }
        .tint(BrakeTheme.accentColor(named: accentChoice))
    }

    private var hubIdentity: some View {
        HStack(spacing: 14) {
            Image(systemName: "gearshape.fill").font(.brake(size: 25))
                .foregroundStyle(BrakeTheme.accentText).frame(width: 52, height: 52)
                .background(BrakeTheme.accent.opacity(0.12), in: RoundedRectangle(cornerRadius: 16))
            VStack(alignment: .leading, spacing: 5) {
                Text("Make BrakeSense yours.").font(.brake(size: 23, weight: .semibold))
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
            Image(systemName: category.icon).font(.brake(size: 19)).foregroundStyle(BrakeTheme.accentText)
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
        .onAppear {
            voicePageVisible = category == .voice
            if voicePageVisible { voiceLanguage = nil; refreshVoiceOptions() }
            if category == .connection {
                backendAddressDraft = settings.address
                backendKeyDraft = ""
            }
        }
        .onDisappear { if category == .voice { voicePageVisible = false } }
        .id(category)
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
            Text("Color theme").font(.brake(.headline))
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 80))], spacing: 12) {
                ForEach(AccentChoice.allCases) { choice in
                    Button { accentChoice = choice.rawValue } label: {
                        VStack(spacing: 8) {
                            Circle().fill(BrakeTheme.accentColor(named: choice.rawValue))
                                .frame(width: 34, height: 34)
                                .overlay {
                                    if accentChoice == choice.rawValue {
                                        Image(systemName: "checkmark").font(.brake(.caption, weight: .bold))
                                            .foregroundStyle(choice == .polar ? Color(white: 0.5) : .white)
                                    }
                                }
                            Text(choice.title).font(.brake(.caption)).foregroundStyle(BrakeTheme.text)
                        }.frame(maxWidth: .infinity, minHeight: 72)
                    }.buttonStyle(.plain)
                        .accessibilityLabel("\(choice.title) theme")
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
            Toggle("Show lane guides", isOn: $showLanes)
            Toggle("Show full camera frame", isOn: $showFullFrame)
            Toggle("Directions on the road", isOn: $cameraAR)
            CameraCalibrationControls(model: model, presentation: arPresentation, cameraAR: $cameraAR, openCameraSetup: openCameraSetup)
            Text("These preferences also apply to replayed drives.").foregroundStyle(BrakeTheme.muted)
        case .recordings:
            LabeledContent("Video space used", value: storageBytes.map {
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
            Text("Trip maps and flagged moments stay on this iPhone. You can remove their GPS trail while keeping the trip totals.")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            Button("Delete saved trip routes", role: .destructive) { confirmRouteDelete = true }
                .disabled(tripActive)
                .alert("Delete saved trip routes?", isPresented: $confirmRouteDelete) {
                    Button("Delete", role: .destructive) { TripRoute.deleteAll() }
                    Button("Cancel", role: .cancel) { }
                } message: {
                    Text("Trip pages will keep their totals and clips, but their maps and flagged moments will be removed.")
                }
        case .connection:
            Button(pairing ? "Pairing…" : "Scan pairing QR") { showingPairingScanner = true }
                .disabled(pairing || busy)
            if settings.isPaired {
                Label("Paired · \(settings.address)", systemImage: "checkmark.shield.fill")
                    .foregroundStyle(BrakeTheme.muted)
            } else {
                Text("Scan the pairing code from your BrakeSense service, or enter its address below.")
                    .foregroundStyle(BrakeTheme.muted)
            }
            Button(busy ? "Connecting…" : "Test saved connection") {
                busy = true
                Task {
                    await service.connect()
                    backendAddressDraft = settings.address
                    busy = false
                }
            }.disabled(busy || pairing)
            Label(service.connectionStatus, systemImage: "network")
            Text(service.deliveryServiceStatus)
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            if let error = service.error { Text(error).foregroundStyle(BrakeTheme.warning) }
            DisclosureGroup("Manual connection") {
                VStack(alignment: .leading, spacing: 16) {
                    TextField("Backend URL", text: $backendAddressDraft)
                        .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                    if settings.isPaired {
                        Text("This phone uses its paired credential. Change the address only if your service moved, or scan a new pairing code to switch services.")
                            .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                    } else {
                        SecureField("Backend app key", text: $backendKeyDraft)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                        Text("Leave the key blank to keep the saved key.")
                            .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                    }
                    Button(busy ? "Connecting…" : settings.isPaired ? "Save address and test" : "Save and test connection") {
                        busy = true
                        Task {
                            do {
                                settings.address = backendAddressDraft.trimmingCharacters(in: .whitespacesAndNewlines)
                                if !settings.isPaired {
                                    if !backendKeyDraft.isEmpty {
                                        guard backendKeyDraft.count >= 24 else {
                                            throw APIError.message("Enter a valid backend key or leave this blank to keep the saved key.")
                                        }
                                        settings.appKey = backendKeyDraft
                                    }
                                    try settings.saveKey()
                                    backendKeyDraft = ""
                                }
                                await service.connect()
                                backendAddressDraft = settings.address
                            }
                            catch { service.error = error.localizedDescription }
                            busy = false
                        }
                    }.disabled(busy || pairing)
                    if !settings.isPaired {
                        Text("Keep your private app key out of screenshots and messages.")
                            .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                    }
                }.padding(.top, 14)
            }
        case .voice:
            Toggle("Spoken alerts and crash check-in", isOn: $settings.voiceEnabled)
            Picker("Device voice", selection: $selectedVoice) {
                Text("Original BrakeSense voice").tag("")
                ForEach(voiceOptions, id: \.identifier) { voice in
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
                .disabled(tripActive || service.checkinPresented)
            Text("Device voice handles directions, brake warnings, and check-in prompts. Preview while parked. Brake warnings remain clear and at full volume.")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            Toggle("Optional ElevenLabs brake alerts", isOn: $backendAlerts)
            Text("ElevenLabs brake audio requires a connected service. If it is unavailable, BrakeSense uses the device voice.")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            Picker("Language", selection: $settings.language) {
                Text("English").tag("en")
                Text("Español").tag("es")
            }
            Button(service.preparing ? "Preparing…" : "Prepare ElevenLabs brake alert") {
                Task { await service.prepareVoice() }
            }.disabled(service.preparing || settings.client == nil || !backendAlerts)
            Button("Allow spoken check-in replies") {
                Task { await service.requestMicrophone() }
            }
            Text(service.voiceStatus).foregroundStyle(BrakeTheme.muted)
            Text("Check-in replies are recognized on this iPhone when on-device speech is supported. The I'm okay and Send help buttons remain available. Automatic contact alerts require a connected service.")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
        case .safety:
            Toggle("Pre-drive checklist before each trip", isOn: Binding(
                get: { !skipChecklist }, set: { skipChecklist = !$0 }))
            Toggle("Experimental impact detection", isOn: Binding(get: { impactEnabled }, set: { model.impactEnabled = $0 })).disabled(demoMode)
            Toggle("Start an emergency check-in on impact", isOn: $settings.autoCheckin)
                .disabled(!impactEnabled || settings.client == nil)
            Text(service.deliveryServiceStatus)
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            Text("An enabled check-in can message confirmed contacts if you ask for help or do not respond. A phone drop can trigger it. No emergency services are called automatically.")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
        case .permissions:
            Button("Open iPhone settings") {
                if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
            }
            Text("Keep BrakeSense visible for camera recording. Video pauses when the phone locks or the app goes into the background.")
                .foregroundStyle(BrakeTheme.muted)
        case .diagnostics:
            NavigationLink {
                MeasurementDiagnosticsView(model: model)
            } label: {
                Label("Open live diagnostics", systemImage: "waveform.path.ecg")
            }
            Text("Detailed detector, camera, GPS, and warning measurements are available here while testing.")
                .foregroundStyle(BrakeTheme.muted)
            Toggle("Developer overlays on the camera", isOn: $developerOverlays)
            Text("Shows the road-model mask and the build number on the camera page, for test recordings.")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
        case .demo:
            Toggle("Simulated driving metrics", isOn: Binding(get: { demoMode }, set: { model.setDemo($0) }))
                .disabled(tripActive || stopping)
            Button("Run crash check-in demo") { requestDemo = true }
            Text("Try the on-device check-in. The demo never sends emergency messages.")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
        case .about:
            Text("A driver aid, not a safety system").font(.brake(.headline))
            Text("BrakeSense estimates the gap and closing speed to vehicles ahead. Warnings and camera directions can be late, missed, or wrong, especially with poor visibility or a loose mount. Set up the phone while parked and keep your eyes on the road.")
                .foregroundStyle(BrakeTheme.muted)
            Text("Model and font notices").font(.brake(.headline))
            ForEach(Self.noticeFiles, id: \.name) { notice in
                if let body = Self.bundledText(folder: notice.folder, name: notice.name) {
                    DisclosureGroup(notice.title) {
                        Text(body).font(.caption.monospaced()).foregroundStyle(BrakeTheme.muted)
                            .textSelection(.enabled).padding(.top, 8)
                    }
                }
            }
            Link("Terms of Use", destination: LegalLinks.termsOfUse)
            if let privacy = LegalLinks.privacyPolicy {
                Link("Privacy Policy", destination: privacy)
            }
            Text(Self.versionText).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
        }
    }

    private struct NoticeFile {
        let title: String
        let folder: String
        let name: String
    }

    private static let noticeFiles = [
        NoticeFile(title: "Vehicle detectors", folder: "ModelLicenses", name: "NOTICE.md"),
        NoticeFile(title: "UFLD lane detection", folder: "ModelLicenses", name: "UFLD-MIT.txt"),
        NoticeFile(title: "Intel road segmentation", folder: "ModelLicenses", name: "Apache-2.0.txt"),
        NoticeFile(title: "Vehicle orientation", folder: "ViewpointModelData", name: "CFV-LICENSE.txt"),
        NoticeFile(title: "Red Hat Display font", folder: "Fonts", name: "OFL.txt")
    ]

    private static func bundledText(folder: String, name: String) -> String? {
        for bundle in [Bundle.module, Bundle.main] {
            guard let directory = bundle.url(forResource: folder, withExtension: nil) else { continue }
            if let text = try? String(contentsOf: directory.appendingPathComponent(name), encoding: .utf8) {
                return text
            }
        }
        return nil
    }

    private static var versionText: String {
        let info = Bundle.main.infoDictionary
        let version = info?["CFBundleShortVersionString"] as? String ?? "?"
        let build = info?["CFBundleVersion"] as? String ?? "?"
        return "BrakeSense \(version) (\(build))"
    }

    private func beginPairing(_ qrText: String) {
        guard !pairing else { return }
        pairing = true; service.error = nil
        Task {
            defer { pairing = false }
            do {
                try await settings.pair(qrString: qrText, deviceName: UIDevice.current.name)
                backendAddressDraft = settings.address
                backendKeyDraft = ""
                await service.connect()
            } catch { service.error = error.localizedDescription }
        }
    }
    private func refreshVoiceOptions() {
        guard voicePageVisible, voiceLanguage != settings.language else { return }
        voiceLanguage = settings.language
        voiceOptions = SpeechVoicePreferences.availableVoices(language: settings.language)
    }
}
