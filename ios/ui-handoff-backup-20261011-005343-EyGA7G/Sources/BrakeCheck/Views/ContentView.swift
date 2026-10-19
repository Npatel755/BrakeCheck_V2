import BrakeCheckCore
import SwiftUI
import UIKit

struct ContentView: View {
    @ObservedObject var model: DriveModel
    @ObservedObject private var safety: SafetyService
    @AppStorage("display.mph") private var useMPH = true
    @AppStorage("display.detectionBoxes") private var showDetections = false
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
    @State private var requestDemo = false
    @State private var showCheckin = false

    init(model: DriveModel) {
        self.model = model
        _safety = ObservedObject(wrappedValue: model.safety)
    }
    private var factor: Double { useMPH ? 2.2369362921 : 3.6 }
    private var unit: String { useMPH ? "mph" : "km/h" }
    private var distanceFactor: Double { useMPH ? 3.280839895 : 1 }
    private var distanceUnit: String { useMPH ? "ft" : "m" }
    private var amber: Color { Color(red: 1, green: 0.68, blue: 0.18) }
    private var prominentRisk: Bool { model.risk.level == .high || model.risk.level == .critical }
    private var riskTitle: String {
        switch model.risk.level {
        case .unknown: return "Waiting for estimates"
        case .low: return "Lower indication"
        case .elevated: return "Elevated risk"
        case .high: return "High collision risk"
        case .critical:
            return model.risk.timeToCollision.map { String(format: "BRAKE · %.1f s", $0) } ?? "BRAKE"
        }
    }
    private var laneLabel: String {
        if model.demo { return "SIMULATION" }
        switch model.lanePath?.source {
        case .some(.neural): return "YOLOP LANE"
        case .some(.hybrid): return "LANE FUSION"
        case .some(.markings): return "PAINT LANE"
        case .none:
            guard model.roadRegion != nil else { return "LANE UNCERTAIN" }
            if model.rangeSource.hasPrefix("CALIBRATED RANGE") { return "ROAD AREA" }
            return model.trackingBox != nil && !model.trackingCoasting
                && model.rangeSource.hasPrefix("APPROX. RANGE · road area + forward motion")
                ? "ROAD + FORWARD MOTION" : "ROAD AREA · CHECKING DIRECTION"
        }
    }
    private var calibration: Double? { Double(measuredDistance.replacingOccurrences(of: ",", with: ".")) }
    private var riskColor: Color {
        switch model.risk.level {
        case .unknown: return .white.opacity(0.7)
        case .low: return BrakeTheme.accent
        case .elevated: return .yellow
        case .high: return .orange
        case .critical: return .red
        }
    }

