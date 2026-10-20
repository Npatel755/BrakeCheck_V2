import CoreLocation
import BrakeCheckCore
import MapKit
import SwiftUI
import UIKit

/// The Navigate page. Planning: the map above a sheet with search, recents and the route card.
/// Guidance: the camera page's turn banner above the map and a trip bar (arrival, mute, end) below.
/// Landscape moves the sheet, banner and trip bar into a side column. The pieces are stacked rather
/// than layered, so no inset is guessed. It shares the driving model without owning its camera session.
struct NavigationView: View {
    @ObservedObject var navigation: NavigationService
    @ObservedObject var model: DriveModel
    var useMPH: Bool
    var startDrive: () -> Void
    @FocusState private var searchFocused: Bool
    @State private var recenterTrigger = 0
    @State private var listHeight: CGFloat = 0
    @State private var confirmingClearRecents = false
    @AppStorage("display.accent") private var accentChoice = BrakeTheme.defaultAccent

    private var canStart: Bool {
        navigation.route != nil && !navigation.isCalculating && navigation.mapLocation != nil
            && !model.demo && !model.replaying && !model.stopping
    }

    private var displaysRisk: Bool {
        model.tripActive || model.brakeWarningActive || model.demo || model.replaying
    }

    private var trimmedQuery: String { navigation.query.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        GeometryReader { geometry in
            // A portrait keyboard reduces height; it must not switch us to the landscape layout.
            let landscape = geometry.size.width > 550 && geometry.size.width > geometry.size.height
            // One layout for both orientations keeps the map (and its camera) alive when the phone rotates.
            let layout = landscape ? AnyLayout(HStackLayout(spacing: 0)) : AnyLayout(VStackLayout(spacing: 0))
            layout {
                if !landscape && navigation.isNavigating {
                    banner(large: true).padding(.horizontal, 12).padding(.vertical, 8)
                }
                mapArea(safeTop: !landscape && navigation.isNavigating ? 0 : geometry.safeAreaInsets.top)
                if landscape {
                    sideColumn(height: geometry.size.height)
                        .frame(width: min(360, geometry.size.width * 0.45))
                } else {
                    bottomSheet(maxListHeight: geometry.size.height * 0.42)
                }
            }
        }
        .background(BrakeTheme.background)
        .foregroundStyle(BrakeTheme.text)
        .tint(BrakeTheme.accentColor(named: accentChoice))
        .animation(.easeInOut(duration: 0.22), value: navigation.isNavigating)
        .animation(.easeInOut(duration: 0.22), value: navigation.route != nil)
        .onChange(of: navigation.pageVisible) { _, visible in
            if !visible { searchFocused = false }
        }
        .onChange(of: navigation.isNavigating) { _, _ in
            searchFocused = false
            recenterTrigger += 1
        }
        .confirmationDialog("Clear recent destinations?", isPresented: $confirmingClearRecents, titleVisibility: .visible) {
            Button("Clear recents", role: .destructive) { navigation.clearRecents() }
        }
    }

    // MARK: - Map

    /// `safeTop` is the area the map extends under at the top, so a route overview isn't framed beneath it.
    private func mapArea(safeTop: CGFloat) -> some View {
        // The map runs up under the hidden status bar; the clear base keeps the controls in the safe area.
        Color.clear
            .background {
                NavigationMapView(route: navigation.route, location: navigation.mapLocation,
                    locationIsCurrent: navigation.hasFreshLocation,
                    followsLocation: navigation.isNavigating && navigation.mapLocation != nil,
                    recenterTrigger: recenterTrigger,
                    topInset: safeTop + (displaysRisk ? 72 : 28),
                    isVisible: navigation.pageVisible && navigation.foreground)
                    .ignoresSafeArea(.container, edges: .top)
            }
            .overlay(alignment: .top) {
                if displaysRisk {
                    RiskPill(risk: model.risk, critical: model.brakeWarningActive, meter: model.dangerMeter,
                             large: false, unavailableTitle: riskUnavailableTitle)
                        .padding(.top, 12)
                }
            }
            .overlay(alignment: .bottomTrailing) {
                Button { recenterTrigger += 1 } label: {
                    Image(systemName: "location.north.fill")
                        .font(.brake(size: 18, weight: .semibold))
                        .foregroundStyle(BrakeTheme.accentText)
                        .frame(width: 48, height: 48)
                        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                }
                .buttonStyle(.plain)
                .accessibilityLabel(navigation.isNavigating ? "Recenter on your position"
                    : navigation.route == nil ? "Center on your position" : "Show the whole route")
                .accessibilityIdentifier("nav.recenter")
                .padding(14)
            }
    }

