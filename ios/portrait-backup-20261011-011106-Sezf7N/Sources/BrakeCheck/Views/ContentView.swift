import BrakeCheckCore
import SwiftUI
import UIKit

/// Full-screen dashcam view with the controls and readouts floating over the road, like a camera app or a car HUD.
///
/// Two layouts, picked by the screen's shape: horizontal (the shipping one) and vertical. The vertical layout is
/// UI only for now. Info.plist still allows landscape right only, and the camera, Vision and distance code
/// (CameraService, CameraPreview, VehicleTracker) assume landscape frames. To ship vertical: make those
/// orientation-aware, then add UIInterfaceOrientationPortrait to Info.plist.
struct ContentView: View {
    @ObservedObject var model: DriveModel
    @ObservedObject private var safety: SafetyService
    @AppStorage("display.mph") private var useMPH = true
    @AppStorage("display.detectionBoxes") private var showDetections = true
    @AppStorage("display.lanes") private var showLanes = true
    @AppStorage("display.fullCameraFrame") private var showFullFrame = true
    @State private var selecting = false
    @State private var selection: CGRect?
    @State private var measuredDistance = ""
    @State private var showingIncidents = false
    @State private var showingSettings = false
    @State private var showingContacts = false
    @State private var stopping = false
    @State private var showingDiagnostics = false
    @State private var showingReplay = false
    @State private var requestDemo = false
    @State private var showCheckin = false

    init(model: DriveModel) {
        self.model = model
        _safety = ObservedObject(wrappedValue: model.safety)
    }
    private var factor: Double { useMPH ? 2.2369362921 : 3.6 }
    private var unit: String { useMPH ? "mph" : "km/h" }
    private var calibration: Double? { Double(measuredDistance.replacingOccurrences(of: ",", with: ".")) }
    /// Same condition as the "Brake" voice and haptic in DriveModel.updateTripEvents (demo included for rehearsal).
    private var critical: Bool { model.risk.level == .critical && (model.tripActive || model.replaying || model.demo) }
    private var running: Bool { model.tripActive || model.replaying || model.demo }
    private var showsPlaceholder: Bool { (!model.cameraReady && !model.replaying) || model.demo }
    private var searching: Bool { (model.cameraReady || model.replaying) && !model.demo && model.risk.distance == nil }

    var body: some View {
        GeometryReader { geometry in
            let portrait = geometry.size.height > geometry.size.width
            ZStack {
                roadView(portrait: portrait).ignoresSafeArea()
                if showsPlaceholder { placeholder }
                scrims(portrait: portrait).ignoresSafeArea().allowsHitTesting(false)
                if portrait { portraitHUD } else { landscapeHUD }
                if critical { BrakeEdgeGlow() }
            }
        }
        .background(BrakeTheme.background.ignoresSafeArea())
        .animation(.easeOut(duration: 0.15), value: critical)
        .tint(BrakeTheme.accent)
        .statusBarHidden()
        .persistentSystemOverlays(.hidden)
        .sheet(isPresented: $showingIncidents, onDismiss: presentPendingCheckin) { IncidentListView(model: model) }
        .sheet(isPresented: $showingSettings, onDismiss: presentPendingCheckin) { SettingsView(model: model, service: safety, settings: safety.settings, requestDemo: $requestDemo) }
        .sheet(isPresented: $showingContacts, onDismiss: presentPendingCheckin) { ContactsView(service: safety) }
        .sheet(isPresented: $showingDiagnostics, onDismiss: presentPendingCheckin) { DiagnosticsView(model: model) }
        .sheet(isPresented: $showingReplay, onDismiss: presentPendingCheckin) { ReplayView(model: model) }
        .fullScreenCover(isPresented: $showCheckin) { CrashCheckinView(service: safety) }
        .alert("BrakeCheck", isPresented: Binding(get: { model.message != nil }, set: { if !$0 { model.message = nil } })) {
            Button("OK", role: .cancel) { model.message = nil }
        } message: { Text(model.message ?? "") }
        .confirmationDialog("End this trip? Remaining post-event recording will stop.", isPresented: $stopping, titleVisibility: .visible) {
            Button("End trip", role: .destructive) { selecting = false; model.endTrip() }
        }
        .onChange(of: safety.checkinPresented) { _, presented in
            if !presented { showCheckin = false }
            else if showingSettings || showingContacts || showingIncidents || showingDiagnostics || showingReplay {
                showingSettings = false; showingContacts = false; showingIncidents = false; showingDiagnostics = false
                showingReplay = false
            } else { showCheckin = true }
        }
        .onChange(of: model.cameraReady) { _, ready in if !ready { selecting = false; selection = nil } }
        .task { await safety.connectIfPaired() }
    }

