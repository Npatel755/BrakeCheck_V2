import SwiftUI

/// Trip controls and preferences live here so the camera can stay focused on the road.
struct HomeView: View {
    @AppStorage("display.accent") private var accentChoice = "blue"
    @ObservedObject var model: DriveModel
    @ObservedObject var service: SafetyService
    @Binding var useMPH: Bool
    @Binding var showDetections: Bool
    @Binding var showLanes: Bool
    @Binding var showFullFrame: Bool

    var openCamera: () -> Void
    var openNavigation: () -> Void
    var tripAction: () -> Void
    var openSettings: () -> Void
    var openContacts: () -> Void
    var openIncidents: () -> Void
    var openTripHistory: () -> Void
    var openDiagnostics: () -> Void
    var openReplay: () -> Void
    var beginCalibration: () -> Void

    private var hasTripSummary: Bool {
        model.tripActive || model.tripMinutes > 0 || model.maxSpeedMPH > 0
    }

    @AppStorage("crashText.hasContacts") private var hasCrashTextContacts = false
    @AppStorage("crashText.promptDismissed") private var crashTextPromptDismissed = false

    /// Offered after the first finished trip instead of during first-launch onboarding.
    private var showsCrashTextPrompt: Bool {
        !AppScope.connectedServices && !model.savedTrips.isEmpty && !model.tripActive
            && !hasCrashTextContacts && !crashTextPromptDismissed
    }

    private var calibrationAvailable: Bool {
        model.cameraReady && !model.replaying && !model.demo && !model.stopping
    }