    /// While the camera isn't tracking, the risk pill says why instead of "No reading".
    private var riskUnavailableTitle: String? {
        guard model.tripActive, !model.demo, !model.replaying else { return nil }
        if model.stopping { return "Saving recording" }
        if !model.cameraReady || model.changingCameraGeometry {
            let status = model.cameraStatus.lowercased()
            if status.contains("denied") { return "Camera not allowed" }
            if model.changingCameraGeometry || status.contains("paused") || status.contains("interrupted") { return "Camera paused" }
            if status.contains("required") || status.contains("could not") || status.contains("unavailable") {
                return "Camera unavailable"
            }
            return "Camera starting"
        }
        if !model.detectorReady {
            return model.detectorStatus.contains("could not load") ? "Detection unavailable" : "Preparing detection"
        }
        return nil
    }

    private func banner(large: Bool) -> some View {
        NavigationBanner(navigation: navigation, presentation: model.arPresentation,
                         compact: model.brakeWarningActive, large: large, alignmentDetail: false)
    }

    // MARK: - Panels

    private func bottomSheet(maxListHeight: CGFloat) -> some View {
        Group {
            if navigation.isNavigating { tripBar } else { planner(maxListHeight: maxListHeight) }
        }
        .padding(.horizontal, 16).padding(.top, 16).padding(.bottom, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BrakeTheme.panel,
                    in: UnevenRoundedRectangle(topLeadingRadius: 26, topTrailingRadius: 26, style: .continuous))
        .shadow(color: .black.opacity(0.25), radius: 14, y: -4)
    }

    private func sideColumn(height: CGFloat) -> some View {
        VStack(spacing: 10) {
            if navigation.isNavigating {
                banner(large: false)
                Spacer(minLength: 0)
                tripBar.padding(14).background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
            } else {
                planner(maxListHeight: max(120, height - 150))
                    .padding(14)
                    .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                Spacer(minLength: 0)
            }
        }
        .padding(10)
    }

    // MARK: - Planning

