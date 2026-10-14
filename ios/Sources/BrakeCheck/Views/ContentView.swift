import BrakeCheckCore
import SwiftUI
import UIKit

/// Home holds controls; Camera and Navigation share the live driving pipeline.
/// Both pages share one model, so swiping never restarts the trip or tracker.
struct ContentView: View {
    @Environment(\.colorScheme) private var colorScheme
    private enum Page: Int, CaseIterable {
        case home, camera, navigation
        var title: String {
            switch self {
            case .home: return "Home"
            case .camera: return "Camera"
            case .navigation: return "Navigate"
            }
        }
    }
    private enum HomeDestination: Hashable {
        case settings, contacts, incidents, tripHistory, diagnostics, replay
    }
    @State private var destinationPath: [HomeDestination] = []
    @ObservedObject var model: DriveModel
    @ObservedObject private var safety: SafetyService
    @ObservedObject private var navigation: NavigationService
    @AppStorage("display.mph") private var useMPH = true
    @AppStorage("display.accent") private var accentChoice = "blue"
    @AppStorage("display.detectionBoxes") private var showDetections = true
    @AppStorage("display.lanes") private var showLanes = true
    @AppStorage("display.fullCameraFrame") private var showFullFrame = true
    @State private var selecting = false
    @State private var selection: CGRect?
    @State private var measuredDistance = ""
    @State private var stopping = false
    @State private var requestDemo = false
    @State private var showCheckin = false
    @State private var page: Page = .home

    init(model: DriveModel) {
        self.model = model
        _safety = ObservedObject(wrappedValue: model.safety)
        _navigation = ObservedObject(wrappedValue: model.navigation)
    }
    private var factor: Double { useMPH ? 2.2369362921 : 3.6 }
    private var unit: String { useMPH ? "mph" : "km/h" }
    private var calibration: Double? { Double(measuredDistance.replacingOccurrences(of: ",", with: ".")) }
    /// A triggered warning lasts at least five seconds, even if the target or
    /// current risk disappears. Numeric measurements keep their own expiry.
    private var critical: Bool { model.brakeWarningActive }
    private var running: Bool { model.tripActive || model.replaying || model.demo }
    private var showsPlaceholder: Bool { (!model.cameraReady && !model.replaying) || model.demo }
    private var gapMotionBlocker: String {
        let reason = model.cameraRange.reason.lowercased()
        if reason.contains("gap ·") { return "Detection gap" }
        if reason.contains("delayed") || reason.contains("expired") { return "Camera delayed" }
        if model.trackedDistance == nil { return "Needs target" }
        if reason.contains("confidence") { return "Low confidence" }
        if reason.contains("unsettled") { return "Range unsettled" }
        return "Sampling"
    }

    var body: some View {
        NavigationStack(path: $destinationPath) {
            drivingPages
                .navigationTitle("Home")
                .toolbar(.hidden, for: .navigationBar)
                .navigationDestination(for: HomeDestination.self) { destination in
                    destinationPage(destination)
                        .modifier(DedicatedPageStyle())
                }
        }
    }

    @ViewBuilder private func destinationPage(_ destination: HomeDestination) -> some View {
        switch destination {
        case .settings:
            SettingsView(model: model, service: safety, settings: safety.settings, requestDemo: $requestDemo)
        case .contacts: ContactsView(service: safety)
        case .incidents: IncidentListView(model: model)
        case .tripHistory: TripHistoryView(model: model, useMPH: useMPH)
        case .diagnostics: MeasurementDiagnosticsView(model: model)
        case .replay: ReplayView(model: model)
        }
    }

    private var messagePresented: Binding<Bool> {
        Binding(get: { model.message != nil }, set: { if !$0 { model.message = nil } })
    }

    // Split into small pieces so Xcode 26 can type-check them; one combined
    // chain of ~30 modifiers timed out. Same modifiers, same order.
    private var drivingPages: some View {
        observingNavigation(observingCamera(observingCheckin(drivingLayout)))
    }