    private func presentPendingCheckin() {
        if requestDemo { requestDemo = false; model.simulateCheckin() }
        if safety.checkinPresented { showCheckin = true }
    }

    // MARK: - Road

    /// The camera (or replay frame) and its overlays share one box with the video's shape. The box fills the
    /// screen, fitting the whole camera frame by default; the scope menu also offers a cropped fill view.
    private func roadView(portrait: Bool) -> some View {
        let frameSize = model.replaying ? model.replayFrame?.size : nil
        let aspect = frameSize.map { Double($0.width / max($0.height, 1)) } ?? (portrait ? 9.0 / 16 : 16.0 / 9)
        return GeometryReader { geometry in
            let frame = showFullFrame
                ? CameraGeometry.aspectFitRect(in: geometry.size, imageAspect: aspect)
                : CameraGeometry.aspectFillRect(in: geometry.size, imageAspect: aspect)
            ZStack {
                Color.black
                if model.replaying {
                    if let frame = model.replayFrame { Image(uiImage: frame).resizable().scaledToFit() }
                } else {
                    CameraPreview(session: model.camera.session)
                }
                if (model.cameraReady || model.replaying) && !model.demo {
                    if showLanes && !selecting {
                        RoadAreaOverlay(road: model.roadRegion)
                        LeadCorridorOverlay(lane: model.lanePath)
                    }
                    if showDetections && !selecting { DetectionOverlay(detections: model.detections) }
                    TargetOverlay(trackedBox: model.trackingBox, selecting: selecting, selectedBox: $selection,
                                  alert: critical, coasting: model.trackingCoasting)
                }
            }
            .frame(width: frame.width, height: frame.height)
            .position(x: frame.midX, y: frame.midY)
        }
        .clipped()
    }

    private var placeholder: some View {
        ZStack {
            BrakeTheme.panel.ignoresSafeArea()
            VStack(spacing: 10) {
                Image(systemName: model.demo ? "car.side.hill.up.fill" : "car.front.waves.up.fill")
                    .font(.system(size: 44, weight: .light)).foregroundStyle(BrakeTheme.accent.opacity(0.7))
                Text(model.demo ? "Simulated drive" : "A clearer view ahead.")
                    .font(.system(size: 22, weight: .semibold))
                Text(model.demo ? "Practice numbers · no camera" : "Start a trip to see the road and track vehicles.")
                    .font(.system(size: 12)).foregroundStyle(BrakeTheme.muted).multilineTextAlignment(.center)
            }
            .padding(.horizontal, 24)
            .offset(y: -24)
        }
    }

    /// Dark gradients behind the top and bottom bars so white text reads over a bright sky or road.
    private func scrims(portrait: Bool) -> some View {
        VStack(spacing: 0) {
            LinearGradient(colors: [.black.opacity(0.6), .clear], startPoint: .top, endPoint: .bottom).frame(height: portrait ? 190 : 130)
            Spacer(minLength: 0)
            LinearGradient(colors: [.clear, .black.opacity(0.7)], startPoint: .top, endPoint: .bottom).frame(height: portrait ? 260 : 150)
        }
    }

    // MARK: - Layouts

    private var landscapeHUD: some View {
        VStack(spacing: 8) {
            HStack(spacing: 8) {
                brand(size: 25)
                Spacer(minLength: 0)
                saveClipButton(compact: false)
                scopeMenu(size: 34)
                iconButton("waveform.path.ecg", "Sensor diagnostics", size: 34) { showingDiagnostics = true }
                iconButton("person.2", "Emergency contacts", size: 34) { showingContacts = true }
                iconButton("film.stack", "Saved incidents", size: 34) { showingIncidents = true }
                iconButton("play.rectangle", "Replay a recorded drive", size: 34) { showingReplay = true }
                iconButton("slider.horizontal.3", "Settings", size: 34) { showingSettings = true }
                primaryButton(wide: false).padding(.leading, 2)
            }
            .buttonStyle(.plain)
            if running && !selecting {
                RiskPill(risk: model.risk, critical: critical, searching: searching, paused: model.trackingCoasting)
            }
            Spacer(minLength: 0)
            if selecting { calibrationControls.frame(maxWidth: 440) }
            // Standby shows only the welcome; readouts appear once there is something to read.
            if running { bottomRow(portrait: false) }
        }
        .padding(.horizontal, 12).padding(.top, 12).padding(.bottom, 6)
        .foregroundStyle(.white)
    }

