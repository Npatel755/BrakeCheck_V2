import CoreLocation
import BrakeCheckCore
import MapKit
import SwiftUI
import UIKit
import UniformTypeIdentifiers

/// The Navigate page. Planning: the map above a sheet with search, recents and the route card.
/// Guidance: the camera page's turn banner above the map and a trip bar (arrival, mute, end) below.
/// Landscape moves the sheet, banner and trip bar into a side column. The pieces are stacked rather
/// than layered, so no inset is guessed. It shares the driving model without owning its camera session.
struct NavigationView: View {
    @ObservedObject var navigation: NavigationService
    @ObservedObject var model: DriveModel
    var useMPH: Bool
    var startDrive: () -> Void
    var openCamera: () -> Void
    @FocusState private var searchFocused: Bool
    @State private var recenterTrigger = 0
    @State private var listHeight: CGFloat = 0
    @State private var confirmingClearRecents = false
    private enum PlannerSheet: String, Identifiable {
        case preferences, itinerary, places, map, lookAround, trafficHistory, coordinates, favorites, offline
        var id: String { rawValue }
    }
    @State private var presentedSheet: PlannerSheet?
    @State private var placeCategory: NavigationPlaceCategory?
    @State private var importingGraph = false
    @State private var importError: String?
    @State private var zoomCommand = 0
    @AppStorage("navigation.map.satellite") private var satellite = false
    @AppStorage("navigation.map.traffic") private var traffic = true
    @AppStorage("navigation.map.3d") private var threeDimensional = true
    @AppStorage("navigation.map.heading") private var headingUp = true
    @AppStorage("navigation.map.night") private var nightMode = "system"
    @AppStorage("navigation.map.routeColor") private var routeColor = "accent"
    @AppStorage("display.accent") private var accentChoice = BrakeTheme.defaultAccent

