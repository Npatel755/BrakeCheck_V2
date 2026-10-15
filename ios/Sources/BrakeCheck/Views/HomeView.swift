import BrakeCheckCore
import SwiftUI

/// Home, the widget grid (design pick #11): a big Start trip tile, Navigate and Clips, and the
/// last trip, which expands to a View trip button for the trip page. Start trip opens the
/// pre-drive checklist first unless the driver turned it off. Display toggles live in Settings.
struct HomeView: View {
    @AppStorage("display.accent") private var accentChoice = BrakeTheme.defaultAccent
    @AppStorage("crashText.hasContacts") private var hasCrashTextContacts = false
    @AppStorage("crashText.promptDismissed") private var crashTextPromptDismissed = false
    @AppStorage("checklist.skip") private var skipChecklist = false
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
    var openTrip: (UUID) -> Void = { _ in }

    @State private var lastTripOpen = false
    @State private var showChecklist = false

    private var calibrationAvailable: Bool {
        model.cameraReady && !model.replaying && !model.demo && !model.stopping
    }

    /// Offered after the first finished trip instead of during first-launch onboarding.
    private var showsCrashTextPrompt: Bool {
        !AppScope.connectedServices && !model.savedTrips.isEmpty && !model.tripActive
            && !hasCrashTextContacts && !crashTextPromptDismissed
    }