    /// Vertical: brand and settings on top, the larger risk pill under them, readouts and a thumb-reach
    /// control bar at the bottom.
    private var portraitHUD: some View {
        VStack(spacing: 14) {
            HStack(spacing: 8) {
                brand(size: 22)
                Spacer(minLength: 0)
                saveClipButton(compact: true)
                iconButton("slider.horizontal.3", "Settings", size: 34) { showingSettings = true }
            }
            .buttonStyle(.plain)
            if running && !selecting {
                RiskPill(risk: model.risk, critical: critical, searching: searching, paused: model.trackingCoasting, large: true)
            }
            Spacer(minLength: 0)
            if selecting { calibrationControls }
            if running { bottomRow(portrait: true) }
            HStack(spacing: 10) {
                scopeMenu(size: 44)
                iconButton("person.2", "Emergency contacts", size: 44) { showingContacts = true }
                iconButton("film.stack", "Saved incidents", size: 44) { showingIncidents = true }
                iconButton("play.rectangle", "Replay a recorded drive", size: 44) { showingReplay = true }
                iconButton("waveform.path.ecg", "Sensor diagnostics", size: 44) { showingDiagnostics = true }
                primaryButton(wide: true)
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 16).padding(.top, 6).padding(.bottom, 8)
        .foregroundStyle(.white)
    }

    // MARK: - Controls

    private func brand(size: CGFloat) -> some View {
        HStack(spacing: 10) {
            BrakeLogo(size: size)
            statusPill
        }
        .fixedSize()
    }

    private var statusPill: some View {
        let (text, color): (String, Color) =
            model.demo ? ("SIMULATED", BrakeTheme.warning)
            : model.replaying ? ("REPLAY", BrakeTheme.warning)
            : model.tripActive ? (model.cameraReady ? "LIVE" : "CAMERA STARTING", model.cameraReady ? BrakeTheme.danger : BrakeTheme.muted)
            : ("STANDBY", BrakeTheme.muted)
        return HStack(spacing: 5) {
            Circle().fill(color).frame(width: 6, height: 6)
            Text(text).font(.system(size: 10, weight: .bold)).tracking(1)
        }
        .padding(.horizontal, 10).frame(height: 26)
        .background(.black.opacity(0.5), in: Capsule())
    }

    /// Icon only in the vertical layout, where the top row is narrow.
    @ViewBuilder private func saveClipButton(compact: Bool) -> some View {
        if model.tripActive && !model.demo {
            Button { model.markIncident() } label: {
                Group {
                    if compact {
                        Image(systemName: "bookmark.fill").font(.system(size: 14, weight: .semibold)).frame(width: 34, height: 34)
                    } else {
                        Label("Save clip", systemImage: "bookmark.fill").font(.system(size: 12, weight: .semibold))
                            .padding(.horizontal, 11).frame(height: 34)
                    }
                }
                .background(.black.opacity(0.5), in: Capsule())
                .foregroundStyle(BrakeTheme.accent)
            }
            .accessibilityLabel("Save clip")
            .accessibilityHint("Keeps available footage from 15 minutes before and after this moment.")
        }
    }

    @ViewBuilder private func scopeMenu(size: CGFloat) -> some View {
        if (model.cameraReady || model.replaying) && !selecting {
            Menu {
                Button("Auto select") { model.clearTarget() }
                if model.cameraReady && !model.replaying {
                    Button("Calibrate gap while parked") {
                        model.clearTarget(); selection = nil; measuredDistance = ""; selecting = true
                    }
                }
                Toggle("Show full camera frame", isOn: $showFullFrame)
                Toggle("Show all vehicle boxes", isOn: $showDetections)
                Toggle("Show lane and road overlay", isOn: $showLanes)
            } label: { hudIcon("scope", size: size) }
            .accessibilityLabel("Lead vehicle selection")
        }
    }

    private func iconButton(_ name: String, _ label: String, size: CGFloat, action: @escaping () -> Void) -> some View {
        Button(action: action) { hudIcon(name, size: size) }.accessibilityLabel(label)
    }

    private func hudIcon(_ name: String, size: CGFloat) -> some View {
        Image(systemName: name).font(.system(size: size > 40 ? 16 : 14, weight: .semibold))
            .frame(width: size, height: size).background(.black.opacity(0.5), in: Circle())
    }

    @ViewBuilder private func primaryButton(wide: Bool) -> some View {
        if model.replaying {
            Button { model.stopReplay() } label: { primaryLabel("Stop replay", fill: BrakeTheme.warning, text: .black, wide: wide) }
        } else {
            Button { if model.tripActive { stopping = true } else { model.startTrip() } } label: {
                primaryLabel(model.stopping ? "Saving…" : model.tripActive ? "End trip" : "Start trip",
                             fill: model.tripActive ? BrakeTheme.danger : BrakeTheme.accent,
                             text: model.tripActive ? .white : BrakeTheme.background, wide: wide)
            }
            .disabled(model.demo || model.stopping)
            .opacity(model.demo || model.stopping ? 0.45 : 1)
        }
    }

    private func primaryLabel(_ title: String, fill: Color, text: Color, wide: Bool) -> some View {
        Text(title).font(.system(size: wide ? 15 : 13, weight: .bold)).foregroundStyle(text).lineLimit(1)
            .padding(.horizontal, 14).frame(maxWidth: wide ? .infinity : nil).frame(height: wide ? 44 : 34)
            .background(fill, in: Capsule())
    }

    // MARK: - Readouts

    /// Speed bottom-left and the car-ahead numbers bottom-right, so the middle of the road (where the lead car is) stays clear.
    private func bottomRow(portrait: Bool) -> some View {
        HStack(alignment: .bottom, spacing: 12) {
            speedReadout(size: portrait ? 58 : 64)
            Spacer(minLength: 0)
            carAheadNumbers(compact: portrait)
        }
    }

    private func speedReadout(size: CGFloat) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("SPEED").font(.system(size: 10, weight: .bold)).tracking(1).foregroundStyle(BrakeTheme.muted)
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text(model.speed.map { String(format: "%.0f", $0 * factor) } ?? "--")
                    .font(.system(size: size, weight: .semibold)).monospacedDigit().tracking(-2)
                    .foregroundStyle(model.speed == nil ? BrakeTheme.muted : .white)
                Button(unit) { useMPH.toggle() }
                    .font(.system(size: 15, weight: .semibold)).foregroundStyle(BrakeTheme.muted)
                    .accessibilityHint("Switches between mph and km/h")
            }
            if model.replaying {
                Text(model.replaySpeedSource.hasPrefix("ASSUMED") ? "ASSUMED SPEED" : "RECORDED SPEED")
                    .font(.system(size: 9, weight: .semibold)).foregroundStyle(BrakeTheme.warning)
            } else if model.speed == nil {
                Text("Waiting for GPS").font(.system(size: 11)).foregroundStyle(BrakeTheme.muted)
            }
        }
        .shadow(color: .black.opacity(0.5), radius: 6)
    }

    /// Numbers float on the video like a head-up display; the bottom scrim and a shadow keep them readable.
    /// Feet and mph when the speed unit is mph, metres and km/h otherwise.
    private func carAheadNumbers(compact: Bool) -> some View {
        let risk = model.risk
        let distance = risk.distance.map { useMPH ? $0 * 3.28084 : $0 }
        let ttc = risk.timeToCollision
        let size: CGFloat = compact ? 21 : 24
        return Grid(alignment: .trailing, horizontalSpacing: compact ? 16 : 22, verticalSpacing: 8) {
            GridRow {
                hudNumber("Distance", distance.map { String(format: "%.0f", $0) }, useMPH ? "ft" : "m", size: size)
                hudNumber("Their speed", risk.leadSpeed.map { String(format: "%.0f", $0 * factor) }, unit, size: size)
            }
            GridRow {
                hudNumber("Closing in", risk.closingSpeed.map { String(format: "%.0f", $0 * factor) }, unit, size: size)
                hudNumber("Time to impact", ttc.map { String(format: "%.1f", $0) }, "s", size: size, alert: (ttc ?? .infinity) < 2)
            }
        }
        .shadow(color: .black.opacity(0.9), radius: 8, y: 1)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Car ahead, estimated")
    }

    private func hudNumber(_ label: String, _ value: String?, _ unit: String, size: CGFloat, alert: Bool = false) -> some View {
        VStack(alignment: .trailing, spacing: 1) {
            Text(label).font(.system(size: 11, weight: .semibold)).foregroundStyle(.white.opacity(0.75))
            Text(value ?? "--").font(.system(size: size, weight: .semibold)).monospacedDigit()
                .foregroundColor(alert ? BrakeTheme.danger : .white)
            + Text(" \(unit)").font(.system(size: 12, weight: .semibold)).foregroundColor(.white.opacity(0.75))
        }
    }

    private var calibrationControls: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("While parked, draw around the rear of a vehicle and enter its measured gap.").font(.caption)
            HStack {
                TextField("Meters", text: $measuredDistance).keyboardType(.decimalPad).textFieldStyle(.roundedBorder)
                Button("Track") {
                    if let box = selection, let distance = calibration { model.selectVehicle(box: box, measuredDistance: distance); selecting = false }
                }.disabled(selection == nil || !(2...80).contains(calibration ?? 0))
                Button("Cancel") { selecting = false; selection = nil }
            }
        }
        .padding(12)
        .background(.black.opacity(0.7), in: RoundedRectangle(cornerRadius: 10))
    }
}
