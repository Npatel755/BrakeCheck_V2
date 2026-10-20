import BrakeCheckCore
import StoreKit
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
        var icon: String {
            switch self {
            case .home: return "house.fill"
            case .camera: return "video.fill"
            case .navigation: return "location.north.line.fill"
            }
        }
        /// Stable accessibility identifier ("page.home"…), used by the UI test.
        var key: String { "page." + title.lowercased() }
    }
    private enum HomeDestination: Hashable {
        case settings, contacts, incidents, tripHistory, replay, premium
        case trip(UUID)
    }
    @State private var destinationPath: [HomeDestination] = []
    @ObservedObject var model: DriveModel
    @ObservedObject private var safety: SafetyService
    @ObservedObject private var navigation: NavigationService
    @ObservedObject private var premiumStore = PremiumStore.shared
    @AppStorage("display.mph") private var useMPH = true
    @AppStorage("display.accent") private var accentChoice = BrakeTheme.defaultAccent
    @AppStorage("display.detectionBoxes") private var showDetections = true
    @AppStorage("display.laneGuides") private var showLanes = false
    @AppStorage("navigation.cameraAR") private var cameraAR = true
    @AppStorage("display.fullCameraFrame") private var showFullFrame = true
    @AppStorage("display.premiumCompactBanner") private var premiumCompactBanner = false
    /// Road-model mask and build tag on the camera, for test recordings (Settings > Diagnostics).
    @AppStorage("diagnostics.developerOverlays") private var developerOverlays = false
    @State private var selecting = false
    @State private var selection: CGRect?
    @State private var measuredDistance = ""
    @State private var stopping = false
    @State private var requestDemo = false
    @State private var showCheckin = false
    @State private var showCameraSetup = false
    @State private var page: Page = .home
    /// While typing (a destination search), the tab bar steps aside so suggestions get the room.
    @State private var keyboardShown = false
    /// Set after the first trip; the system review prompt is requested once its summary closes.
    @State private var reviewAfterSummary = false
    @State private var showFirstTripPremium = false
    @State private var completedTripID: UUID?
    @AppStorage("review.firstTripPrompted") private var firstTripPrompted = false

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
        if reason.contains("clipped") { return "Vehicle clipped" }
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
            SettingsScreen(model: model, service: safety, settings: safety.settings, requestDemo: $requestDemo,
                openCameraSetup: { destinationPath = []; page = .camera; showCameraSetup = true }).equatable()
        case .contacts: ContactsView(service: safety)
        case .incidents: IncidentListView(model: model)
        case .tripHistory: TripHistoryView(model: model, useMPH: useMPH)
        case .replay: ReplayView(model: model)
        case .premium: PremiumView()
        case .trip(let id):
            if let trip = model.savedTrips.first(where: { $0.id == id }) {
                TripDetailView(model: model, trip: trip, useMPH: useMPH)
            }
        }
    }

    // Xcode 26 times out type-checking one 35-modifier chain, so the page modifiers are split:
    // pageStack -> presentedPages -> observing helpers -> drivingPages. Same modifiers, same order.
    private var pageStack: some View {
        GeometryReader { geometry in
            let portrait = geometry.size.height > geometry.size.width
            // One layout for both orientations, so rotating never rebuilds the pages (map, camera, scroll).
            let layout = portrait ? AnyLayout(VStackLayout(spacing: 0)) : AnyLayout(HStackLayout(spacing: 0))
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
                        layout {
                            if !portrait { pageRail }
                            pages(portrait: portrait)
                            if portrait && !keyboardShown { pageBar }
                        }
                    }
                }
                if critical { BrakeEdgeGlow() }
            }
        }
    }

    private func pages(portrait: Bool) -> some View {
        TabView(selection: $page) {
            homePage.tag(Page.home)
            cameraPage(portrait: portrait).tag(Page.camera)
            NavigationView(navigation: navigation, model: model, useMPH: useMPH,
                startDrive: model.startNavigationDrive, openCamera: { page = .camera })
                .tag(Page.navigation)
        }
        .tabViewStyle(.page(indexDisplayMode: .never))
        // The camera and map run up under the hidden status bar; page content keeps its own safe area.
        .ignoresSafeArea(.container, edges: .top)
    }

    private var presentedPages: some View {
        pageStack
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
        .fullScreenCover(isPresented: $showFirstTripPremium, onDismiss: openCompletedTrip) {
            PremiumView(onContinue: { showFirstTripPremium = false })
        }
        .alert("BrakeSense", isPresented: messageShown) {
            Button("OK", role: .cancel) { model.message = nil }
        } message: { Text(model.message ?? "") }
        .confirmationDialog("End this trip? Remaining post-event recording will stop.", isPresented: $stopping, titleVisibility: .visible) {
            Button("End trip", role: .destructive) { finishTripAndPresentSummary() }
        }
    }

    private var messageShown: Binding<Bool> {
        Binding(get: { model.message != nil }, set: { if !$0 { model.message = nil } })
    }

    private var drivingPages: some View {
        observingKeyboard(arSynced(observingNavigation(observingCamera(observingCheckin(presentedPages)))))
        .overlay {
            if showCameraSetup {
                CameraCalibrationOverlay(model: model, presentation: model.arPresentation, cameraAR: $cameraAR,
                    onClose: { showCameraSetup = false })
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .animation(.easeOut(duration: 0.2), value: showCameraSetup)
    }

    private func observingKeyboard<Content: View>(_ content: Content) -> some View {
        content
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in
            keyboardShown = true
        }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillHideNotification)) { _ in
            keyboardShown = false
        }
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
            updateARDemand()
            navigation.setPageVisible(destinations.isEmpty && page == .navigation && !selecting)
            if destinations.isEmpty { presentPendingCheckin() }
            // Back from the first trip summary: a calm moment for the system's own review prompt,
            // which decides by itself whether to show (App Review 5.6.1: no custom review prompts).
            if destinations.isEmpty && reviewAfterSummary && !safety.checkinPresented && !model.tripActive {
                reviewAfterSummary = false
                requestAppReview()
            }
        }
    }

    /// Calling StoreKit directly also works with the cross-compiled iPhone build, whose SDK
    /// does not automatically import StoreKit's SwiftUI environment extension.
    private func requestAppReview() {
        guard let scene = UIApplication.shared.connectedScenes.first(where: {
            $0.activationState == .foregroundActive
        }) as? UIWindowScene else { return }
        AppStore.requestReview(in: scene)
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
    }

    private func arSynced<Content: View>(_ content: Content) -> some View {
        content
        .task { await safety.connectIfPaired() }
        .onAppear {
            // The service starts in miles; without this a km/h setting only applied after it was changed.
            navigation.useMPH = useMPH
            updateARDemand()
        }
        .onChange(of: page) { _, _ in updateARDemand() }
        .onChange(of: navigation.isNavigating) { _, _ in updateARDemand() }
        .onChange(of: cameraAR) { _, _ in updateARDemand() }
        .onChange(of: selecting) { _, _ in updateARDemand() }
        .onDisappear { model.setARCameraVisible(false) }
    }
    private func updateARDemand() {
        model.setARCameraVisible(page == .camera && cameraAR && !selecting && destinationPath.isEmpty)
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
                 openReplay: { destinationPath.append(.replay) }, beginCalibration: beginCalibration,
                 openTrip: { destinationPath.append(.trip($0)) },
                 openPremium: { destinationPath.append(.premium) })
    }

    private func cameraPage(portrait: Bool) -> some View {
        ZStack {
            roadView(portrait: portrait).ignoresSafeArea()
            if showsPlaceholder { placeholder }
            scrims(portrait: portrait).ignoresSafeArea().allowsHitTesting(false)
            if portrait { portraitHUD } else { landscapeHUD }
            if developerOverlays && cameraAR && model.cameraReady && !model.replaying && !model.demo && !selecting {
                CameraRoadMaskStatus(presentation: model.arPresentation, portrait: portrait)
            }
        }
        .clipShape(SideClip())
    }

    // MARK: - Page switcher

    /// Portrait: a tab bar under the pages. A swipe along it also changes pages.
    private var pageBar: some View {
        HStack(spacing: 4) {
            ForEach(Page.allCases, id: \.rawValue) { destination in
                pageButton(destination).frame(maxWidth: .infinity)
            }
        }
        .padding(4)
        .frame(maxWidth: 420)
        .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .padding(.horizontal, 12).padding(.vertical, 4)
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

    /// Landscape (a dash mount): a slim rail on the leading edge, like CarPlay's. In the default
    /// full-frame view it only takes room from the camera's pillarbox bars, so the road stays as large.
    private var pageRail: some View {
        VStack(spacing: 8) {
            ForEach(Page.allCases, id: \.rawValue) { destination in
                pageButton(destination).frame(width: 60)
            }
        }
        .padding(.horizontal, 6)
        .frame(maxHeight: .infinity)
        .background(BrakeTheme.background)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Pages")
    }

    private func pageButton(_ destination: Page) -> some View {
        let selected = page == destination
        return Button {
            withAnimation(.easeInOut(duration: 0.22)) { page = destination }
        } label: {
            VStack(spacing: 3) {
                Image(systemName: destination.icon).font(.brake(size: 17, weight: .semibold))
                Text(destination.title).font(.brake(size: 11, weight: .semibold)).lineLimit(1).minimumScaleFactor(0.8)
            }
            .foregroundStyle(selected ? BrakeTheme.accentText : BrakeTheme.muted)
            .frame(maxWidth: .infinity, minHeight: 50)
            .background(selected ? BrakeTheme.accent.opacity(0.14) : .clear,
                        in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(destination.title)
        .accessibilityValue(selected ? "Selected" : "")
        .accessibilityIdentifier(destination.key)
    }

    private func performTripAction() {
        if model.replaying { model.stopReplay() }
        else if model.tripActive { stopping = true }
        else { model.startTrip() }
    }

    private func finishTripAndPresentSummary() {
        selecting = false
        let previousCount = model.savedTrips.count
        model.endTrip()
        page = .home
        guard model.savedTrips.count > previousCount, let trip = model.savedTrips.first else { return }
        completedTripID = trip.id
        if previousCount == 0 && !firstTripPrompted {
            firstTripPrompted = true
            reviewAfterSummary = true
            presentFirstTripPremium()
        } else {
            openCompletedTrip()
        }
    }

    private func openCompletedTrip() {
        guard let id = completedTripID else { return }
        completedTripID = nil
        destinationPath = [.trip(id)]
    }

    private func presentFirstTripPremium() {
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(300))
            showFirstTripPremium = true
        }
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
        VStack(spacing: 4) {
            RiskPill(risk: model.risk, critical: critical,
                 meter: model.dangerMeter, large: large,
                 unavailableTitle: !model.demo && !model.replaying && !model.detectorReady
                    ? (model.detectorStatus.contains("could not load") ? "Detection unavailable" : "Preparing detection") : nil)
            if let alert = model.drivingAssistAlert, premiumStore.isSubscribed,
               !critical && !safety.checkinPresented && running && !selecting {
                Label(alert.feature.alertText, systemImage: alert.feature.icon)
                    .font(.brake(size: large ? 14 : 12, weight: .semibold))
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 14).padding(.vertical, 10)
                    .foregroundStyle(BrakeTheme.text)
                    .background(BrakeTheme.cameraSurface, in: RoundedRectangle(cornerRadius: 14))
                    .accessibilityIdentifier("drivingAssistAlert")
                    .allowsHitTesting(false)
            }
            if !critical && !model.demo && !model.replaying && !model.detectorReady && model.detectorStatus.contains("could not load") {
                Button("Detection failed · tap for details") { model.message = model.detectorStatus }
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.caution)
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(.black.opacity(0.65), in: Capsule())
            }
        }
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
                    if developerOverlays && page == .camera && cameraAR && !model.replaying && !selecting && destinationPath.isEmpty {
                        CameraRoadMaskOverlay(presentation: model.arPresentation)
                    }
                    if page == .camera && !model.replaying && !selecting && destinationPath.isEmpty {
                        CameraNavigationOverlay(navigation: navigation, presentation: model.arPresentation, geometry: model.captureGeometry)
                    }
                    if showLanes && !selecting {
                        CameraPerceptionOverlay(presentation: model.arPresentation, lane: model.lanePath,
                            navigationOnCamera: page == .camera && navigation.isNavigating && cameraAR)
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
        let text = placeholderText
        return ZStack {
            BrakeTheme.panel.ignoresSafeArea()
            VStack(spacing: 10) {
                Image(systemName: model.demo ? "car.side.hill.up.fill" : "car.front.waves.up.fill")
                    .font(.brake(size: 44, weight: .light)).foregroundStyle(BrakeTheme.accentText.opacity(0.7))
                Text(text.title)
                    .font(.brake(size: 22, weight: .semibold))
                Text(text.detail)
                    .font(.brake(size: 13)).foregroundStyle(BrakeTheme.muted).multilineTextAlignment(.center)
            }
            .padding(.horizontal, 24)
            .offset(y: -24)
        }
    }

    /// During a trip the camera is starting (or can't), so "Start a trip" would be wrong there.
    private var placeholderText: (title: String, detail: String) {
        if model.demo { return ("Simulated drive", "Practice numbers · no camera") }
        guard model.tripActive else {
            return ("A clearer view ahead.", "Start a trip from Home to see the road and track vehicles.")
        }
        let status = model.cameraStatus
        if status.localizedCaseInsensitiveContains("denied") {
            return ("Camera access is off", "Allow Camera for BrakeSense in iPhone Settings to see the road and track vehicles.")
        }
        if status.hasPrefix("Camera: ") || status.localizedCaseInsensitiveContains("could not") {
            return ("Camera unavailable", String(status.dropFirst(status.hasPrefix("Camera: ") ? 8 : 0)))
        }
        return ("Starting the camera…", "Keep the phone steady in its mount.")
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

    /// Text directions stay available when the optional road ribbon is off.
    private var showsDirections: Bool { page == .camera && navigation.isNavigating && !selecting }

    private func directionsBanner(large: Bool) -> some View {
        NavigationBanner(navigation: navigation, presentation: model.arPresentation,
            compact: critical || (premiumStore.isSubscribed && premiumCompactBanner),
            large: large, onSetup: { showCameraSetup = true })
    }

    private var landscapeHUD: some View {
        VStack(spacing: 8) {
            if showsDirections {
                HStack(alignment: .top, spacing: 12) {
                    directionsBanner(large: false).frame(maxWidth: 400, alignment: .leading)
                    Spacer(minLength: 0)
                    if critical || running { riskPill(large: false).padding(.top, 4) }
                }
            } else {
                HStack(alignment: .top, spacing: 8) {
                    brand(size: 25)
                    Spacer(minLength: 0)
                    cameraSetupShortcut
                }
                if critical || (running && !selecting) { riskPill(large: false) }
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
            if showsDirections {
                if critical || (running && !selecting) {
                    riskPill(large: true)
                }
                directionsBanner(large: true)
            } else {
                HStack(spacing: 8) {
                    brand(size: 19)
                    Spacer(minLength: 0)
                    cameraSetupShortcut
                }
                if critical || (running && !selecting) {
                    riskPill(large: true)
                }
            }
            Spacer(minLength: 0)
            if selecting { calibrationControls }
            if running { bottomRow(portrait: true) }
        }
        .padding(.horizontal, 16).padding(.top, 6).padding(.bottom, 8)
        .foregroundStyle(BrakeTheme.text)
    }

    // MARK: - Controls

    @ViewBuilder private var cameraSetupShortcut: some View {
        if page == .camera && running && !model.replaying && !model.demo && !selecting && !navigation.isNavigating {
            CameraAlignmentShortcut(presentation: model.arPresentation, onOpen: { showCameraSetup = true })
        }
    }

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
            Text(developerOverlays ? "\(text) · \(model.measurementRevision)" : text)
                .font(.brake(size: 10, weight: .bold)).tracking(1)
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
                .font(.brake(size: 10, weight: .bold)).tracking(1).foregroundStyle(BrakeTheme.muted)
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text(model.speed.map { String(format: "%.0f", $0 * factor) } ?? "--")
                    .font(.brake(size: size, weight: .semibold)).monospacedDigit().tracking(-2)
                    .foregroundStyle(model.speed == nil ? BrakeTheme.muted : BrakeTheme.text)
                Text(unit).font(.brake(size: 15, weight: .semibold)).foregroundStyle(BrakeTheme.muted)
            }
            if model.replaying {
                Text(model.replaySpeedSource.hasPrefix("ASSUMED") ? "ASSUMED SPEED" : "RECORDED SPEED")
                    .font(.brake(size: 9, weight: .semibold)).foregroundStyle(BrakeTheme.warning)
            } else if model.gpsSpeedEstimated {
                Text("GPS GAP · ESTIMATED").font(.brake(size: 9, weight: .semibold)).foregroundStyle(BrakeTheme.warning)
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
            Text(label).font(.brake(size: 11, weight: .semibold)).foregroundStyle(BrakeTheme.muted)
            Text(value ?? "--").font(.brake(size: size, weight: .semibold)).monospacedDigit()
                .foregroundColor(alert ? BrakeTheme.danger : BrakeTheme.text)
            + Text(" \(unit)").font(.brake(size: 12, weight: .semibold)).foregroundColor(BrakeTheme.muted)
            if value == nil, let unavailable, !unavailable.isEmpty {
                Text(unavailable).font(.brake(size: 10)).foregroundStyle(BrakeTheme.muted)
            }
        }
    }

    private var calibrationControls: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("While parked, draw around the rear of a vehicle and enter its measured gap.").font(.brake(.caption))
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

/// Clips only the sides, so the camera page can't spill onto its neighbours during a swipe, while its
/// full-bleed layers (camera, placeholder, scrims) still reach up under the hidden status bar.
private struct SideClip: Shape {
    func path(in rect: CGRect) -> Path { Path(rect.insetBy(dx: 0, dy: -rect.height)) }
}