    private func observingCheckin<Content: View>(_ content: Content) -> some View {
        content
        .onChange(of: safety.checkinPresented) { _, presented in
            if !presented { showCheckin = false }
            else { destinationPath = []; showCheckin = true }
        }
        .onChange(of: requestDemo) { _, requested in
            if requested { destinationPath = [] }
        }
        .onChange(of: destinationPath) { _, destinations in
            navigation.setPageVisible(destinations.isEmpty && page == .navigation && !selecting)
            if destinations.isEmpty { presentPendingCheckin() }
        }
    }

    private func observingCamera<Content: View>(_ content: Content) -> some View {
        content
        .onChange(of: model.cameraReady) { _, ready in if !ready { selecting = false; selection = nil } }
        .onChange(of: model.captureGeometry) { _, _ in selecting = false; selection = nil }
        .onChange(of: model.changingCameraGeometry) { _, changing in
            if changing { selecting = false; selection = nil }
        }
        .onChange(of: model.tripActive) { _, active in
            if active { page = navigation.isNavigating ? .navigation : .camera }
        }
    }

    private func observingNavigation<Content: View>(_ content: Content) -> some View {
        content
        .onChange(of: navigation.isNavigating) { _, active in if active { page = .navigation } }
        .onChange(of: page) { _, destination in navigation.setPageVisible(destinationPath.isEmpty && destination == .navigation && !selecting) }
        .onChange(of: selecting) { _, active in navigation.setPageVisible(destinationPath.isEmpty && page == .navigation && !active) }
        .onChange(of: useMPH) { _, miles in navigation.useMPH = miles }
        .onChange(of: model.replaying) { _, active in if active { page = .camera } }
        .onChange(of: model.demo) { _, active in if active { page = .camera } }
        .task { await safety.connectIfPaired() }
    }

    private var drivingLayout: some View {
        GeometryReader { geometry in
            let portrait = geometry.size.height > geometry.size.width
            ZStack {
                VStack(spacing: 0) {
                    if critical && page == .home && !selecting {
                        riskPill(large: portrait)
                            .padding(.horizontal, 16).padding(.vertical, 12)
                            .frame(maxWidth: .infinity)
                            .background(BrakeTheme.danger.opacity(0.12))
                    }
                    if selecting {
                        // Drawing a calibration box must not compete with a paging gesture.
                        cameraPage(portrait: portrait)
                    } else {
                        TabView(selection: $page) {
                            homePage.tag(Page.home)
                            cameraPage(portrait: portrait).tag(Page.camera)
                            NavigationView(navigation: navigation, model: model, useMPH: useMPH,
                                startDrive: model.startNavigationDrive, openCamera: { page = .camera })
                                .tag(Page.navigation)
                        }
                        .tabViewStyle(.page(indexDisplayMode: .never))
                        pageNavigation
                    }
                }
                if critical { BrakeEdgeGlow() }
            }
        }
        .background {
            InterfaceOrientationObserver { model.interfaceOrientationChanged($0) }
                .allowsHitTesting(false)
        }
        .background(BrakeTheme.background.ignoresSafeArea())
        .animation(.easeOut(duration: 0.15), value: critical)
        .tint(BrakeTheme.accentColor(named: accentChoice))
        .statusBarHidden()
        .persistentSystemOverlays(.hidden)
        .fullScreenCover(isPresented: $showCheckin) { CrashCheckinView(service: safety) }
        .alert("BrakeCheck", isPresented: messagePresented) {
            Button("OK", role: .cancel) { model.message = nil }
        } message: { Text(model.message ?? "") }
        .confirmationDialog("End this trip? Remaining post-event recording will stop.", isPresented: $stopping, titleVisibility: .visible) {
            Button("End trip", role: .destructive) { selecting = false; model.endTrip(); page = .home }
        }
    }