    var body: some View {
        GeometryReader { geometry in
            ZStack {
                cameraStage.ignoresSafeArea()
                // Gradients protect legibility without covering the middle of the road.
                VStack {
                    LinearGradient(colors: [.black.opacity(0.65), .clear], startPoint: .top, endPoint: .bottom)
                        .frame(height: 110)
                    Spacer(minLength: 0)
                    LinearGradient(colors: [.clear, .black.opacity(0.72)], startPoint: .top, endPoint: .bottom)
                        .frame(height: 165)
                }.ignoresSafeArea().allowsHitTesting(false)

                VStack(spacing: 8) {
                    header(compact: geometry.size.height > geometry.size.width || geometry.size.width < 650)
                    HStack(spacing: 12) {
                        Label(laneLabel, systemImage: "road.lanes")
                        Spacer(minLength: 0)
                        Text(model.demo ? "GENERATED METRICS" : model.trackingCoasting ? "CANDIDATE · ESTIMATES PAUSED"
                             : model.trackingBox == nil ? "SEARCHING FOR LEAD" : "LEAD TRACKED")
                    }
                    .font(.system(size: 9, weight: .semibold)).tracking(0.5)
                    .foregroundStyle(.white.opacity(0.85))
                    .padding(.horizontal, 10).padding(.vertical, 6)
                    .background(.black.opacity(0.35), in: Capsule())

                    Spacer(minLength: 12)
                    if selecting {
                        calibrationControls.frame(maxWidth: 480)
                    } else if geometry.size.height > geometry.size.width {
                        riskHUD
                        HStack(alignment: .center, spacing: 10) {
                            speedHUD.frame(width: 120)
                            metricPanel(compact: true)
                        }
                    } else {
                        HStack(alignment: .bottom, spacing: 14) {
                            speedHUD.frame(width: geometry.size.width < 700 ? 120 : 155)
                            Spacer(minLength: 0)
                            VStack(spacing: 7) {
                                riskHUD
                                metricPanel(compact: false)
                            }.frame(width: min(440, max(290, geometry.size.width * 0.60)))
                        }
                    }
                    footer
                }
                .padding(.horizontal, 14).padding(.vertical, 8)
            }
            .overlay {
                if model.risk.level == .critical {
                    RoundedRectangle(cornerRadius: 24)
                        .stroke(Color.red.opacity(0.95), lineWidth: 7)
                        .shadow(color: .red.opacity(0.65), radius: 16)
                        .padding(3).ignoresSafeArea().allowsHitTesting(false)
                }
            }
            .animation(.easeInOut(duration: 0.2), value: model.risk.level.rawValue)
            .clipped()
        }
        .background(Color.black.ignoresSafeArea())
        .foregroundStyle(.white)
        .tint(BrakeTheme.accent)
        .sheet(isPresented: $showingIncidents, onDismiss: presentPendingCheckin) { IncidentListView(model: model) }
        .sheet(isPresented: $showingSettings, onDismiss: presentPendingCheckin) {
            SettingsView(model: model, service: safety, settings: safety.settings, requestDemo: $requestDemo)
        }
        .sheet(isPresented: $showingContacts, onDismiss: presentPendingCheckin) { ContactsView(service: safety) }
        .sheet(isPresented: $showingDiagnostics, onDismiss: presentPendingCheckin) { DiagnosticsView(model: model) }
        .fullScreenCover(isPresented: $showCheckin) { CrashCheckinView(service: safety) }
        .alert("BrakeCheck", isPresented: Binding(get: { model.message != nil }, set: { if !$0 { model.message = nil } })) {
            Button("OK", role: .cancel) { model.message = nil }
        } message: { Text(model.message ?? "") }
        .confirmationDialog("End this trip? Remaining post-event recording will stop.", isPresented: $stopping, titleVisibility: .visible) {
            Button("End trip", role: .destructive) { selecting = false; model.endTrip() }
        }
        .onChange(of: safety.checkinPresented) { _, presented in
            if !presented { showCheckin = false }
            else if showingSettings || showingContacts || showingIncidents || showingDiagnostics {
                showingSettings = false; showingContacts = false; showingIncidents = false; showingDiagnostics = false
            } else { showCheckin = true }
        }
        .onChange(of: model.cameraReady) { _, ready in if !ready { selecting = false; selection = nil } }
        .task { await safety.connectIfPaired() }
    }

    private var cameraStage: some View {
        GeometryReader { geometry in
            let frame = showFullFrame ? CameraGeometry.aspectFitRect(in: geometry.size)
                : CameraGeometry.aspectFillRect(in: geometry.size)
            ZStack {
                CameraPreview(session: model.camera.session)
                if !model.cameraReady || model.demo {
                    Color(red: 0.04, green: 0.07, blue: 0.085)
                    VStack(spacing: 8) {
                        Image(systemName: model.demo ? "car.side.hill.up.fill" : "exclamationmark.brakesignal")
                            .font(.system(size: 42, weight: .light)).foregroundStyle(amber)
                        Text(model.demo ? "Simulated drive" : "A clearer view ahead.")
                            .font(.system(size: 22, weight: .medium, design: .rounded))
                        Text(model.demo ? "Generated metrics · no camera or recordings" : "Start a trip to see the road.")
                            .font(.system(size: 12)).foregroundStyle(.white.opacity(0.7))
                    }
                }
                if model.cameraReady && !model.demo {
                    if showLanes && !selecting {
                        RoadAreaOverlay(road: model.roadRegion)
                        LeadCorridorOverlay(lane: model.lanePath)
                    }
                    if showDetections && !selecting { DetectionOverlay(detections: model.detections) }
                    TargetOverlay(trackedBox: model.trackingBox, selecting: selecting,
                                  selectedBox: $selection, coasting: model.trackingCoasting)
                }
            }
            // Preview, boxes, lanes and gestures share one rectangle. Full
            // frame mode fits the entire image without cropping any pixels.
            .frame(width: frame.width, height: frame.height)
            .position(x: frame.midX, y: frame.midY)
        }.clipped()
    }

