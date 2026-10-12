import SwiftUI

/// Trip controls and preferences live here so the camera can stay focused on the road.
struct HomeView: View {
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
    var openDiagnostics: () -> Void
    var openReplay: () -> Void
    var beginCalibration: () -> Void

    private var hasTripSummary: Bool {
        model.tripActive || model.tripMinutes > 0 || model.maxSpeedMPH > 0
    }

    private var targetControlsAvailable: Bool {
        (model.cameraReady || model.replaying) && !model.demo && !model.stopping
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
                    if wide {
                        HStack(alignment: .top, spacing: 20) {
                            navigationCard.frame(maxWidth: .infinity)
                            VStack(spacing: 20) {
                                displayCard
                                targetCard
                            }
                            .frame(maxWidth: .infinity)
                        }
                    } else {
                        navigationCard
                        displayCard
                        targetCard
                    }
                    Text("Swipe between Home, Camera, and Navigation.")
                        .font(.footnote)
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
        .foregroundStyle(.white)
        .tint(BrakeTheme.accent)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                BrakeLogo(size: 25)
                Spacer(minLength: 8)
                Text("HOME")
                    .font(.system(size: 10, weight: .bold))
                    .tracking(1.6)
                    .foregroundStyle(BrakeTheme.muted)
            }
            VStack(alignment: .leading, spacing: 6) {
                Text("Your drive, at a glance.")
                    .font(.system(size: 28, weight: .semibold))
                    .tracking(-0.7)
                    .fixedSize(horizontal: false, vertical: true)
                Text("Trip controls, recordings, and preferences.")
                    .font(.subheadline)
                    .foregroundStyle(BrakeTheme.muted)
            }
        }
    }

    private var sessionCard: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack(alignment: .top, spacing: 14) {
                Image(systemName: model.replaying ? "play.rectangle.fill" : "car.front.waves.up.fill")
                    .font(.system(size: 25, weight: .regular))
                    .foregroundStyle(BrakeTheme.amber)
                    .frame(width: 48, height: 48)
                    .background(BrakeTheme.amber.opacity(0.1), in: RoundedRectangle(cornerRadius: 14))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 7) {
                    statusLabel
                    Text(sessionTitle)
                        .font(.system(size: 20, weight: .semibold))
                        .fixedSize(horizontal: false, vertical: true)
                    Text(sessionDetail)
                        .font(.subheadline)
                        .foregroundStyle(BrakeTheme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }

            Button(action: tripAction) {
                Label(primaryTitle, systemImage: model.tripActive || model.replaying ? "stop.fill" : "play.fill")
                    .font(.system(size: 16, weight: .bold))
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
                        .font(.subheadline.weight(.semibold))
                        .frame(minHeight: 44)
                }
                .buttonStyle(.plain)
                .foregroundStyle(BrakeTheme.accent)
                Spacer(minLength: 0)
                if model.tripActive && !model.demo && !model.replaying {
                    Button { model.markIncident() } label: {
                        Label("Save clip", systemImage: "bookmark.fill")
                            .font(.subheadline.weight(.semibold))
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
                .font(.system(size: 10, weight: .bold))
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
                Text(value).font(.system(size: 26, weight: .semibold)).monospacedDigit()
                if let unit { Text(unit).font(.caption).foregroundStyle(BrakeTheme.muted) }
            }
            Text(title).font(.caption).foregroundStyle(BrakeTheme.muted)
        }
        .accessibilityElement(children: .combine)
    }

    private var navigationCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionTitle("Your essentials", icon: "square.grid.2x2")
                .padding(.bottom, 6)
            navigationRow("Navigation", detail: "Directions with camera tracking still active", icon: "location.north.line", action: openNavigation)
            rowDivider
            navigationRow("Settings", detail: "Connection, voice, safety, and permissions", icon: "slider.horizontal.3", action: openSettings)
            rowDivider
            navigationRow("Emergency contacts", detail: service.contacts.isEmpty ? "Choose who can receive a check-in" : "\(service.contacts.count) saved contact\(service.contacts.count == 1 ? "" : "s")", icon: "person.2", action: openContacts)
            rowDivider
            navigationRow("Saved incidents", detail: model.incidents.isEmpty ? "Your saved clips and event details" : "\(model.incidents.count) saved incident\(model.incidents.count == 1 ? "" : "s")", icon: "film.stack", action: openIncidents)
            rowDivider
            navigationRow("Replay a drive", detail: "Review a recorded journey", icon: "play.rectangle", action: openReplay)
            rowDivider
            navigationRow("Diagnostics", detail: "Camera, GPS, and measurement details", icon: "waveform.path.ecg", action: openDiagnostics)
        }
        .brakePanel()
    }

    private var displayCard: some View {
        VStack(alignment: .leading, spacing: 18) {
            sectionTitle("Camera display", icon: "viewfinder")
            VStack(alignment: .leading, spacing: 8) {
                Text("Units").font(.subheadline.weight(.medium))
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
                .font(.caption)
                .foregroundStyle(BrakeTheme.muted)
        }
        .font(.subheadline)
        .brakePanel()
    }

    private var targetCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            sectionTitle("Car ahead", icon: "scope")
            Text("BrakeCheck automatically looks for a vehicle ahead in your lane.")
                .font(.subheadline)
                .foregroundStyle(BrakeTheme.muted)
                .fixedSize(horizontal: false, vertical: true)
            Button { model.clearTarget() } label: {
                Label("Return to auto selection", systemImage: "arrow.triangle.2.circlepath")
                    .font(.subheadline.weight(.semibold))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .frame(minHeight: 44)
            }
            .buttonStyle(.plain)
            .disabled(!targetControlsAvailable)
            .opacity(targetControlsAvailable ? 1 : 0.4)
            rowDivider
            Button(action: beginCalibration) {
                HStack(spacing: 12) {
                    Image(systemName: "ruler")
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Calibrate gap while parked").font(.subheadline.weight(.semibold))
                        Text("Select a vehicle and enter its measured distance.")
                            .font(.caption)
                            .foregroundStyle(BrakeTheme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 0)
                    Image(systemName: "chevron.right").font(.caption.weight(.semibold))
                }
                .frame(minHeight: 44)
            }
            .buttonStyle(.plain)
            .disabled(!calibrationAvailable)
            .opacity(calibrationAvailable ? 1 : 0.4)
            if !calibrationAvailable {
                Text("Calibration is available with the live camera running.")
                    .font(.caption)
                    .foregroundStyle(BrakeTheme.muted)
            }
        }
        .foregroundStyle(BrakeTheme.accent)
        .brakePanel()
    }

    private func sectionTitle(_ title: String, icon: String) -> some View {
        Label(title, systemImage: icon)
            .font(.system(size: 16, weight: .semibold))
            .foregroundStyle(.white)
    }

    private var rowDivider: some View {
        Rectangle().fill(.white.opacity(0.06)).frame(height: 1).accessibilityHidden(true)
    }

    private func navigationRow(_ title: String, detail: String, icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 14) {
                Image(systemName: icon)
                    .font(.system(size: 18, weight: .medium))
                    .foregroundStyle(BrakeTheme.accent)
                    .frame(width: 28)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 4) {
                    Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(.white)
                    Text(detail).font(.caption).foregroundStyle(BrakeTheme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.system(size: 11, weight: .semibold))
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