    private var homePage: some View {
        HomeView(model: model, service: safety,
                 useMPH: $useMPH, showDetections: $showDetections,
                 showLanes: $showLanes, showFullFrame: $showFullFrame,
                 openCamera: { page = .camera }, openNavigation: { page = .navigation }, tripAction: performTripAction,
                 openSettings: { destinationPath.append(.settings) },
                 openContacts: { destinationPath.append(.contacts) },
                 openIncidents: { destinationPath.append(.incidents) },
                 openTripHistory: { destinationPath.append(.tripHistory) },
                 openDiagnostics: { destinationPath.append(.diagnostics) },
                 openReplay: { destinationPath.append(.replay) }, beginCalibration: beginCalibration)
    }

    private func cameraPage(portrait: Bool) -> some View {
        ZStack {
            roadView(portrait: portrait).ignoresSafeArea()
            if showsPlaceholder { placeholder }
            scrims(portrait: portrait).ignoresSafeArea().allowsHitTesting(false)
            if portrait { portraitHUD } else { landscapeHUD }
        }
        .clipped()
    }

    private var pageNavigation: some View {
        HStack(spacing: 4) {
            ForEach(Page.allCases, id: \.rawValue) { destination in
                Button {
                    withAnimation(.easeInOut(duration: 0.22)) { page = destination }
                } label: {
                    HStack(spacing: 8) {
                        Circle().fill(page == destination ? BrakeTheme.accent : BrakeTheme.muted.opacity(0.45))
                            .frame(width: 5, height: 5)
                        Text(destination.title).font(.system(size: 13, weight: .semibold))
                    }
                    .foregroundStyle(page == destination ? BrakeTheme.accent : BrakeTheme.muted)
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .background(page == destination ? BrakeTheme.accent.opacity(0.12) : .clear, in: Capsule())
                }
                .accessibilityValue(page == destination ? "Selected" : "")
                .accessibilityHint("Tap a tab or swipe along this bottom bar.")
            }
        }
        .buttonStyle(.plain)
        .padding(4)
        .frame(maxWidth: 360)
        .background(BrakeTheme.panel, in: Capsule())
        .padding(.vertical, 4)
        .frame(maxWidth: .infinity)
        .background(BrakeTheme.background)
        .contentShape(Rectangle())
        .highPriorityGesture(DragGesture(minimumDistance: 24).onEnded { drag in
            let horizontal = drag.translation.width
            guard abs(horizontal) > 45, abs(horizontal) > abs(drag.translation.height) * 1.5 else { return }
            let next = min(Page.allCases.count - 1, max(0, page.rawValue + (horizontal < 0 ? 1 : -1)))
            if let destination = Page(rawValue: next) {
                withAnimation(.easeInOut(duration: 0.2)) { page = destination }
            }
        })
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Pages")
    }

    private func performTripAction() {
        if model.replaying { model.stopReplay() }
        else if model.tripActive { stopping = true }
        else { model.startTrip() }
    }

    private func beginCalibration() {
        guard model.cameraReady, !model.replaying, !model.demo, !model.changingCameraGeometry else { return }
        model.clearTarget()
        selection = nil
        measuredDistance = ""
        page = .camera
        selecting = true
    }

    private func riskPill(large: Bool) -> some View {
        RiskPill(risk: model.risk, critical: critical,
                 meter: model.dangerMeter, large: large)
    }

    private func presentPendingCheckin() {
        if requestDemo { requestDemo = false; model.simulateCheckin() }
        if safety.checkinPresented { showCheckin = true }
    }

    // MARK: - Road