    private func header(compact: Bool) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.brakesignal")
                .font(.system(size: 23, weight: .semibold)).foregroundStyle(amber)
            Text("brakecheck").font(.system(size: 20, weight: .bold, design: .rounded)).tracking(-0.6)
            Label(model.demo ? "DEMO" : model.tripActive ? "LIVE" : "IDLE", systemImage: "circle.fill")
                .font(.system(size: 8, weight: .bold))
                .foregroundStyle(model.demo ? .orange : model.tripActive ? BrakeTheme.accent : .white.opacity(0.6))
            Spacer(minLength: 4)
            if !compact {
                hudButton("person.2", label: "Emergency contacts") { showingContacts = true }
                hudButton("film.stack", label: "Saved incidents") { showingIncidents = true }
                hudButton("waveform.path.ecg", label: "Sensor diagnostics") { showingDiagnostics = true }
                hudButton("slider.horizontal.3", label: "Settings") { showingSettings = true }
            }
            Menu {
                if compact {
                    Button("Emergency contacts", systemImage: "person.2") { showingContacts = true }
                    Button("Saved incidents", systemImage: "film.stack") { showingIncidents = true }
                    Button("Sensor diagnostics", systemImage: "waveform.path.ecg") { showingDiagnostics = true }
                    Button("Settings", systemImage: "slider.horizontal.3") { showingSettings = true }
                }
                Button("Auto select lead vehicle") { selecting = false; selection = nil; model.clearTarget() }
                Button("Calibrate gap while parked") {
                    model.clearTarget(); selection = nil; measuredDistance = ""; selecting = true
                }.disabled(!model.cameraReady || model.demo)
                Toggle("Show full camera frame", isOn: $showFullFrame)
                Toggle("Show all vehicle boxes", isOn: $showDetections)
                Toggle("Show lane estimate", isOn: $showLanes)
            } label: { Image(systemName: "scope").frame(width: 32, height: 32).background(.black.opacity(0.5), in: Circle()) }
                .accessibilityLabel("Camera and tracking options")
            Button(model.stopping ? "Saving…" : model.tripActive ? "End trip" : "Start trip") {
                if model.tripActive { stopping = true } else { model.startTrip() }
            }
            .font(.system(size: 12, weight: .bold)).padding(.horizontal, 14).frame(height: 34)
            .background(model.tripActive ? Color.red.opacity(0.85) : BrakeTheme.accent, in: Capsule())
            .foregroundStyle(model.tripActive ? .white : BrakeTheme.background)
            .disabled(model.demo || model.stopping)
        }
        .buttonStyle(.plain).padding(.horizontal, 12).padding(.vertical, 6)
        .background(.black.opacity(0.48), in: RoundedRectangle(cornerRadius: 18))
    }

    private func hudButton(_ image: String, label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: image).font(.system(size: 14, weight: .semibold)).frame(width: 32, height: 32)
                .background(.white.opacity(0.08), in: Circle())
        }.accessibilityLabel(label)
    }
    private var speedHUD: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(model.demo ? "SIMULATED SPEED" : "YOUR SPEED")
                .font(.system(size: 9, weight: .bold)).tracking(0.7).foregroundStyle(.white.opacity(0.75))
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text(model.speed.map { String(format: "%.0f", $0 * factor) } ?? "—")
                    .font(.system(size: 58, weight: .medium, design: .rounded)).monospacedDigit().tracking(-2)
                    .minimumScaleFactor(0.7).lineLimit(1)
                Button(unit) { useMPH.toggle() }.font(.system(size: 12, weight: .semibold))
                    .accessibilityLabel("Switch speed units")
            }
            Text(model.gpsAccuracy.map { String(format: "GPS ±%.1f %@", $0 * factor, unit) }
                 ?? (model.demo ? "Generated scenario" : "GPS waiting"))
                .font(.system(size: 9)).foregroundStyle(.white.opacity(0.75)).lineLimit(1).minimumScaleFactor(0.75)
        }
        .frame(maxWidth: .infinity, alignment: .leading).padding(12)
        .background(.black.opacity(0.58), in: RoundedRectangle(cornerRadius: 18))
    }
    private var riskHUD: some View {
        VStack(alignment: .leading, spacing: prominentRisk ? 7 : 5) {
            HStack {
                Image(systemName: model.risk.level == .critical ? "exclamationmark.brakesignal" : "shield.lefthalf.filled")
                    .font(.system(size: prominentRisk ? 20 : 12, weight: .semibold))
                Text(riskTitle).font(.system(size: prominentRisk ? 23 : 13, weight: .bold, design: .rounded))
                    .monospacedDigit().lineLimit(1).minimumScaleFactor(0.65)
                Spacer()
                if model.risk.level != .critical {
                    Text(model.risk.score.map { String(format: "%.0f / 100", $0) } ?? "—")
                        .font(.system(size: 11, weight: .semibold)).monospacedDigit()
                }
            }.foregroundStyle(model.risk.level == .critical ? Color.white : riskColor)
            ProgressView(value: (model.risk.score ?? 0) / 100)
                .tint(model.risk.level == .critical ? Color.white : riskColor)
            Text(model.risk.reason).font(.system(size: 10)).foregroundStyle(.white.opacity(0.85))
                .lineLimit(2).fixedSize(horizontal: false, vertical: true)
        }
        .padding(prominentRisk ? 15 : 10)
        .background(model.risk.level == .critical ? Color.red.opacity(0.95) : Color.black.opacity(0.66),
                    in: RoundedRectangle(cornerRadius: prominentRisk ? 23 : 18))
        .overlay(RoundedRectangle(cornerRadius: prominentRisk ? 23 : 18)
            .stroke(riskColor.opacity(prominentRisk ? 0.80 : 0.25), lineWidth: prominentRisk ? 2 : 1))
        .accessibilityElement(children: .combine)
    }
    private func metricPanel(compact: Bool) -> some View {
        LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 4), count: compact ? 2 : 4), spacing: 10) {
            metric("Distance", value: model.risk.distance.map { $0 * distanceFactor }, unit: distanceUnit)
            metric("Their speed", value: model.risk.leadSpeed.map { $0 * factor }, unit: unit)
            metric("Closing in", value: model.risk.closingSpeed.map { $0 * factor }, unit: unit)
            metric("Time to impact", value: model.risk.timeToCollision, unit: "s")
        }
        .padding(.vertical, 10).padding(.horizontal, 6)
        .background(.black.opacity(0.58), in: RoundedRectangle(cornerRadius: 16))
    }
    private func metric(_ label: String, value: Double?, unit: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.system(size: 9, weight: .semibold)).foregroundStyle(.white.opacity(0.70))
                .lineLimit(1).minimumScaleFactor(0.7)
            (Text(value.map { String(format: "%.1f", $0) } ?? "—")
                .font(.system(size: 19, weight: .semibold, design: .rounded)).monospacedDigit()
             + Text(" \(unit)").font(.system(size: 9)))
                .lineLimit(1).minimumScaleFactor(0.7)
        }.frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 6)
    }
    private var footer: some View {
        HStack(spacing: 10) {
            Button { model.markIncident() } label: { Label("Save incident", systemImage: "bookmark.fill") }
                .font(.system(size: 11, weight: .semibold)).padding(.horizontal, 12).padding(.vertical, 7)
                .background(.black.opacity(0.5), in: Capsule())
                .disabled(!model.tripActive || model.demo)
            Text("15 min before + after").font(.system(size: 9)).foregroundStyle(.white.opacity(0.65))
            Spacer(minLength: 0)
            Text("Approximate camera estimates")
                .font(.system(size: 9)).foregroundStyle(.white.opacity(0.7)).lineLimit(1).minimumScaleFactor(0.8)
        }
    }
    private var calibrationControls: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("While parked, draw around the rear of a vehicle and enter its measured gap.").font(.caption)
            HStack {
                TextField("Meters", text: $measuredDistance).keyboardType(.decimalPad).textFieldStyle(.roundedBorder)
                Button("Track") {
                    if let box = selection, let distance = calibration {
                        model.selectVehicle(box: box, measuredDistance: distance); selecting = false
                    }
                }.disabled(selection == nil || !(2...80).contains(calibration ?? 0))
                Button("Cancel") { selecting = false; selection = nil }
            }
        }.padding(14).background(.black.opacity(0.8), in: RoundedRectangle(cornerRadius: 18))
    }
    private func presentPendingCheckin() {
        if requestDemo { requestDemo = false; model.simulateCheckin() }
        if safety.checkinPresented { showCheckin = true }
    }
}
