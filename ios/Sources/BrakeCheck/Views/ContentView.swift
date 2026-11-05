import BrakeCheckCore
import StoreKit
import SwiftUI
import UIKit

/// Home holds controls; Camera and Navigation share the live driving pipeline.
/// Both pages share one model, so swiping never restarts the trip or tracker.
struct ContentView: View {
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.scenePhase) private var scenePhase
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
    let model: DriveModel
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
    @State private var completedTripPreviousCount: Int?
    @State private var firstTripPresentationTask: Task<Void, Never>?
    @AppStorage("review.firstTripPrompted") private var firstTripPrompted = false
    /// Onboarding: the driver-aid notice (BrakeCheckApp), then one practice drive offer, then Premium.
    @AppStorage("onboarding.driverAidNotice") private var acceptedNotice = 0
    @AppStorage("onboarding.practiceDriveOffered") private var practiceDriveOffered = false
    @State private var showPracticePrompt = false
    @State private var homeChecklistPresented = false
    @State private var practiceFlow = PracticeDriveFlow()
    @State private var onboardingVisible = false
    @AppStorage("onboarding.appTourOffered") private var appTourOffered = false
    @State private var showTourOffer = false
    @State private var tourAccepted = false
    @State private var tourStep: AppTourStep?
    @State private var showOnboardingPremium = false

    init(model: DriveModel) {
        self.model = model
        _safety = ObservedObject(wrappedValue: model.safety)
        _navigation = ObservedObject(wrappedValue: model.navigation)
    }
    /// The warning transition is the only camera-rate state the page shell needs.
    private var critical: Bool { model.brakeWarningActive }

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
                openCameraSetup: { destinationPath = []; page = .camera; showCameraSetup = true },
                showTour: startTour).equatable()
        case .contacts: ContactsView(service: safety)
        case .incidents: IncidentListView(model: model)
        case .tripHistory: TripHistoryView(model: model, useMPH: useMPH)
        case .replay: ReplayView(model: model)
        case .premium: PremiumView(model: model)
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
        .fullScreenCover(isPresented: $showCheckin, onDismiss: presentFinalizedTripSummary) {
            CrashCheckinView(service: safety)
        }
        .fullScreenCover(isPresented: $showFirstTripPremium, onDismiss: {
            presentPendingCheckin()
            presentFinalizedTripSummary()
        }) {
            PremiumView(model: model, onContinue: { showFirstTripPremium = false })
        }
        .alert("BrakeSense", isPresented: messageShown) {
            Button("OK", role: .cancel) { model.message = nil }
        } message: { Text(model.message ?? "") }
        .confirmationDialog("End this trip? Remaining post-event recording will stop.", isPresented: $stopping, titleVisibility: .visible) {
            Button("End trip", role: .destructive) { finishTripAndPresentSummary() }
        }
        .onChange(of: TripIntentBridge.shared.request, initial: true) { _, request in
            guard let request else { return }
            performIntent(request)
        }
    }

    /// Siri / Shortcuts requests go through the same trip path as the buttons.
    private func performIntent(_ request: TripIntentRequest) {
        TripIntentBridge.shared.clear()
        switch request.kind {
        case .start:
            guard !model.tripActive, !model.demo, !model.replaying, !model.stopping else { return }
            stopping = false
            showCameraSetup = false
            destinationPath = []
            page = .camera
            model.startTrip()
        case .end:
            guard model.tripActive else { return }
            stopping = false
            finishTripAndPresentSummary()
        }
    }

    private var messageShown: Binding<Bool> {
        Binding(get: { model.message != nil }, set: { if !$0 { model.message = nil } })
    }

    private var drivingPages: some View {
        observingOnboarding(observingKeyboard(arSynced(observingNavigation(observingCamera(observingCheckin(presentedPages))))))
        .overlay {
            if showCameraSetup {
                CameraCalibrationOverlay(model: model, presentation: model.arPresentation, cameraAR: $cameraAR,
                    onClose: { showCameraSetup = false })
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .animation(.easeOut(duration: 0.2), value: showCameraSetup)
        .overlay(alignment: .bottom) {
            if let step = tourStep, destinationPath.isEmpty, !showCameraSetup {
                AppTourCard(step: step, back: { showTourStep(step.previous) },
                            advance: { showTourStep(step.next) }, skip: { showTourStep(nil) })
                    .padding(.bottom, 96)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .animation(.easeOut(duration: 0.2), value: tourStep)
    }

    /// The tour is offered once, after the driver-aid notice and the practice-drive flow
    /// have had their turn, on an idle Home page.
    private var tourOfferReady: Bool {
        acceptedNotice >= 1 && practiceDriveOffered && !appTourOffered && !showPracticePrompt && !showOnboardingPremium
            && page == .home && destinationPath.isEmpty && scenePhase == .active && !model.tripActive && !model.demo
            && !model.replaying && !model.stopping && !showCheckin && !requestDemo && !safety.checkinPresented
            && !showFirstTripPremium && !showCameraSetup && model.message == nil && completedTripID == nil
            && !homeChecklistPresented && tourStep == nil
    }

    private func startTour() {
        destinationPath = []
        showCameraSetup = false
        showTourStep(.startTrip)
    }

    private func showTourStep(_ step: AppTourStep?) {
        tourStep = step
        guard let step else { return }
        switch step {
        case .camera: page = .camera
        case .navigate: page = .navigation
        default: page = .home
        }
    }

    private var practiceContext: PracticeDriveFlow.Context {
        var context = PracticeDriveFlow.Context()
        context.visible = onboardingVisible
        context.foreground = scenePhase == .active
        context.noticeAccepted = acceptedNotice >= 1
        context.previouslyOffered = practiceDriveOffered
        context.onHome = page == .home
        context.liveTrip = model.tripActive
        context.finalizing = model.stopping
        context.replay = model.replaying
        context.navigation = navigation.isNavigating
        context.checkin = safety.checkinPresented || showCheckin || requestDemo
        context.presentationBusy = !destinationPath.isEmpty || showCameraSetup || selecting || keyboardShown || homeChecklistPresented
            || showFirstTripPremium || showOnboardingPremium || stopping || model.message != nil || completedTripID != nil
        context.subscribed = premiumStore.isSubscribed
        context.demoSessionID = model.demoSessionID
        return context
    }

    private func observingOnboarding<Content: View>(_ content: Content) -> some View {
        content
        .onAppear { onboardingVisible = true; reconcilePractice() }
        .onDisappear { onboardingVisible = false; reconcilePractice() }
        .onChange(of: practiceContext) { _, _ in reconcilePractice() }
        .sheet(isPresented: $showPracticePrompt, onDismiss: {
            practiceFlow.skip(); presentPendingCheckin(); presentFinalizedTripSummary()
        }) {
            PracticeDrivePrompt(start: startPracticeDrive, skip: {
                practiceFlow.skip(); showPracticePrompt = false
            })
            .presentationDetents([.medium, .large])
        }
        .fullScreenCover(isPresented: $showOnboardingPremium, onDismiss: {
            presentPendingCheckin(); presentFinalizedTripSummary()
        }) {
            PremiumView(model: model, context: .onboarding, onClose: { showOnboardingPremium = false })
        }
        .task(id: tourOfferReady) {
            guard tourOfferReady else { return }
            // Let the practice flow's own follow-ups (the Premium offer) settle first.
            try? await Task.sleep(for: .seconds(2))
            guard !Task.isCancelled, tourOfferReady else { return }
            appTourOffered = true
            tourAccepted = false
            showTourOffer = true
        }
        .sheet(isPresented: $showTourOffer, onDismiss: {
            if tourAccepted { startTour() }
        }) {
            AppTourOfferView(accept: { tourAccepted = true; showTourOffer = false },
                             skip: { showTourOffer = false })
                .presentationDetents([.medium])
        }
        .task(id: practiceFlow.request) {
            guard let request = practiceFlow.request else { return }
            do {
                // Loop only for an early wake; canceled or replaced requests never fire.
                while ProcessInfo.processInfo.systemUptime < request.deadline {
                    try await Task.sleep(for: .seconds(max(0.001,
                        request.deadline - ProcessInfo.processInfo.systemUptime)))
                }
                try Task.checkCancellation()
            } catch { return }
            applyPractice(practiceFlow.fire(request, context: practiceContext,
                now: ProcessInfo.processInfo.systemUptime))
            reconcilePractice()
        }
    }

    private func reconcilePractice() {
        applyPractice(practiceFlow.reconcile(practiceContext, now: ProcessInfo.processInfo.systemUptime))
    }
    private func applyPractice(_ actions: [PracticeDriveFlow.Action]) {
        for action in actions {
            switch action {
            case .showOffer:
                // Persist only an offer that is actually being presented.
                practiceDriveOffered = true; showPracticePrompt = true
            case .dismissOffer: showPracticePrompt = false
            case .startDemo(let id): model.setDemo(true, hardBrake: true, sessionID: id)
            case .stopDemo(let id):
                if model.demoSessionID == id { model.setDemo(false) }
            case .returnHome: page = .home
            case .showPremium: showOnboardingPremium = true
            }
        }
    }
    private func startPracticeDrive() {
        practiceFlow.accept(practiceContext, now: ProcessInfo.processInfo.systemUptime)
        showPracticePrompt = false
    }
    private func finishPracticeDrive() {
        applyPractice(practiceFlow.finish(practiceContext, now: ProcessInfo.processInfo.systemUptime))
        reconcilePractice()
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
            else {
                firstTripPresentationTask?.cancel()
                firstTripPresentationTask = nil
                destinationPath = []
                presentPendingCheckin()
            }
        }
        .onChange(of: requestDemo) { _, requested in
            if requested { destinationPath = [] }
        }
        .onChange(of: destinationPath) { _, destinations in
            updateARDemand()
            navigation.setPageVisible(destinations.isEmpty && page == .navigation && !selecting)
            if destinations.isEmpty {
                presentPendingCheckin()
                presentFinalizedTripSummary()
            }
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
        .onChange(of: model.stopping) { _, finishing in
            if !finishing { presentFinalizedTripSummary() }
        }
        .onChange(of: model.lastSavedTripID) { _, _ in presentFinalizedTripSummary() }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                presentPendingCheckin()
                presentFinalizedTripSummary()
            } else {
                firstTripPresentationTask?.cancel()
                firstTripPresentationTask = nil
            }
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
        .onDisappear {
            model.setARCameraVisible(false)
            firstTripPresentationTask?.cancel()
            firstTripPresentationTask = nil
        }
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
                 openPremium: { destinationPath.append(.premium) }, isSubscribed: premiumStore.isSubscribed,
                 onChecklistChanged: { homeChecklistPresented = $0 },
                 lastDestination: navigation.destinationTitle == nil && !navigation.isNavigating
                     ? navigation.recentDestinations.first?.title : nil,
                 navigateAgain: {
                     if let recent = navigation.recentDestinations.first { navigation.chooseRecent(recent) }
                     page = .navigation
                 })
    }

    private func cameraPage(portrait: Bool) -> some View {
        CameraPage(model: model, navigation: navigation, safety: safety, premiumStore: premiumStore,
                   portrait: portrait, cameraVisible: page == .camera && destinationPath.isEmpty,
                   showCameraSetup: $showCameraSetup, selecting: $selecting,
                   selection: $selection, measuredDistance: $measuredDistance,
                   practiceActive: practiceFlow.sessionID != nil, endPractice: finishPracticeDrive)
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
        guard let tripID = model.endTrip() else { return }
        page = .home
        completedTripID = tripID
        completedTripPreviousCount = previousCount
        presentFinalizedTripSummary()
    }

    /// Keep replay/history behind the final recording and speed-log flush.
    /// A safety check-in retains presentation priority while that work ends.
    private var canPresentCompletedTrip: Bool {
        scenePhase == .active && !model.stopping && !model.tripActive
            && !safety.checkinPresented && !showCheckin && !showFirstTripPremium
            && destinationPath.isEmpty && !showCameraSetup
    }

    private func presentFinalizedTripSummary() {
        guard canPresentCompletedTrip, let id = completedTripID, model.savedTrips.contains(where: { $0.id == id }),
              let previousCount = completedTripPreviousCount else { return }
        if max(previousCount, model.savedTrips.count - 1) == 0 && !firstTripPrompted {
            presentFirstTripPremium()
        } else {
            openCompletedTrip()
        }
    }

    private func openCompletedTrip() {
        guard canPresentCompletedTrip, let id = completedTripID else { return }
        completedTripID = nil
        completedTripPreviousCount = nil
        destinationPath = [.trip(id)]
    }

    private func presentFirstTripPremium() {
        guard firstTripPresentationTask == nil else { return }
        firstTripPresentationTask = Task { @MainActor in
            do { try await Task.sleep(for: .milliseconds(300)) }
            catch { return }
            firstTripPresentationTask = nil
            // A check-in or a new trip may have started during the transition.
            // Keep the summary pending until a safe presentation opportunity.
            guard !Task.isCancelled, canPresentCompletedTrip, completedTripID != nil else { return }
            firstTripPrompted = true
            reviewAfterSummary = true
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
        DrivingRiskStatus(model: model, safety: safety, premiumStore: premiumStore, large: large, selecting: selecting)
    }

    private func presentPendingCheckin() {
        if requestDemo { requestDemo = false; model.simulateCheckin() }
        if safety.checkinPresented {
            if showPracticePrompt { showPracticePrompt = false }
            else if showOnboardingPremium { showOnboardingPremium = false }
            else if showFirstTripPremium { showFirstTripPremium = false }
            else { showCheckin = true }
        }
    }

}