    /// The camera (or replay frame) and its overlays share one box with the video's shape. The box fills the
    /// screen, fitting the whole camera frame by default; Home also offers a cropped fill view.
    private func roadView(portrait: Bool) -> some View {
        let frameSize = model.replaying ? model.replayFrame?.size : nil
        let aspect = frameSize.map { Double($0.width / max($0.height, 1)) } ?? model.captureGeometry.aspect
        return GeometryReader { geometry in
            let frame = showFullFrame
                ? CameraGeometry.aspectFitRect(in: geometry.size, imageAspect: aspect)
                : CameraGeometry.aspectFillRect(in: geometry.size, imageAspect: aspect)
            ZStack {
                BrakeTheme.background
                if model.replaying {
                    if let frame = model.replayFrame { Image(uiImage: frame).resizable().scaledToFit() }
                } else {
                    CameraPreview(session: model.camera.session, orientation: model.captureGeometry.videoOrientation)
                }
                if (model.cameraReady || model.replaying) && !model.demo && !model.changingCameraGeometry {
                    if showLanes && !selecting {
                        RoadAreaOverlay(road: model.roadRegion)
                        LeadCorridorOverlay(lane: model.lanePath)
                    }
                    if showDetections && !selecting {
                        DetectionOverlay(detections: model.detections, hazardTargetID: model.collisionHazardID,
                                         leadTargetID: model.leadTargetID)
                    }
                    TargetOverlay(trackedBox: model.trackingBox, selecting: selecting, selectedBox: $selection,
                                  alert: model.risk.level == .critical, coasting: model.trackingCoasting)
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
                if !model.demo {
                    Text("Swipe to Home for trip controls.")
                        .font(.system(size: 12)).foregroundStyle(BrakeTheme.accent)
                }
            }
            .padding(.horizontal, 24)
            .offset(y: -24)
        }
    }

    /// Theme-matched gradients keep readouts legible over the live road.
    private func scrims(portrait: Bool) -> some View {
        VStack(spacing: 0) {
            LinearGradient(colors: [BrakeTheme.cameraScrim.opacity(colorScheme == .light ? 0.94 : 0.6), .clear], startPoint: .top, endPoint: .bottom).frame(height: portrait ? 190 : 130)
            Spacer(minLength: 0)
            LinearGradient(colors: [.clear, BrakeTheme.cameraScrim.opacity(colorScheme == .light ? 0.96 : 0.7)], startPoint: .top, endPoint: .bottom).frame(height: portrait ? 260 : 150)
        }
    }

    // MARK: - Layouts

    private var landscapeHUD: some View {
        VStack(spacing: 8) {
            HStack(spacing: 8) {
                brand(size: 25)
                Spacer(minLength: 0)
            }
            if critical || (running && !selecting) {
                riskPill(large: false)
            }
            Spacer(minLength: 0)
            if selecting { calibrationControls.frame(maxWidth: 440) }
            // Standby shows only the welcome; readouts appear once there is something to read.
            if running { bottomRow(portrait: false) }
        }
        .padding(.horizontal, 12).padding(.top, 12).padding(.bottom, 6)
        .foregroundStyle(BrakeTheme.text)
    }

    /// Vertical: status and the danger meter above an unobstructed road, readouts below.
    private var portraitHUD: some View {
        VStack(spacing: 14) {
            HStack(spacing: 8) {
                brand(size: 19)
                Spacer(minLength: 0)
            }
            if critical || (running && !selecting) {
                riskPill(large: true)
            }
            Spacer(minLength: 0)
            if selecting { calibrationControls }
            if running { bottomRow(portrait: true) }
        }
        .padding(.horizontal, 16).padding(.top, 6).padding(.bottom, 8)
        .foregroundStyle(BrakeTheme.text)
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
            Text("\(text) · \(model.measurementRevision)").font(.system(size: 10, weight: .bold)).tracking(1)
        }
        .padding(.horizontal, 10).frame(height: 26)
        .background(BrakeTheme.cameraSurface, in: Capsule())
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
            Text(model.gpsSpeedEstimated ? "EST. SPEED" : "SPEED")
                .font(.system(size: 10, weight: .bold)).tracking(1).foregroundStyle(BrakeTheme.muted)
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text(model.speed.map { String(format: "%.0f", $0 * factor) } ?? "--")
                    .font(.system(size: size, weight: .semibold)).monospacedDigit().tracking(-2)
                    .foregroundStyle(model.speed == nil ? BrakeTheme.muted : BrakeTheme.text)
                Text(unit).font(.system(size: 15, weight: .semibold)).foregroundStyle(BrakeTheme.muted)
            }
            if model.replaying {
                Text(model.replaySpeedSource.hasPrefix("ASSUMED") ? "ASSUMED SPEED" : "RECORDED SPEED")
                    .font(.system(size: 9, weight: .semibold)).foregroundStyle(BrakeTheme.warning)
            } else if model.gpsSpeedEstimated {
                Text("GPS GAP · ESTIMATED").font(.system(size: 9, weight: .semibold)).foregroundStyle(BrakeTheme.warning)
            } else if model.speed == nil {
                Text("Waiting for GPS").font(.system(size: 11)).foregroundStyle(BrakeTheme.muted)
            }
        }
        .shadow(color: colorScheme == .dark ? .black.opacity(0.5) : .clear, radius: 6)
    }

    /// Numbers float on the video like a head-up display; the bottom scrim and a shadow keep them readable.
    /// Feet and mph when the speed unit is mph, metres and km/h otherwise.
    private func carAheadNumbers(compact: Bool) -> some View {
        let risk = model.risk
        let cameraDistance = model.demo ? risk.distance : model.trackedDistance
        let distance = cameraDistance.map { useMPH ? $0 * 3.28084 : $0 }
        let distanceTitle = model.demo ? "Distance" : model.rangeSource.hasPrefix("CALIBRATED RANGE")
            ? "Calibrated gap" : "Approx. gap"
        let ttc = risk.timeToCollision
        let closing = model.demo ? risk.closingSpeed : model.collisionHazardID != nil
            ? risk.closingSpeed ?? model.cameraRange.closingSpeed
            : model.cameraRange.closingSpeed ?? risk.closingSpeed
        let size: CGFloat = compact ? 21 : 24
        return Grid(alignment: .trailing, horizontalSpacing: compact ? 16 : 22, verticalSpacing: 8) {
            GridRow {
                hudNumber(distanceTitle, distance.map { String(format: "%.0f", $0) }, useMPH ? "ft" : "m", size: size)
                hudNumber(risk.leadSpeed == nil && model.estimatedLeadSpeed != nil ? "Est. their speed" : "Their speed",
                          (risk.leadSpeed ?? model.estimatedLeadSpeed).map { String(format: "%.0f", $0 * factor) }, unit,
                          size: size, unavailable: model.demo ? nil : model.speedBlocker)
            }
            GridRow {
                hudNumber("Gap closing", closing.map { String(format: "%.0f", $0 * factor) }, unit, size: size,
                          unavailable: gapMotionBlocker)
                hudNumber("Est. closing time", ttc.map { String(format: "%.1f", $0) }, "s", size: size,
                          alert: (ttc ?? .infinity) < 2,
                          unavailable: risk.leadSpeed != nil && (risk.closingSpeed ?? .infinity) <= 0.5
                            ? "Not closing" : model.collisionBlocker)
            }
        }
        .shadow(color: colorScheme == .dark ? .black.opacity(0.9) : .clear, radius: 8, y: 1)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Car ahead, estimated")
    }

    private func hudNumber(_ label: String, _ value: String?, _ unit: String, size: CGFloat,
                           alert: Bool = false, unavailable: String? = nil) -> some View {
        VStack(alignment: .trailing, spacing: 1) {
            Text(label).font(.system(size: 11, weight: .semibold)).foregroundStyle(BrakeTheme.muted)
            Text(value ?? "--").font(.system(size: size, weight: .semibold)).monospacedDigit()
                .foregroundColor(alert ? BrakeTheme.danger : BrakeTheme.text)
            + Text(" \(unit)").font(.system(size: 12, weight: .semibold)).foregroundColor(BrakeTheme.muted)
            if value == nil, let unavailable, !unavailable.isEmpty {
                Text(unavailable).font(.system(size: 10)).foregroundStyle(BrakeTheme.muted)
            }
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
        .background(BrakeTheme.cameraSurface, in: RoundedRectangle(cornerRadius: 10))
    }
}