    var body: some View {
        GeometryReader { geometry in
            let wide = geometry.size.width >= 760
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    header
                    sessionCard
                    if hasTripSummary { tripSummary }
                    if showsCrashTextPrompt {
                        CrashTextPromptCard(setUp: openContacts, dismiss: { crashTextPromptDismissed = true })
                    }
                    if wide {
                        HStack(alignment: .top, spacing: 20) {
                            navigationCard.frame(maxWidth: .infinity)
                            displayCard.frame(maxWidth: .infinity)
                        }
                    } else {
                        navigationCard
                        displayCard
                    }
                    Text("Swipe between pages or use the bottom tabs. On Navigation, swipe the search panel; drag the map to explore.")
                        .font(.brake(.footnote))
                        .foregroundStyle(BrakeTheme.muted)
                        .frame(maxWidth: .infinity)
                        .padding(.top, 2)
                }
                .padding(.horizontal, wide ? 28 : 20)
                .padding(.top, 20)
                .padding(.bottom, 24)
                .frame(maxWidth: 1040)
                .frame(maxWidth: .infinity)
            }
            .scrollIndicators(.hidden)
        }
        .background(BrakeTheme.background)
        .foregroundStyle(BrakeTheme.text)
        .tint(BrakeTheme.accentColor(named: accentChoice))
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                BrakeLogo(size: 25)
                Spacer(minLength: 8)
                Text("HOME")
                    .font(.brake(size: 10, weight: .bold))
                    .tracking(1.6)
                    .foregroundStyle(BrakeTheme.muted)
            }
            VStack(alignment: .leading, spacing: 6) {
                Text("Your drive, at a glance.")
                    .font(.brake(size: 28, weight: .semibold))
                    .tracking(-0.7)
                    .fixedSize(horizontal: false, vertical: true)
                Text("Trip controls, recordings, and preferences.")
                    .font(.brake(.subheadline))
                    .foregroundStyle(BrakeTheme.muted)
            }
        }
    }

    private var sessionCard: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack(alignment: .top, spacing: 14) {
                Image(systemName: model.replaying ? "play.rectangle.fill" : "car.front.waves.up.fill")
                    .font(.brake(size: 25, weight: .regular))
                    .foregroundStyle(BrakeTheme.amber)
                    .frame(width: 48, height: 48)
                    .background(BrakeTheme.amber.opacity(0.1), in: RoundedRectangle(cornerRadius: 14))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 7) {
                    statusLabel
                    Text(sessionTitle)
                        .font(.brake(size: 20, weight: .semibold))
                        .fixedSize(horizontal: false, vertical: true)
                    Text(sessionDetail)
                        .font(.brake(.subheadline))
                        .foregroundStyle(BrakeTheme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }

            Button(action: tripAction) {
                Label(primaryTitle, systemImage: model.tripActive || model.replaying ? "stop.fill" : "play.fill")
                    .font(.brake(size: 16, weight: .bold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 15)
                    .foregroundStyle(model.tripActive ? Color.white : BrakeTheme.background)
                    .background(primaryColor, in: Capsule())
            }
            .buttonStyle(.plain)
            .disabled(model.demo || model.stopping)
            .opacity(model.demo || model.stopping ? 0.45 : 1)

            HStack(spacing: 16) {
                Button(action: openCamera) {
                    Label(model.replaying ? "View replay" : "Open camera", systemImage: "video")
                        .font(.brake(.subheadline, weight: .semibold))
                        .frame(minHeight: 44)
                }
                .buttonStyle(.plain)
                .foregroundStyle(BrakeTheme.accent)
                Spacer(minLength: 0)
                if model.tripActive && !model.demo && !model.replaying {
                    Button { model.markIncident() } label: {
                        Label("Save clip", systemImage: "bookmark.fill")
                            .font(.brake(.subheadline, weight: .semibold))
                            .frame(minHeight: 44)
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(BrakeTheme.amber)
                    .disabled(model.stopping)
                    .accessibilityHint("Keeps available footage from 15 minutes before and after this moment.")
                }
            }
        }
        .brakePanel()
    }

    private var statusLabel: some View {
        HStack(spacing: 6) {
            Circle().fill(statusColor).frame(width: 6, height: 6)
            Text(statusText)
                .font(.brake(size: 10, weight: .bold))
                .tracking(1.1)
                .foregroundStyle(statusColor)
        }
        .accessibilityElement(children: .combine)
    }

    private var statusText: String {
        if model.demo { return "SIMULATED" }
        if model.replaying { return "REPLAY" }
        if model.stopping { return "SAVING" }
        if model.tripActive { return model.cameraReady ? "LIVE" : "STARTING" }
        return "STANDBY"
    }

    private var statusColor: Color {
        if model.demo || model.replaying { return BrakeTheme.warning }
        if model.tripActive && model.cameraReady { return BrakeTheme.accent }
        return BrakeTheme.muted
    }

    private var sessionTitle: String {
        if model.demo { return "Explore a simulated drive." }
        if model.replaying { return "Your recording is playing." }
        if model.stopping { return "Saving your trip." }
        if model.tripActive { return model.cameraReady ? "Your trip is underway." : "Getting the camera ready." }
        return "A clearer view ahead."
    }

    private var sessionDetail: String {
        if model.demo { return "Practice with sample readings on the Camera page." }
        if model.replaying { return "See vehicle tracking and estimated risk on the Camera page." }
        if model.stopping { return "Finishing the recording before your next drive." }
        if model.tripActive { return "Keep BrakeCheck on screen for camera tracking and recording. Your road view is on the Camera page." }
        return "Start a trip when you are ready. Camera tracking and recording work while BrakeCheck is on screen."
    }

    private var primaryTitle: String {
        if model.replaying { return "Stop replay" }
        if model.stopping { return "Saving trip…" }
        return model.tripActive ? "End trip" : "Start trip"
    }

    private var primaryColor: Color {
        if model.replaying { return BrakeTheme.warning }
        return model.tripActive ? BrakeTheme.danger : BrakeTheme.accent
    }

    private var tripSummary: some View {
        VStack(alignment: .leading, spacing: 16) {
            sectionTitle(model.tripActive ? "This trip" : "Last trip", icon: "chart.bar.xaxis")
            LazyVGrid(columns: [GridItem(.flexible(), alignment: .leading), GridItem(.flexible(), alignment: .leading)],
                      alignment: .leading, spacing: 18) {
                summaryValue("Duration", value: String(format: "%.1f", model.tripMinutes), unit: "min")
                summaryValue("Max. GPS speed", value: String(format: "%.0f", model.maxSpeedMPH * (useMPH ? 1 : 1.609344)), unit: useMPH ? "mph" : "km/h")
                summaryValue("Est. lead hard brakes", value: "\(model.hardBrakes)")
                summaryValue("Est. closing time below 2 s", value: "\(model.closeCalls)")
            }
        }
        .brakePanel()
    }

    private func summaryValue(_ title: String, value: String, unit: String? = nil) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text(value).font(.brake(size: 26, weight: .semibold)).monospacedDigit()
                if let unit { Text(unit).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted) }
            }
            Text(title).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
        }
        .accessibilityElement(children: .combine)
    }

    private var navigationCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionTitle("Your essentials", icon: "square.grid.2x2")
                .padding(.bottom, 6)
            navigationRow("Trip history", detail: model.savedTrips.isEmpty ? "Completed trips save automatically" : "\(model.savedTrips.count) saved trips · view and export metrics", icon: "chart.xyaxis.line", action: openTripHistory)
            rowDivider
            navigationRow("Navigation", detail: "Directions with camera tracking still active", icon: "location.north.line", action: openNavigation)
            rowDivider
            navigationRow("Settings", detail: AppScope.connectedServices ? "Connection, voice, safety, and permissions" : "Voice, recordings, safety, and permissions", icon: "slider.horizontal.3", action: openSettings)
            rowDivider
            if AppScope.connectedServices {
                navigationRow("Emergency contacts", detail: service.contacts.isEmpty ? "Choose who can receive a check-in" : "\(service.contacts.count) saved contact\(service.contacts.count == 1 ? "" : "s")", icon: "person.2", action: openContacts)
                rowDivider
            } else {
                // Phone-only builds: openContacts leads to Crash texts (CrashTextSetupView).
                navigationRow("Crash texts", detail: hasCrashTextContacts ? "On · text your people if you crash" : "Text someone if you crash", icon: "message.badge.waveform", action: openContacts)
                rowDivider
            }
            navigationRow("Saved incidents", detail: model.incidents.isEmpty ? "Your saved clips and event details" : "\(model.incidents.count) saved incident\(model.incidents.count == 1 ? "" : "s")", icon: "film.stack", action: openIncidents)
            rowDivider
            navigationRow("Replay a drive", detail: "Review a recorded journey", icon: "play.rectangle", action: openReplay)
            rowDivider
            if AppScope.developerTools {
                navigationRow("Diagnostics", detail: "Camera, GPS, and measurement details", icon: "waveform.path.ecg", action: openDiagnostics)
                rowDivider
            }
            navigationRow("Calibrate camera", detail: "While parked, enter a measured vehicle gap", icon: "ruler", action: beginCalibration)
                .disabled(!calibrationAvailable).opacity(calibrationAvailable ? 1 : 0.4)
        }
        .brakePanel()
    }

    private var displayCard: some View {
        VStack(alignment: .leading, spacing: 18) {
            sectionTitle("Camera display", icon: "viewfinder")
            VStack(alignment: .leading, spacing: 8) {
                Text("Units").font(.brake(.subheadline, weight: .medium))
                Picker("Units", selection: $useMPH) {
                    Text("mph · ft").tag(true)
                    Text("km/h · m").tag(false)
                }
                .pickerStyle(.segmented)
            }
            Toggle("Show vehicle boxes", isOn: $showDetections)
            Toggle("Show lane and road overlay", isOn: $showLanes)
            Toggle("Show full camera frame", isOn: $showFullFrame)
            Text("These preferences also apply to replayed drives.")
                .font(.brake(.caption))
                .foregroundStyle(BrakeTheme.muted)
        }
        .font(.brake(.subheadline))
        .brakePanel()
    }

    private func sectionTitle(_ title: String, icon: String) -> some View {
        Label(title, systemImage: icon)
            .font(.brake(size: 16, weight: .semibold))
            .foregroundStyle(BrakeTheme.text)
    }

    private var rowDivider: some View {
        Rectangle().fill(BrakeTheme.text.opacity(0.06)).frame(height: 1).accessibilityHidden(true)
    }

    private func navigationRow(_ title: String, detail: String, icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 14) {
                Image(systemName: icon)
                    .font(.brake(size: 18, weight: .medium))
                    .foregroundStyle(BrakeTheme.accent)
                    .frame(width: 28)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 4) {
                    Text(title).font(.brake(.subheadline, weight: .semibold)).foregroundStyle(BrakeTheme.text)
                    Text(detail).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.brake(size: 11, weight: .semibold))
                    .foregroundStyle(BrakeTheme.muted)
                    .accessibilityHidden(true)
            }
            .padding(.vertical, 9)
            .frame(minHeight: 52)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
    }
}