    @ViewBuilder private func planner(maxListHeight: CGFloat) -> some View {
        if navigation.route != nil {
            // The route card must never hide its Start button: scroll only when it can't fit.
            ViewThatFits(in: .vertical) {
                routeCard
                ScrollView { routeCard }.scrollIndicators(.hidden)
            }
        } else {
            VStack(alignment: .leading, spacing: 10) {
                searchField
                if let error = navigation.suggestionError {
                    Text(error).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                }
                // While a chosen destination's route is on the way, the list would only distract.
                if !(navigation.destinationTitle != nil && (navigation.isCalculating || navigation.resolvingDestination != nil)) {
                    fittedList(maxHeight: maxListHeight)
                }
                progressRow
                if let note = statusNote {
                    Text(note).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let error = navigation.errorMessage { errorRow(error) }
            }
        }
    }

    private var searchField: some View {
        HStack(spacing: 10) {
            Image(systemName: "magnifyingglass").foregroundStyle(BrakeTheme.muted)
            TextField("Where to?", text: $navigation.query)
                .font(.brake(.body))
                .textInputAutocapitalization(.words)
                .submitLabel(.search)
                .focused($searchFocused)
                .onSubmit { submitSearch() }
                .accessibilityLabel("Destination search")
                .accessibilityIdentifier("nav.search")
            if navigation.isSearching || navigation.suggestionsLoading || navigation.resolvingDestination != nil {
                ProgressView().tint(BrakeTheme.accentText)
            } else if !navigation.query.isEmpty {
                Button { navigation.query = "" } label: {
                    Image(systemName: "xmark.circle.fill").foregroundStyle(BrakeTheme.muted)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Clear destination search")
            }
        }
        .padding(.horizontal, 14)
        .frame(minHeight: 48)
        .background(BrakeTheme.raised, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }

    /// Recents, suggestions or results: as tall as their rows, scrolling past `maxHeight`.
    private func fittedList(maxHeight: CGFloat) -> some View {
        ScrollView {
            placeList
                .background(GeometryReader { proxy in
                    Color.clear.preference(key: PlaceListHeight.self, value: proxy.size.height)
                })
        }
        .scrollIndicators(.hidden)
        .scrollDismissesKeyboard(.interactively)
        .frame(height: min(listHeight, maxHeight))
        .onPreferenceChange(PlaceListHeight.self) { listHeight = $0 }
    }

    @ViewBuilder private var placeList: some View {
        VStack(alignment: .leading, spacing: 0) {
            if trimmedQuery.isEmpty {
                recentList
            } else {
                ForEach(navigation.suggestions) { suggestion in
                    placeRow(suggestion.title, suggestion.subtitle, icon: "mappin.and.ellipse") {
                        searchFocused = false
                        navigation.chooseSuggestion(suggestion)
                    }
                }
                ForEach(navigation.results) { result in
                    placeRow(result.title, result.subtitle, icon: "mappin.circle.fill") {
                        searchFocused = false
                        navigation.chooseDestination(result)
                    }
                }
                if navigation.results.isEmpty && !navigation.isSearching {
                    placeRow("Search for “\(trimmedQuery)”", "", icon: "magnifyingglass") { submitSearch() }
                }
            }
        }
    }

    @ViewBuilder private var recentList: some View {
        if navigation.recentDestinations.isEmpty {
            Label("Search for a place or an address. Plan your route while parked.", systemImage: "info.circle")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                .padding(.vertical, 8)
        } else {
            HStack {
                Text("RECENT").font(.brake(size: 11, weight: .bold)).tracking(1.2).foregroundStyle(BrakeTheme.muted)
                Spacer()
                Button("Clear") { confirmingClearRecents = true }
                    .font(.brake(.caption, weight: .semibold))
                    .foregroundStyle(BrakeTheme.accentText)
                    .frame(minHeight: 32)
            }
            ForEach(navigation.recentDestinations.prefix(6)) { recent in
                HStack(spacing: 0) {
                    placeRow(recent.title, recent.subtitle, icon: "clock.arrow.circlepath") {
                        searchFocused = false
                        navigation.chooseRecent(recent)
                    }
                    Button { navigation.removeRecent(recent.id) } label: {
                        Image(systemName: "xmark").font(.brake(.caption, weight: .semibold))
                            .foregroundStyle(BrakeTheme.muted)
                            .frame(width: 44, height: 44)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Remove \(recent.title) from recents")
                }
            }
        }
    }

    private func placeRow(_ title: String, _ subtitle: String, icon: String, action: @escaping () -> Void) -> some View {
        Button {
            action()
            UISelectionFeedbackGenerator().selectionChanged()
        } label: {
            HStack(spacing: 12) {
                Image(systemName: icon)
                    .font(.brake(size: 17, weight: .semibold))
                    .foregroundStyle(BrakeTheme.accentText)
                    .frame(width: 28)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.brake(.subheadline, weight: .semibold)).foregroundStyle(BrakeTheme.text)
                        .lineLimit(2)
                    if !subtitle.isEmpty {
                        Text(subtitle).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted).lineLimit(2)
                    }
                }
                Spacer(minLength: 0)
            }
            .frame(maxWidth: .infinity, minHeight: 52, alignment: .leading)
            .overlay(alignment: .bottom) {
                Rectangle().fill(BrakeTheme.text.opacity(0.06)).frame(height: 1).padding(.leading, 40)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private var progressRow: some View {
        if let title = navigation.resolvingDestination {
            progressLine("Finding \(title)…")
        } else if navigation.isCalculating {
            progressLine("Finding your route…")
        }
    }

    private func progressLine(_ text: String) -> some View {
        HStack(spacing: 10) {
            ProgressView().tint(BrakeTheme.accentText)
            Text(text).font(.brake(.subheadline))
        }
        .frame(maxWidth: .infinity, minHeight: 40, alignment: .leading)
    }

    /// Service messages that need the driver's attention; routine progress lines stay hidden.
    private var statusNote: String? {
        let status = navigation.status
        let notable = ["No matching places", "Search is available", "Navigation is available"]
        return notable.contains { status.hasPrefix($0) } ? status : nil
    }

    private func errorRow(_ error: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(error, systemImage: "exclamationmark.triangle.fill")
                .font(.brake(.subheadline)).foregroundStyle(BrakeTheme.warning)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 18) {
                if navigation.destinationTitle != nil {
                    Button(navigation.isCalculating ? "Finding route…" : "Try again") { navigation.retryRoute() }
                        .disabled(navigation.isCalculating)
                }
                if error.contains("Settings") {
                    Button("Open Settings") {
                        if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
                    }
                }
            }
            .font(.brake(.subheadline, weight: .semibold))
            .foregroundStyle(BrakeTheme.accentText)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BrakeTheme.warning.opacity(0.1), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }

    private var routeCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: "mappin.circle.fill")
                    .font(.brake(size: 26, weight: .semibold))
                    .foregroundStyle(BrakeTheme.accentText)
                Text(navigation.destinationTitle ?? "Your destination")
                    .font(.brake(size: 20, weight: .bold))
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                Button { navigation.clearDestination() } label: {
                    Image(systemName: "xmark")
                        .font(.brake(size: 14, weight: .bold))
                        .foregroundStyle(BrakeTheme.muted)
                        .frame(width: 36, height: 36)
                        .background(BrakeTheme.raised, in: Circle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Clear planned destination")
                .accessibilityIdentifier("nav.clear")
            }
            journeyMetrics(time: navigation.route?.expectedTravelTime, distance: navigation.route?.distance)
            if navigation.routeUsesLastKnownLocation {
                Label("Starts at your last known GPS position", systemImage: "location.circle")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            }
            Button(action: startDrive) {
                Label("Start navigation", systemImage: "location.north.fill")
                    .font(.brake(size: 17, weight: .bold))
                    .frame(maxWidth: .infinity, minHeight: 54)
                    .foregroundStyle(BrakeTheme.background)
                    .background(BrakeTheme.accent, in: Capsule())
                    .shadow(color: BrakeTheme.buttonShadow, radius: 10, y: 5)
            }
            .buttonStyle(.plain)
            .disabled(!canStart)
            .opacity(canStart ? 1 : 0.4)
            .accessibilityIdentifier("nav.start")
            Toggle(isOn: $navigation.spokenDirections) {
                Label("Spoken directions", systemImage: "speaker.wave.2").font(.brake(.subheadline))
            }
            .tint(BrakeTheme.accent)
            if model.demo || model.replaying {
                Text("Stop the simulated drive or replay before starting navigation.")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.warning)
            } else if !model.tripActive {
                Text("Starting navigation also starts your camera trip.")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            }
        }
    }