    private var canStart: Bool {
        navigation.canStartGuidance
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
        .animation(.easeInOut(duration: 0.22), value: navigation.hasPlannedRoute)
        .onChange(of: navigation.pageVisible) { _, visible in
            if !visible { searchFocused = false }
        }
        .onChange(of: navigation.isNavigating) { _, _ in
            searchFocused = false
            recenterTrigger += 1
        }
        .sheet(item: $presentedSheet) { plannerSheet($0) }
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
                NavigationMapView(route: navigation.route, routePath: navigation.routePath,
                    alternatives: navigation.alternatives, stops: navigation.itineraryStops,
                    location: navigation.mapLocation,
                    locationIsCurrent: navigation.hasFreshLocation && !navigation.positionEstimated,
                    positionIsEstimated: navigation.positionEstimated,
                    followsLocation: navigation.isNavigating && navigation.mapLocation != nil,
                    recenterTrigger: recenterTrigger,
                    topInset: safeTop + (displaysRisk ? 72 : 28),
                    isVisible: navigation.pageVisible && navigation.foreground,
                    satellite: satellite, traffic: traffic, threeDimensional: threeDimensional,
                    headingUp: headingUp, nightMode: nightMode, routeColor: routeColor, zoomCommand: zoomCommand,
                    nextTurnDistance: navigation.isNavigating && navigation.hasFreshLocation
                        && !navigation.isPaused && !navigation.userPaused && !navigation.positionEstimated && !navigation.hasArrived
                        ? navigation.maneuverDistance : nil)
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
                VStack(spacing: 8) {
                    mapControl("map", label: "Map and guidance options") { presentedSheet = .map }
                    mapControl("plus", label: "Zoom in") { zoomCommand += 1 }
                    mapControl("minus", label: "Zoom out") { zoomCommand -= 1 }
                    mapControl("location.north.fill", label: navigation.isNavigating
                        ? "Recenter on your position" : navigation.hasPlannedRoute
                        ? "Show the whole route" : "Center on your position") { recenterTrigger += 1 }
                        .accessibilityIdentifier("nav.recenter")
                }
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
        if navigation.hasPlannedRoute {
            // The route card must never hide its Start button: scroll only when it can't fit.
            ViewThatFits(in: .vertical) {
                routeCard
                ScrollView { routeCard }.scrollIndicators(.hidden)
            }
        } else {
            VStack(alignment: .leading, spacing: 10) {
                searchField
                destinationTools
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
                Button { navigation.toggleFavoriteCurrentDestination() } label: {
                    Image(systemName: currentDestinationSaved ? "star.fill" : "star")
                        .frame(width: 44, height: 44)
                }
                .buttonStyle(.plain).foregroundStyle(BrakeTheme.accentText)
                .accessibilityLabel(currentDestinationSaved ? "Remove saved destination" : "Save destination")
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
            journeyMetrics(time: navigation.activeRoute?.expectedTravelTime, distance: navigation.activeRoute?.distance)
            if let attribution = navigation.offlineAttribution, !attribution.isEmpty {
                Text(attribution).font(.brake(.caption2)).foregroundStyle(BrakeTheme.muted)
            }
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
            DisclosureGroup("Route choices, stops and options") {
                VStack(spacing: 12) {
                    alternateRoutesCard
                    plannerActions
                }.padding(.top, 8)
            }
            .font(.brake(.subheadline, weight: .semibold))
            progressRow
            if let error = navigation.errorMessage { errorRow(error) }
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
                        // "4:43 AM arrival", or just the time where the column is too narrow (landscape).
                        ViewThatFits(in: .horizontal) {
                            arrivalLine(labelled: true)
                            arrivalLine(labelled: false)
                        }
                        Text(remainingLine).font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
                            .monospacedDigit().lineLimit(1).minimumScaleFactor(0.8)
                    }
                }
                .layoutPriority(1)
                .accessibilityElement(children: .combine)
                Spacer(minLength: 0)
                muteButton
                endButton
            }
            HStack(spacing: 12) {
                journeyMenu
                Spacer(minLength: 0)
                Button(action: openCamera) { Label("Camera", systemImage: "video") }
                    .buttonStyle(.plain).frame(minHeight: 44)
            }.font(.brake(.subheadline, weight: .semibold))
            if let total = navigation.activeRoute?.distance, total > 0,
               let remaining = navigation.remainingDistance, remaining.isFinite {
                ProgressView(value: min(1, max(0, 1 - remaining / total)))
                    .tint(BrakeTheme.accent).accessibilityLabel("Route progress")
            }
            if navigation.stopAndGo {
                Label("Stop-and-go traffic detected", systemImage: "car.fill")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.warning)
            }
            trafficCard
            if let error = navigation.errorMessage {
                errorRow(error)
            } else if let notice = navigation.routeNotice, !notice.isEmpty {
                Label(notice, systemImage: "exclamationmark.circle")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.warning)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func arrivalLine(labelled: Bool) -> some View {
        let time = Text(navigation.remainingTime.map(arrivalText) ?? "—").font(.brake(size: 24, weight: .bold))
        let line = labelled
            ? time + Text(" arrival").font(.brake(size: 14, weight: .semibold)).foregroundColor(BrakeTheme.muted)
            : time
        return line.monospacedDigit().lineLimit(1).minimumScaleFactor(labelled ? 1 : 0.7)
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

    @ViewBuilder private func plannerSheet(_ sheet: PlannerSheet) -> some View {
        switch sheet {
        case .favorites: librarySheet("Saved places") { favoritesCard }
        case .offline: librarySheet("Offline routes") { offlineRoutesCard }
        case .preferences: NavigationRoutingPreferencesSheet(navigation: navigation)
        case .itinerary: NavigationItinerarySheet(navigation: navigation)
        case .places:
            NavigationPlaceSearchSheet(route: navigation.route, origin: navigation.mapLocation,
                initialCategory: placeCategory, actionTitle: navigation.hasPlannedRoute && !navigation.hasArrived ? "Add stop" : "Route here",
                select: { result in
                    if navigation.hasPlannedRoute && !navigation.hasArrived { navigation.addStop(result: result) }
                    else { navigation.chooseDestination(result) }
                }, routePath: navigation.routePath)
        case .map: NavigationMapOptions()
        case .coordinates: NavigationCoordinateDestinationSheet(navigation: navigation)
        case .trafficHistory: trafficHistorySheet
        case .lookAround:
            if let coordinate = navigation.destinationCoordinate {
                NavigationLookAroundView(coordinate: coordinate, title: navigation.destinationTitle ?? "Look Around")
            }
        }
    }

    private func mapControl(_ symbol: String, label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.brake(size: 17, weight: .semibold))
                .frame(width: 44, height: 44)
                .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
        }.buttonStyle(.plain).accessibilityLabel(label)
    }

    private var favoritesCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            if navigation.favorites.isEmpty {
                Label("Save a destination with the star on its route preview.", systemImage: "star")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted).padding(.vertical, 10)
            }
            ForEach(navigation.favorites) { favorite in
                HStack {
                    Button {
                        searchFocused = false
                        navigation.chooseFavorite(favorite)
                        presentedSheet = nil
                    } label: {
                        HStack(spacing: 12) {
                            Image(systemName: "star.fill").foregroundStyle(BrakeTheme.accent)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(favorite.title).font(.brake(.subheadline, weight: .semibold)).foregroundStyle(BrakeTheme.text)
                                Text(favorite.subtitle).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted).lineLimit(2)
                            }
                            Spacer(minLength: 0)
                        }.frame(minHeight: 48).contentShape(Rectangle())
                    }.buttonStyle(.plain)
                    Button { navigation.removeFavorite(id: favorite.id) } label: {
                        Image(systemName: "star.slash").frame(width: 44, height: 44)
                    }.buttonStyle(.plain).foregroundStyle(BrakeTheme.muted)
                        .accessibilityLabel("Remove \(favorite.title) from saved places")
                }
            }
        }
    }

    private var offlineRoutesCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Toggle("Use offline road graph", isOn: $navigation.preferOfflineRouting)
                .font(.brake(.subheadline)).disabled(!navigation.offlineGraphReady)
                .onChange(of: navigation.preferOfflineRouting) { _, _ in
                    if navigation.destinationTitle != nil { navigation.retryRoute() }
                }
            Label(navigation.offlineGraphName ?? "No offline road graph imported",
                  systemImage: navigation.offlineGraphReady ? "checkmark.circle" : "arrow.down.circle")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            if let warning = navigation.offlineImportWarning {
                Text(warning).font(.brake(.caption)).foregroundStyle(BrakeTheme.warning)
            }
            Button { importingGraph = true } label: {
                Label("Import offline roads (JSON)", systemImage: "square.and.arrow.down")
                    .font(.brake(.subheadline, weight: .semibold)).frame(minHeight: 44)
            }.buttonStyle(.plain)
            Button { presentedSheet = .coordinates } label: {
                Label("Route to coordinates", systemImage: "location.circle")
                    .font(.brake(.subheadline, weight: .semibold)).frame(minHeight: 44)
            }.buttonStyle(.plain)
            Text("Saved routes retain directions and progress. Offline rerouting needs an imported road graph covering your trip. Map imagery and place searches need a connection.")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            ForEach(navigation.savedRoutes) { saved in
                HStack(spacing: 8) {
                    Button {
                        searchFocused = false
                        navigation.loadSavedRoute(saved)
                        presentedSheet = nil
                    } label: {
                        HStack(spacing: 12) {
                            Image(systemName: "arrow.down.circle.fill").foregroundStyle(BrakeTheme.accent)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(saved.title).font(.brake(.subheadline, weight: .semibold)).foregroundStyle(BrakeTheme.text)
                                Text("\(distanceText(saved.distance)) · \(timeText(saved.expectedTravelTime))")
                                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                            }
                            Spacer(minLength: 0)
                        }.frame(maxWidth: .infinity, minHeight: 48).contentShape(Rectangle())
                    }.buttonStyle(.plain)
                    Button { navigation.removeSavedRoute(id: saved.id) } label: {
                        Image(systemName: "trash").frame(width: 44, height: 44)
                    }.buttonStyle(.plain).foregroundStyle(BrakeTheme.muted)
                        .accessibilityLabel("Remove saved route to \(saved.title)")
                }
            }
            if let attribution = navigation.offlineAttribution, !attribution.isEmpty {
                Text(attribution).font(.brake(.caption2)).foregroundStyle(BrakeTheme.muted)
            }
        }
    }

    private var placeCategories: some View {
        ScrollView(.horizontal) {
            HStack(spacing: 8) {
                ForEach(NavigationPlaceCategory.allCases) { category in
                    Button { openPlaceSearch(category) } label: {
                        Label(category.rawValue, systemImage: category.symbol)
                            .font(.brake(.caption, weight: .semibold)).padding(.horizontal, 12).frame(minHeight: 44)
                            .background(BrakeTheme.panel, in: Capsule())
                    }.buttonStyle(.plain)
                }
            }
        }.scrollIndicators(.hidden)
    }

    private func openPlaceSearch(_ category: NavigationPlaceCategory? = nil) {
        searchFocused = false
        placeCategory = category
        presentedSheet = .places
    }

    private var currentDestinationSaved: Bool {
        guard let coordinate = navigation.destinationCoordinate else { return false }
        return navigation.favorites.contains {
            abs($0.latitude - coordinate.latitude) < 0.0001 && abs($0.longitude - coordinate.longitude) < 0.0001
        }
    }

    @ViewBuilder private var alternateRoutesCard: some View {
        if navigation.alternatives.count > 1 {
            VStack(alignment: .leading, spacing: 10) {
                Text("Choose your route").font(.brake(.caption, weight: .semibold)).foregroundStyle(BrakeTheme.muted)
                ForEach(Array(navigation.alternatives.enumerated()), id: \.offset) { index, route in
                    Button { navigation.selectRoute(at: index); recenterTrigger += 1 } label: {
                        HStack(spacing: 10) {
                            Image(systemName: navigation.selectedRouteIndex == index ? "checkmark.circle.fill" : "circle")
                                .foregroundStyle(BrakeTheme.accent)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(route.name.isEmpty ? "Route \(index + 1)" : route.name)
                                    .font(.brake(.subheadline, weight: .semibold)).foregroundStyle(BrakeTheme.text).lineLimit(1)
                                Text("\(timeText(alternativeTime(index, route: route))) • \(distanceText(alternativeDistance(index, route: route)))")
                                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                            }
                            Spacer(minLength: 4)
                            Text(routeDifference(index, route: route)).font(.brake(.caption, weight: .semibold)).foregroundStyle(BrakeTheme.muted)
                        }.frame(minHeight: 48).contentShape(Rectangle())
                    }.buttonStyle(.plain)
                        .disabled(navigation.isCalculating)
                        .accessibilityAddTraits(navigation.selectedRouteIndex == index ? .isSelected : [])
                }
            }.brakePanel()
        }
    }

    private func alternativeTime(_ index: Int, route: MKRoute) -> Double {
        navigation.alternativeTravelTimes.indices.contains(index) ? navigation.alternativeTravelTimes[index] : route.expectedTravelTime
    }

    private func alternativeDistance(_ index: Int, route: MKRoute) -> Double {
        navigation.alternativeDistances.indices.contains(index) ? navigation.alternativeDistances[index] : route.distance
    }

    private func routeDifference(_ index: Int, route: MKRoute) -> String {
        let fastest = navigation.alternativeTravelTimes.min() ?? route.expectedTravelTime
        let difference = Int(((alternativeTime(index, route: route) - fastest) / 60).rounded())
        return difference <= 0 ? "Fastest" : "+\(difference) min"
    }

    private var currentRouteSaved: Bool {
        guard let route = navigation.activeRoute else { return false }
        return navigation.savedRoutes.contains { $0.id == route.id }
    }

    private var plannerActions: some View {
        VStack(spacing: 12) {
            HStack(spacing: 10) {
                plannerButton("Stops\(navigation.itineraryStops.isEmpty ? "" : " (\(navigation.itineraryStops.count))")", symbol: "point.topleft.down.curvedto.point.bottomright.up") { presentedSheet = .itinerary }
                plannerButton("Options", symbol: "slider.horizontal.3") { presentedSheet = .preferences }
                plannerButton("History", symbol: "clock.arrow.circlepath") { presentedSheet = .trafficHistory }
            }
            HStack(spacing: 10) {
                if navigation.destinationCoordinate != nil {
                    plannerButton("Look Around", symbol: "binoculars") { presentedSheet = .lookAround }
                }
                plannerButton(currentRouteSaved ? "Saved offline" : "Save offline", symbol: currentRouteSaved ? "checkmark.circle" : "arrow.down.circle") { navigation.saveCurrentRoute() }
                shareDestination
            }
            placeCategories
            if navigation.avoidTolls || navigation.avoidHighways || navigation.departureDate != nil {
                Text(preferencesDescription).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private func plannerButton(_ title: String, symbol: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: symbol).font(.brake(.caption, weight: .semibold))
                .frame(maxWidth: .infinity, minHeight: 44).background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 13))
        }.buttonStyle(.plain)
    }

    @ViewBuilder private var shareDestination: some View {
        if let text = navigation.shareText {
            ShareLink(item: text + (navigation.shareURL.map { "\n" + $0.absoluteString } ?? "")) {
                Label("Share", systemImage: "square.and.arrow.up").font(.brake(.caption, weight: .semibold))
                    .frame(maxWidth: .infinity, minHeight: 44).background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 13))
            }.buttonStyle(.plain).accessibilityLabel(navigation.isNavigating ? "Share destination and estimated arrival" : "Share destination")
        }
    }

    @ViewBuilder private var trafficCard: some View {
        if navigation.trafficNotice != nil || navigation.betterRoute != nil {
            VStack(alignment: .leading, spacing: 10) {
                if let notice = navigation.trafficNotice {
                    Label(notice, systemImage: "car.fill").font(.brake(.caption)).foregroundStyle(BrakeTheme.warning)
                }
                if let better = navigation.betterRoute {
                    Button { navigation.acceptBetterRoute() } label: {
                        Label("Use faster route • \(timeText(navigation.betterRouteTravelTime ?? better.expectedTravelTime))", systemImage: "arrow.triangle.branch")
                            .font(.brake(.subheadline, weight: .semibold)).frame(minHeight: 44)
                    }.buttonStyle(.plain).disabled(navigation.userPaused)
                }
            }.frame(maxWidth: .infinity, alignment: .leading).brakePanel()
        }
    }

    private var trafficHistorySheet: some View {
        SwiftUI.NavigationStack {
            List {
                Section {
                    Text("Completed drives saved on this phone help estimate the duration of similar trips and departure times.")
                        .font(.brake(.subheadline)).foregroundStyle(.secondary)
                    if navigation.trafficHistory.isEmpty {
                        ContentUnavailableView("No completed drives yet", systemImage: "clock.arrow.circlepath",
                            description: Text("Finish navigation to start building your local travel history."))
                    }
                }
                ForEach(navigation.trafficHistory.sorted { $0.departure > $1.departure }) { observation in
                    VStack(alignment: .leading, spacing: 5) {
                        Text(observation.departure.formatted(date: .abbreviated, time: .shortened))
                            .font(.brake(.subheadline, weight: .semibold))
                        Text("\(distanceText(observation.distance)) • \(timeText(observation.duration)) actual • \(timeText(observation.baselineDuration)) initial estimate")
                            .font(.brake(.caption)).foregroundStyle(.secondary)
                    }.padding(.vertical, 4)
                }
            }
            .scrollContentBackground(.hidden)
            .background(BrakeTheme.background)
            .foregroundStyle(BrakeTheme.text)
            .navigationTitle("Travel history").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { presentedSheet = nil } } }
        }.tint(BrakeTheme.accent)
    }

    private var preferencesDescription: String {
        var descriptions: [String] = []
        if navigation.avoidTolls { descriptions.append("Avoid tolls") }
        if navigation.avoidHighways { descriptions.append("Avoid highways") }
        if let departure = navigation.departureDate { descriptions.append("Leave \(departure.formatted(date: .abbreviated, time: .shortened))") }
        return descriptions.joined(separator: " • ")
    }

    private var destinationTools: some View {
        HStack(spacing: 16) {
            Button { presentedSheet = .favorites } label: { Label("Saved", systemImage: "star") }
            Button { presentedSheet = .offline } label: { Label("Offline", systemImage: "arrow.down.circle") }
            Menu {
                Button("Route options", systemImage: "slider.horizontal.3") { presentedSheet = .preferences }
                Button("Nearby places", systemImage: "mappin.and.ellipse") { openPlaceSearch() }
                Button("Route to coordinates", systemImage: "location.circle") { presentedSheet = .coordinates }
                Button("Travel history", systemImage: "clock.arrow.circlepath") { presentedSheet = .trafficHistory }
            } label: { Label("More", systemImage: "ellipsis.circle") }
        }
        .font(.brake(.caption, weight: .semibold)).buttonStyle(.plain)
        .frame(minHeight: 44)
    }

    private func librarySheet<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        SwiftUI.NavigationStack {
            ScrollView { content().padding(20) }
                .background(BrakeTheme.background).foregroundStyle(BrakeTheme.text)
                .navigationTitle(title).navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { presentedSheet = nil } } }
        }
        .tint(BrakeTheme.accent)
        .fileImporter(isPresented: $importingGraph, allowedContentTypes: [.json]) { result in
            do { navigation.importOfflineGraph(from: try result.get()) }
            catch { importError = error.localizedDescription }
        }
        .alert("Couldn’t import offline roads", isPresented: Binding(
            get: { importError != nil }, set: { if !$0 { importError = nil } })) {
            Button("OK", role: .cancel) { importError = nil }
        } message: { Text(importError ?? "") }
    }

    private var journeyMenu: some View {
        Menu {
            if !navigation.hasArrived {
                Button(navigation.userPaused ? "Resume navigation" : "Pause navigation",
                       systemImage: navigation.userPaused ? "play.fill" : "pause.fill") {
                    if navigation.userPaused { navigation.resumeGuidance() }
                    else { navigation.pauseGuidance() }
                }
                Button("Stops", systemImage: "point.topleft.down.curvedto.point.bottomright.up") { presentedSheet = .itinerary }
                Button("Add a stop", systemImage: "plus.circle") { openPlaceSearch() }
            }
            Button("Route options", systemImage: "slider.horizontal.3") { presentedSheet = .preferences }
            Button("Find parking", systemImage: "parkingsign.circle") { openPlaceSearch(.parking) }
            Button("Travel history", systemImage: "clock.arrow.circlepath") { presentedSheet = .trafficHistory }
            Button("Save offline", systemImage: "arrow.down.circle") { navigation.saveCurrentRoute() }
            if navigation.destinationCoordinate != nil {
                Button("Look Around", systemImage: "binoculars") { presentedSheet = .lookAround }
            }
            if let text = navigation.shareText {
                ShareLink(item: text + (navigation.shareURL.map { "\n" + $0.absoluteString } ?? "")) {
                    Label("Share destination and ETA", systemImage: "square.and.arrow.up")
                }
            }
        } label: { Label(navigation.userPaused ? "Paused · Options" : "Trip options", systemImage: "ellipsis.circle") }
        .accessibilityIdentifier("nav.options")
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
        return (navigation.isNavigating ? Date() : navigation.departureDate ?? Date()).addingTimeInterval(max(0, seconds)).formatted(date: .omitted, time: .shortened)
    }
}

private struct PlaceListHeight: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}