    var body: some View {
        GeometryReader { geometry in
            let wide = geometry.size.width >= 760
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    header
                    startTile
                    if model.tripActive && !model.demo && !model.replaying { tripControls }
                    HStack(spacing: 12) {
                        smallTile("Navigate", detail: "Directions on camera", icon: "location.north.line.fill",
                                  iconColor: BrakeTheme.accentText, iconBackground: BrakeTheme.accent.opacity(0.15), action: openNavigation)
                        smallTile("Clips", detail: model.incidents.isEmpty ? "None saved yet" : "\(model.incidents.count) saved",
                                  icon: "film.stack", iconColor: BrakeTheme.danger, iconBackground: BrakeTheme.danger.opacity(0.15), action: openIncidents)
                    }
                    lastTripTile
                    if showsCrashTextPrompt {
                        CrashTextPromptCard(setUp: openContacts, dismiss: { crashTextPromptDismissed = true })
                    }
                    moreCard
                }
                .padding(.horizontal, wide ? 28 : 20)
                .padding(.top, 14)
                .padding(.bottom, 24)
                .frame(maxWidth: 640)
                .frame(maxWidth: .infinity)
            }
            .scrollIndicators(.hidden)
        }
        .background(BrakeTheme.background)
        .foregroundStyle(BrakeTheme.text)
        .tint(BrakeTheme.accentColor(named: accentChoice))
        .sheet(isPresented: $showChecklist) {
            PreDriveChecklist(skip: $skipChecklist) {
                showChecklist = false
                tripAction()
            }
            .presentationDragIndicator(.visible)
        }
    }

    // MARK: - Header

    private var header: some View {
        HStack {
            BrakeLogo(size: 22)
            Spacer(minLength: 8)
            Button(action: openSettings) {
                Image(systemName: "gearshape.fill")
                    .font(.brake(size: 19, weight: .semibold))
                    .foregroundStyle(BrakeTheme.text)
                    .frame(width: 44, height: 44)
                    .background(BrakeTheme.panel, in: Circle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Settings")
        }
    }

    // MARK: - Start trip

    private var running: Bool { model.tripActive || model.replaying }
    private var tileFill: Color {
        if model.replaying { return BrakeTheme.warning }
        return model.tripActive ? BrakeTheme.danger : BrakeTheme.accent
    }
    /// Text on the tile: the page background reads well on every accent, white on red/orange.
    private var tileInk: Color { running ? .white : BrakeTheme.background }

    private var statusText: String {
        if model.demo { return "SIMULATED" }
        if model.replaying { return "REPLAY" }
        if model.stopping { return "SAVING" }
        if model.tripActive { return model.cameraReady ? "TRIP RUNNING" : "STARTING" }
        return "READY"
    }

    private var primaryTitle: String {
        if model.replaying { return "Stop replay" }
        if model.stopping { return "Saving trip…" }
        return model.tripActive ? "End trip" : "Start trip"
    }

    private var startDetail: String? {
        if model.demo { return "A simulated drive is running" }
        if model.tripActive { return "\(Int(model.tripMinutes)) min · camera and recording on" }
        return nil
    }

    private var startTile: some View {
        Button(action: startTapped) {
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Text(statusText).font(.brake(size: 13, weight: .bold)).tracking(1)
                    Spacer()
                    Image(systemName: model.tripActive ? "record.circle" : "camera.fill").font(.brake(size: 17, weight: .semibold))
                }
                .opacity(0.85)
                Spacer(minLength: 18)
                HStack(alignment: .bottom, spacing: 12) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(primaryTitle).font(.brake(size: 40, weight: .bold)).lineLimit(1).minimumScaleFactor(0.7)
                        if let startDetail {
                            Text(startDetail).font(.brake(.subheadline, weight: .medium)).opacity(0.85)
                        }
                    }
                    Spacer(minLength: 0)
                    Image(systemName: running ? "stop.fill" : "play.fill")
                        .font(.brake(size: 22, weight: .bold))
                        .foregroundStyle(tileFill)
                        .frame(width: 58, height: 58)
                        .background(tileInk, in: Circle())
                }
            }
            .padding(20)
            .frame(maxWidth: .infinity, minHeight: 160, alignment: .leading)
            .foregroundStyle(tileInk)
            .background(tileFill, in: RoundedRectangle(cornerRadius: 26, style: .continuous))
            .shadow(color: running ? tileFill.opacity(0.3) : BrakeTheme.buttonShadow, radius: 14, y: 8)
        }
        .buttonStyle(.plain)
        .disabled(model.demo || model.stopping)
        .opacity(model.demo || model.stopping ? 0.5 : 1)
        .accessibilityLabel(primaryTitle)
        .accessibilityHint(running ? "" : skipChecklist ? "Starts the trip." : "Opens a short checklist, then starts the trip.")
    }

    private func startTapped() {
        if running || skipChecklist { tripAction() } else { showChecklist = true }
    }

    private var tripControls: some View {
        HStack(spacing: 12) {
            chip("Open camera", icon: "video.fill", action: openCamera)
            chip("Save clip", icon: "bookmark.fill", action: { model.markIncident() })
                .disabled(model.stopping)
                .accessibilityHint("Keeps available footage from 15 minutes before and after this moment.")
        }
    }

    private func chip(_ title: String, icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: icon)
                .font(.brake(.subheadline, weight: .semibold))
                .foregroundStyle(BrakeTheme.accentText)
                .frame(maxWidth: .infinity, minHeight: 48)
                .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        }
        .buttonStyle(.plain)
    }

    // MARK: - Tiles

    private func smallTile(_ title: String, detail: String, icon: String, iconColor: Color, iconBackground: Color,
                           action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 0) {
                Image(systemName: icon)
                    .font(.brake(size: 19, weight: .semibold))
                    .foregroundStyle(iconColor)
                    .frame(width: 44, height: 44)
                    .background(iconBackground, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                Spacer(minLength: 16)
                Text(title).font(.brake(size: 17, weight: .bold))
                Text(detail).font(.brake(.footnote)).foregroundStyle(BrakeTheme.muted).lineLimit(1)
            }
            .padding(18)
            .frame(maxWidth: .infinity, minHeight: 140, alignment: .leading)
            .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 26, style: .continuous))
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
    }

    private var lastTripTile: some View {
        VStack(alignment: .leading, spacing: 14) {
            if model.tripActive {
                Text("This trip").font(.brake(size: 16, weight: .bold))
                stats(minutes: model.tripMinutes, maxMPH: model.maxSpeedMPH, hardBrakes: model.hardBrakes, closeCalls: model.closeCalls)
            } else if let trip = model.savedTrips.first {
                Button {
                    withAnimation(.easeOut(duration: 0.2)) { lastTripOpen.toggle() }
                } label: {
                    VStack(alignment: .leading, spacing: 14) {
                        HStack(spacing: 6) {
                            Text("Last trip").font(.brake(size: 16, weight: .bold))
                            Spacer()
                            Text(when(trip.startedAt)).font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
                            Image(systemName: "chevron.right")
                                .font(.brake(size: 13, weight: .bold))
                                .foregroundStyle(BrakeTheme.muted)
                                .rotationEffect(.degrees(lastTripOpen ? 90 : 0))
                        }
                        stats(minutes: trip.durationSeconds / 60, maxMPH: trip.maxSpeedMPH,
                              hardBrakes: trip.estimatedLeadHardBrakes, closeCalls: trip.estimatedCloseCalls)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityValue(lastTripOpen ? "Expanded" : "Collapsed")
                if lastTripOpen {
                    Button { openTrip(trip.id) } label: {
                        HStack(spacing: 6) {
                            Text("View trip")
                            Image(systemName: "chevron.right")
                        }
                        .font(.brake(size: 15, weight: .bold))
                        .foregroundStyle(BrakeTheme.accentText)
                        .frame(maxWidth: .infinity, minHeight: 46)
                        .background(BrakeTheme.accent.opacity(0.15), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                    }
                    .buttonStyle(.plain)
                    .transition(.opacity.combined(with: .move(edge: .top)))
                }
            } else {
                Text("Last trip").font(.brake(size: 16, weight: .bold))
                Text("Your first drive shows up here after you tap End trip.")
                    .font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 26, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 26, style: .continuous)
            .stroke(lastTripOpen && !model.tripActive ? BrakeTheme.accent : .clear, lineWidth: 1.5))
    }

    private func stats(minutes: Double, maxMPH: Double?, hardBrakes: Int, closeCalls: Int) -> some View {
        HStack(alignment: .top, spacing: 8) {
            stat("\(Int(minutes.rounded()))", "min", BrakeTheme.text)
            stat(maxMPH.map { String(format: "%.0f", $0 * (useMPH ? 1 : 1.609344)) } ?? "--", useMPH ? "max mph" : "max km/h", BrakeTheme.text)
            stat("\(hardBrakes)", hardBrakes == 1 ? "hard brake" : "hard brakes", BrakeTheme.warning)
            stat("\(closeCalls)", closeCalls == 1 ? "close call" : "close calls", BrakeTheme.danger)
        }
        .accessibilityElement(children: .combine)
    }

    private func stat(_ value: String, _ label: String, _ color: Color) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value).font(.brake(size: 28, weight: .bold)).monospacedDigit().foregroundStyle(color)
            Text(label).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func when(_ date: Date) -> String {
        let time = date.formatted(date: .omitted, time: .shortened)
        if Calendar.current.isDateInToday(date) { return "Today \(time)" }
        if Calendar.current.isDateInYesterday(date) { return "Yesterday \(time)" }
        return "\(date.formatted(.dateTime.weekday(.abbreviated))) \(time)"
    }

    // MARK: - More

    private var moreCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            navigationRow("Trip history", detail: model.savedTrips.isEmpty ? "Completed trips save automatically" : "\(model.savedTrips.count) saved trips", icon: "chart.xyaxis.line", action: openTripHistory)
            rowDivider
            if AppScope.connectedServices {
                navigationRow("Emergency contacts", detail: service.contacts.isEmpty ? "Choose who can receive a check-in" : "\(service.contacts.count) saved contact\(service.contacts.count == 1 ? "" : "s")", icon: "person.2", action: openContacts)
            } else {
                navigationRow("Crash texts", detail: hasCrashTextContacts ? "On · text your people if you crash" : "Text someone if you crash", icon: "message.badge.waveform", action: openContacts)
            }
            rowDivider
            navigationRow("Replay a drive", detail: "Review a recorded journey", icon: "play.rectangle", action: openReplay)
            rowDivider
            navigationRow("Calibrate camera", detail: "While parked, enter a measured vehicle gap", icon: "ruler", action: beginCalibration)
                .disabled(!calibrationAvailable).opacity(calibrationAvailable ? 1 : 0.4)
            if AppScope.developerTools {
                rowDivider
                navigationRow("Diagnostics", detail: "Camera, GPS, and measurement details", icon: "waveform.path.ecg", action: openDiagnostics)
            }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 4)
        .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 26, style: .continuous))
    }

    private var rowDivider: some View {
        Rectangle().fill(BrakeTheme.text.opacity(0.06)).frame(height: 1).accessibilityHidden(true)
    }

    private func navigationRow(_ title: String, detail: String, icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 14) {
                Image(systemName: icon)
                    .font(.brake(size: 18, weight: .medium))
                    .foregroundStyle(BrakeTheme.accentText)
                    .frame(width: 28)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 3) {
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
            .padding(.vertical, 10)
            .frame(minHeight: 56)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
    }
}