    private func journeyMetrics(time: Double?, distance: Double?) -> some View {
        HStack(alignment: .top, spacing: 12) {
            journeyValue("Est. drive time", time.map(timeText) ?? "—")
            Spacer(minLength: 0)
            journeyValue("Distance", distance.map(distanceText) ?? "—")
            Spacer(minLength: 0)
            journeyValue("Est. arrival", time.map(arrivalText) ?? "—")
        }
    }

    private func journeyValue(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(value).font(.brake(size: 19, weight: .semibold)).monospacedDigit().lineLimit(1).minimumScaleFactor(0.7)
            Text(title).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
        }
        .accessibilityElement(children: .combine)
    }

    // MARK: - Guidance

    private var tripBar: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    if navigation.hasArrived {
                        Text("You’ve arrived").font(.brake(size: 22, weight: .bold))
                        Text(navigation.destinationTitle ?? "Destination reached")
                            .font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted).lineLimit(1)
                    } else {
                        (Text(navigation.remainingTime.map(arrivalText) ?? "—").font(.brake(size: 24, weight: .bold))
                            + Text(" arrival").font(.brake(size: 14, weight: .semibold)).foregroundColor(BrakeTheme.muted))
                            .monospacedDigit()
                            .lineLimit(1)
                        Text(remainingLine).font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
                            .monospacedDigit().lineLimit(1)
                    }
                }
                .accessibilityElement(children: .combine)
                Spacer(minLength: 0)
                muteButton
                endButton
            }
            if let error = navigation.errorMessage {
                errorRow(error)
            } else if let notice = navigation.routeNotice, !notice.isEmpty {
                Label(notice, systemImage: "exclamationmark.circle")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.warning)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var remainingLine: String {
        "\(navigation.remainingTime.map(timeText) ?? "—") · \(navigation.remainingDistance.map(distanceText) ?? "—")"
    }

    private var muteButton: some View {
        Button { navigation.spokenDirections.toggle() } label: {
            Image(systemName: navigation.spokenDirections ? "speaker.wave.2.fill" : "speaker.slash.fill")
                .font(.brake(size: 17, weight: .semibold))
                .foregroundStyle(navigation.spokenDirections ? BrakeTheme.text : BrakeTheme.warning)
                .frame(width: 48, height: 48)
                .background(BrakeTheme.raised, in: Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(navigation.spokenDirections ? "Mute spoken directions" : "Turn on spoken directions")
        .accessibilityIdentifier("nav.mute")
    }

    private var endButton: some View {
        Button { navigation.stopGuidance() } label: {
            Text(navigation.hasArrived ? "Done" : "End")
                .font(.brake(size: 16, weight: .bold))
                .foregroundStyle(navigation.hasArrived ? BrakeTheme.background : .white)
                .frame(minWidth: 76, minHeight: 48)
                .padding(.horizontal, 4)
                .background(navigation.hasArrived ? BrakeTheme.accent : BrakeTheme.danger, in: Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(navigation.hasArrived ? "Finish navigation" : "End navigation")
        .accessibilityHint("Your camera trip keeps running.")
        .accessibilityIdentifier("nav.end")
    }

    // MARK: - Formatting

    private func submitSearch() {
        guard !trimmedQuery.isEmpty && !navigation.isSearching else { return }
        searchFocused = false
        navigation.search()
    }

    /// Same rounding as the turn banner, so both say the same distance.
    private func distanceText(_ meters: Double) -> String {
        guard meters.isFinite else { return "—" }
        let value = NavigationBanner.formatted(meters, miles: useMPH)
        return "\(value.value) \(value.unit)"
    }

    private func timeText(_ seconds: Double) -> String {
        guard seconds.isFinite else { return "—" }
        if seconds <= 0 { return "0 min" }
        let minutes = max(1, Int(ceil(seconds / 60)))
        return minutes < 60 ? "\(minutes) min" : "\(minutes / 60) hr \(minutes % 60) min"
    }

    private func arrivalText(_ seconds: Double) -> String {
        guard seconds.isFinite else { return "—" }
        return Date().addingTimeInterval(max(0, seconds)).formatted(date: .omitted, time: .shortened)
    }
}

private struct PlaceListHeight: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

/// Pan and pinch work on the map itself (they win over the page pager, see NavigationMapSurface);
/// a horizontal swipe elsewhere on the page still changes pages. Attribution stays in the map footer.
private struct NavigationMapView: UIViewRepresentable {
    @Environment(\.colorScheme) private var colorScheme
    var route: MKRoute?
    var location: CLLocation?
    var locationIsCurrent = true
    var followsLocation: Bool
    var recenterTrigger: Int
    var topInset: CGFloat = 40
    var isVisible = true

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> MKMapView {
        let map = NavigationMapSurface()
        map.delegate = context.coordinator
        map.mapType = .standard
        map.pointOfInterestFilter = .excludingAll
        map.isScrollEnabled = true
        map.isZoomEnabled = true
        map.isRotateEnabled = false
        map.isPitchEnabled = false
        map.showsCompass = false
        map.showsScale = true
        map.showsUserLocation = false
        context.coordinator.attach(map)
        let pinch = UIPinchGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.observePinch(_:)))
        pinch.cancelsTouchesInView = false
        pinch.delegate = context.coordinator
        map.addGestureRecognizer(pinch)
        map.mapInteractionPinch = pinch
        let pan = UIPanGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.observePan(_:)))
        pan.maximumNumberOfTouches = 1
        pan.cancelsTouchesInView = false
        pan.delegate = context.coordinator
        map.addGestureRecognizer(pan)
        map.mapInteractionPan = pan
        return map
    }

    static func dismantleUIView(_ map: MKMapView, coordinator: Coordinator) {
        coordinator.displayLink?.invalidate()
        map.delegate = nil
    }

    func updateUIView(_ map: MKMapView, context: Context) {
        let style: UIUserInterfaceStyle = colorScheme == .dark ? .dark : .light
        let styleChanged = map.overrideUserInterfaceStyle != style
        map.overrideUserInterfaceStyle = style
        let coordinator = context.coordinator
        coordinator.displayLink?.isPaused = !isVisible
        coordinator.locationIsCurrent = locationIsCurrent
        let insetChanged = coordinator.topInset != topInset
        coordinator.topInset = topInset
        let routeChanged = coordinator.route !== route
        let recenter = coordinator.recenterTrigger != recenterTrigger
        let followChanged = coordinator.wasFollowing != followsLocation
        if routeChanged || recenter { coordinator.userPanned = false }
        if routeChanged {
            if let previous = coordinator.route { map.removeOverlay(previous.polyline) }
            if let destination = coordinator.destination { map.removeAnnotation(destination) }
            coordinator.destination = nil
            coordinator.route = route
            if let route {
                map.addOverlay(route.polyline)
                if route.polyline.pointCount > 0 {
                    let destination = MKPointAnnotation()
                    destination.coordinate = route.polyline.points()[route.polyline.pointCount - 1].coordinate
                    destination.title = "Destination"
                    coordinator.destination = destination
                    map.addAnnotation(destination)
                }
            }
        }
        if styleChanged || coordinator.accentName != BrakeTheme.accentName {
            coordinator.accentName = BrakeTheme.accentName
            if let route, let renderer = map.renderer(for: route.polyline) as? MKPolylineRenderer {
                renderer.strokeColor = BrakeTheme.accentUIColor(for: style)
                renderer.setNeedsDisplay()
            }
            if let destination = coordinator.destination,
               let marker = map.view(for: destination) as? MKMarkerAnnotationView {
                marker.markerTintColor = BrakeTheme.accentUIColor(for: style)
            }
        }
        let validLocation = location.flatMap { CLLocationCoordinate2DIsValid($0.coordinate) ? $0 : nil }
        if let location = validLocation {
            if coordinator.userPosition == nil {
                let annotation = MKPointAnnotation()
                annotation.title = "Your location"
                coordinator.userPosition = annotation
                map.addAnnotation(annotation)
            }
            if coordinator.lastLocationTimestamp != location.timestamp {
                let accepted = coordinator.smoother.ingest(NavigationCoordinate(latitude: location.coordinate.latitude,
                    longitude: location.coordinate.longitude), accuracy: location.horizontalAccuracy,
                    speed: location.speed, timestamp: location.timestamp.timeIntervalSince1970,
                    now: Date().timeIntervalSince1970, isCurrent: locationIsCurrent)
                if accepted, locationIsCurrent, location.speed >= 2, location.course >= 0,
                   location.course.isFinite, location.courseAccuracy >= 0, location.courseAccuracy <= 35 {
                    coordinator.targetHeading = location.course
                }
                if let point = coordinator.smoother.coordinate {
                    coordinator.userPosition?.coordinate = CLLocationCoordinate2D(latitude: point.latitude, longitude: point.longitude)
                    coordinator.updateAccuracyCircle(CLLocationCoordinate2D(latitude: point.latitude, longitude: point.longitude))
                }
            }
            coordinator.userPosition?.title = locationIsCurrent ? "Your location" : "Last known GPS location"
            if let annotation = coordinator.userPosition, let view = map.view(for: annotation) {
                let icon = coordinator.positionImage()
                if view.image !== icon { view.image = icon }
            }
        } else if let annotation = coordinator.userPosition {
            map.removeAnnotation(annotation)
            coordinator.userPosition = nil
            coordinator.smoother = NavigationMapSmoother()
            if let circle = coordinator.accuracyCircle { map.removeOverlay(circle) }
            coordinator.accuracyCircle = nil
        }
        coordinator.following = followsLocation && validLocation != nil
        if coordinator.following {
            coordinator.needsOverviewFit = false
            if followChanged || recenter || !coordinator.hasCentered {
                coordinator.followDistance = 700
                coordinator.followHeading = map.camera.heading
            }
            if !coordinator.hasCentered, let location = validLocation {
                map.setCamera(MKMapCamera(lookingAtCenter: location.coordinate, fromDistance: 700,
                    pitch: 45, heading: coordinator.targetHeading), animated: false)
            }
            coordinator.hasCentered = true
        } else if !followsLocation && !coordinator.userPanned && (routeChanged || recenter || insetChanged || !coordinator.hasCentered) {
            if route != nil {
                coordinator.needsOverviewFit = true
            } else if let location = validLocation {
                map.setRegion(MKCoordinateRegion(center: location.coordinate, latitudinalMeters: 1800, longitudinalMeters: 1800), animated: coordinator.hasCentered)
                coordinator.hasCentered = true
            }
        }
        coordinator.lastLocationTimestamp = validLocation?.timestamp
        coordinator.wasFollowing = followsLocation
        coordinator.recenterTrigger = recenterTrigger
    }

    @MainActor final class Coordinator: NSObject, @preconcurrency MKMapViewDelegate, UIGestureRecognizerDelegate {
        var userPanned = false
        var accentName = ""
        private var circleBudget = MapOverlayCoalescer()
        private var circleWasCurrent: Bool?
        private var positionImages: [Bool: UIImage] = [:]
        var route: MKRoute?
        var destination: MKPointAnnotation?
        var userPosition: MKPointAnnotation?
        var lastLocationTimestamp: Date?
        var recenterTrigger = 0
        var wasFollowing = false
        var hasCentered = false
        var locationIsCurrent = true
        var topInset: CGFloat = 40
        var smoother = NavigationMapSmoother()
        weak var map: MKMapView?
        var displayLink: CADisplayLink?
        var accuracyCircle: MKCircle?
        var following = false
        var needsOverviewFit = false
        var followDistance = 700.0
        var followHeading = 0.0
        var targetHeading = 0.0
        private var lastRender = 0.0
        private var lastCircleUpdate = 0.0
        private var applyingCamera = false
        private var userZooming = false
        private var lastBounds = CGSize.zero
        private var boundsChangedAt = 0.0

        @MainActor final class LinkTarget: NSObject {
            weak var owner: Coordinator?
            init(_ owner: Coordinator) { self.owner = owner }
            @objc func step(_ link: CADisplayLink) { owner?.render(at: link.timestamp) }
        }
        func attach(_ map: MKMapView) {
            self.map = map
            let link = CADisplayLink(target: LinkTarget(self), selector: #selector(LinkTarget.step(_:)))
            link.preferredFrameRateRange = CAFrameRateRange(minimum: 15, maximum: 30, preferred: 30)
            link.add(to: .main, forMode: .common)
            displayLink = link
        }
        private func render(at time: Double) {
            guard let map, map.window != nil else { lastRender = 0; return }
            let dt = lastRender == 0 ? 1 / 30.0 : min(0.1, time - lastRender)
            lastRender = time
            if map.bounds.size != lastBounds {
                lastBounds = map.bounds.size
                boundsChangedAt = time
                if !following, !userPanned, route != nil { needsOverviewFit = true }
            }
            if needsOverviewFit, !following, !userPanned, time - boundsChangedAt > 0.25,
               map.bounds.height > 20, map.bounds.width > 20, let route {
                needsOverviewFit = false
                map.setVisibleMapRect(route.polyline.boundingMapRect,
                    edgePadding: UIEdgeInsets(top: min(topInset, map.bounds.height * 0.4), left: 30, bottom: 44, right: 30),
                    animated: hasCentered)
                hasCentered = true
            }
            guard let point = smoother.advance(seconds: dt) else { return }
            let coordinate = CLLocationCoordinate2D(latitude: point.latitude, longitude: point.longitude)
            if let current = userPosition?.coordinate,
               abs(current.latitude - coordinate.latitude) + abs(current.longitude - coordinate.longitude) > 0.0000005 {
                userPosition?.coordinate = coordinate
            }
            if time - lastCircleUpdate > 0.25 {
                updateAccuracyCircle(coordinate)
                lastCircleUpdate = time
            }
            guard following, !userZooming, !userPanned else { return }
            let factor = 1 - exp(-dt / 0.65)
            let current = map.camera
            let longitudeDelta = (coordinate.longitude - current.centerCoordinate.longitude + 540).truncatingRemainder(dividingBy: 360) - 180
            let center = CLLocationCoordinate2D(latitude: current.centerCoordinate.latitude + (coordinate.latitude - current.centerCoordinate.latitude) * factor,
                longitude: (current.centerCoordinate.longitude + longitudeDelta * factor + 540).truncatingRemainder(dividingBy: 360) - 180)
            let turn = (targetHeading - followHeading + 540).truncatingRemainder(dividingBy: 360) - 180
            followHeading = (followHeading + turn * factor + 360).truncatingRemainder(dividingBy: 360)
            let distance = current.centerCoordinateDistance + (followDistance - current.centerCoordinateDistance) * factor
            let displacement = MapOverlayCoalescer.distance(
                NavigationCoordinate(latitude: current.centerCoordinate.latitude, longitude: current.centerCoordinate.longitude),
                NavigationCoordinate(latitude: center.latitude, longitude: center.longitude))
            let headingChange = abs((followHeading - current.heading + 540).truncatingRemainder(dividingBy: 360) - 180)
            guard displacement > 0.1 || abs(distance - current.centerCoordinateDistance) > 0.3
                || headingChange > 0.08 || abs(current.pitch - 45) > 0.03 else { return }
            applyingCamera = true
            map.setCamera(MKMapCamera(lookingAtCenter: center, fromDistance: max(180, distance),
                pitch: current.pitch + (45 - current.pitch) * factor, heading: followHeading), animated: false)
            applyingCamera = false
        }

        func updateAccuracyCircle(_ center: CLLocationCoordinate2D) {
            guard let map else { return }
            if accuracyCircle == nil { circleBudget.reset() }
            let replace = circleBudget.accept(center: NavigationCoordinate(latitude: center.latitude, longitude: center.longitude),
                                               radius: max(5, smoother.uncertaintyRadius))
            if !replace {
                if circleWasCurrent != locationIsCurrent, let circle = accuracyCircle,
                   let renderer = map.renderer(for: circle) as? MKCircleRenderer {
                    let color: UIColor = locationIsCurrent ? .systemBlue : .systemGray
                    renderer.fillColor = color.withAlphaComponent(0.12)
                    renderer.strokeColor = color.withAlphaComponent(0.35)
                    renderer.setNeedsDisplay()
                }
                circleWasCurrent = locationIsCurrent
                return
            }
            if let circle = accuracyCircle { map.removeOverlay(circle) }
            let circle = MKCircle(center: center, radius: max(5, smoother.uncertaintyRadius) + 5)
            accuracyCircle = circle
            circleWasCurrent = locationIsCurrent
            map.addOverlay(circle, level: .aboveRoads)
        }
        @objc func observePan(_ gesture: UIPanGestureRecognizer) {
            if gesture.state == .began || gesture.state == .changed {
                userPanned = true
                needsOverviewFit = false
            }
        }
        @objc func observePinch(_ gesture: UIPinchGestureRecognizer) {
            if gesture.state == .began || gesture.state == .changed { userZooming = true }
            else {
                if let map { followDistance = max(180, min(5000, map.camera.centerCoordinateDistance)) }
                userZooming = false
            }
        }
        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
            shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer) -> Bool {
            if let scroll = otherGestureRecognizer.view as? UIScrollView, scroll.isPagingEnabled { return false }
            return true
        }
        func positionImage() -> UIImage {
            if let cached = positionImages[locationIsCurrent] { return cached }
            let icon = UIGraphicsImageRenderer(size: CGSize(width: 26, height: 26)).image { context in
                let color: UIColor = locationIsCurrent ? .systemBlue : .systemGray
                color.setFill()
                context.cgContext.fillEllipse(in: CGRect(x: 3, y: 3, width: 20, height: 20))
                UIColor.white.setStroke()
                context.cgContext.setLineWidth(3)
                context.cgContext.strokeEllipse(in: CGRect(x: 3, y: 3, width: 20, height: 20))
            }
            positionImages[locationIsCurrent] = icon
            return icon
        }

        func mapView(_ mapView: MKMapView, rendererFor overlay: MKOverlay) -> MKOverlayRenderer {
            if let circle = overlay as? MKCircle {
                let renderer = MKCircleRenderer(circle: circle)
                let color: UIColor = locationIsCurrent ? .systemBlue : .systemGray
                renderer.fillColor = color.withAlphaComponent(0.12)
                renderer.strokeColor = color.withAlphaComponent(0.35)
                renderer.lineWidth = 1
                return renderer
            }
            guard let line = overlay as? MKPolyline else { return MKOverlayRenderer(overlay: overlay) }
            let renderer = MKPolylineRenderer(polyline: line)
            renderer.strokeColor = BrakeTheme.accentUIColor(for: mapView.traitCollection.userInterfaceStyle)
            renderer.lineWidth = 6
            return renderer
        }

        func mapView(_ mapView: MKMapView, viewFor annotation: MKAnnotation) -> MKAnnotationView? {
            let isUser = (annotation as AnyObject) === userPosition
            if isUser {
                let view = mapView.dequeueReusableAnnotationView(withIdentifier: "NavigationDot")
                    ?? MKAnnotationView(annotation: annotation, reuseIdentifier: "NavigationDot")
                view.annotation = annotation
                view.image = positionImage()
                view.displayPriority = .required
                view.collisionMode = .circle
                view.accessibilityLabel = locationIsCurrent ? "Approximate GPS location" : "Last known GPS location"
                return view
            }
            let identifier = isUser ? "NavigationPosition" : "NavigationDestination"
            let view = (mapView.dequeueReusableAnnotationView(withIdentifier: identifier) as? MKMarkerAnnotationView)
                ?? MKMarkerAnnotationView(annotation: annotation, reuseIdentifier: identifier)
            view.annotation = annotation
            view.markerTintColor = isUser ? (locationIsCurrent ? .systemBlue : .systemGray)
                : BrakeTheme.accentUIColor(for: mapView.traitCollection.userInterfaceStyle)
            view.glyphImage = UIImage(systemName: isUser ? "location.fill" : "flag.fill")
            view.displayPriority = .required
            view.canShowCallout = false
            return view
        }
    }
}

/// Map touches win over the ancestor page scroll view. Touches outside this
/// map (including the destination panel) remain available to the page pager.
private final class NavigationMapSurface: MKMapView {
    var mapInteractionPan: UIPanGestureRecognizer?
    var mapInteractionPinch: UIPinchGestureRecognizer?
    private weak var protectedPager: UIScrollView?
    override func didMoveToWindow() {
        super.didMoveToWindow()
        protectMapGestures()
    }
    override func didMoveToSuperview() {
        super.didMoveToSuperview()
        protectMapGestures()
    }
    private func protectMapGestures() {
        guard let pan = mapInteractionPan else { return }
        var ancestor = superview
        while let view = ancestor {
            if let pager = view as? UIScrollView, pager.isPagingEnabled {
                if protectedPager !== pager {
                    pager.panGestureRecognizer.require(toFail: pan)
                    if let pinch = mapInteractionPinch { pager.panGestureRecognizer.require(toFail: pinch) }
                    protectedPager = pager
                }
                return
            }
            ancestor = view.superview
        }
    }
}
