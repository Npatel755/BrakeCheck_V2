import CoreLocation
import BrakeCheckCore
import MapKit
import SwiftUI
import UIKit
import UniformTypeIdentifiers

/// Route planning and guidance share the driving model, without owning its camera session.
struct NavigationView: View {
    @ObservedObject var navigation: NavigationService
    @ObservedObject var model: DriveModel
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    var useMPH: Bool
    var startDrive: () -> Void
    var openCamera: () -> Void
    @FocusState private var searchFocused: Bool
    @State private var recenterTrigger = 0
    @State private var panelExpanded = false
    private enum SearchSection: String { case search = "Search", recents = "Recents", favorites = "Saved", offline = "Offline" }
    @State private var searchSection: SearchSection = .search
    @State private var confirmingClearRecents = false
    private enum PlannerSheet: String, Identifiable {
        case preferences, itinerary, places, map, lookAround, trafficHistory, coordinates
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

    var body: some View {
        pageLayout
        .onChange(of: searchFocused) { _, focused in if focused { panelExpanded = true } }
        .onChange(of: navigation.pageVisible) { _, visible in
            if !visible { searchFocused = false }
        }
        .onChange(of: navigation.query) { _, _ in searchSection = .search }
        .onChange(of: searchSection) { _, section in
            if section != .search { searchFocused = false; panelExpanded = true }
        }
        .confirmationDialog("Clear recent destinations?", isPresented: $confirmingClearRecents, titleVisibility: .visible) {
            Button("Clear recents", role: .destructive) { navigation.clearRecents() }
        }
        .sheet(item: $presentedSheet) { plannerSheet($0) }
        .fileImporter(isPresented: $importingGraph, allowedContentTypes: [.json]) { result in
            do { navigation.importOfflineGraph(from: try result.get()) }
            catch { importError = error.localizedDescription }
        }
        .alert("Couldn’t import offline roads", isPresented: Binding(get: { importError != nil }, set: { if !$0 { importError = nil } })) {
            Button("OK", role: .cancel) { importError = nil }
        } message: { Text(importError ?? "") }
        .onChange(of: navigation.isNavigating) { _, _ in
            searchFocused = false
            panelExpanded = false
            recenterTrigger += 1
        }
        .background(BrakeTheme.background)
        .foregroundStyle(BrakeTheme.text)
        .tint(BrakeTheme.accentColor(named: accentChoice))
        .font(.brake(.body))
    }

    private var pageLayout: some View {
        GeometryReader { geometry in
            // A portrait keyboard reduces height; it must not switch us to the landscape layout.
            let landscape = geometry.size.width > 550 && geometry.size.width > geometry.size.height
            let panelHeight = min(geometry.size.height * 0.65,
                navigation.isNavigating ? (panelExpanded ? 440.0 : 228.0)
                    : panelExpanded || searchFocused ? 450.0 : navigation.hasPlannedRoute ? 310.0 : 230.0)
            let sideWidth = min(340.0, geometry.size.width * 0.46)
            ZStack(alignment: .topLeading) {
                NavigationMapView(route: navigation.route, routePath: navigation.routePath,
                    alternatives: navigation.alternatives, stops: navigation.itineraryStops,
                    location: navigation.mapLocation,
                    locationIsCurrent: navigation.hasFreshLocation && !navigation.positionEstimated,
                    positionIsEstimated: navigation.positionEstimated,
                    followsLocation: navigation.isNavigating && navigation.mapLocation != nil,
                    recenterTrigger: recenterTrigger,
                    topInset: navigation.isNavigating ? (displaysRisk ? 230 : 160) : (displaysRisk ? 150 : 90),
                    isVisible: navigation.pageVisible && navigation.foreground,
                    satellite: satellite, traffic: traffic, threeDimensional: threeDimensional,
                    headingUp: headingUp, nightMode: nightMode, routeColor: routeColor, zoomCommand: zoomCommand,
                    nextTurnDistance: navigation.isNavigating && navigation.hasFreshLocation
                        && !navigation.isPaused && !navigation.userPaused && !navigation.positionEstimated && !navigation.hasArrived
                        ? navigation.maneuverDistance : nil)
                    .overlay(alignment: .bottomTrailing) {
                        VStack(spacing: 8) {
                            mapControl("map", label: "Map and guidance options") { presentedSheet = .map }
                            mapControl("plus", label: "Zoom in") { zoomCommand += 1 }
                            mapControl("minus", label: "Zoom out") { zoomCommand -= 1 }
                            mapControl("location.north.fill", label: "Recenter map") { recenterTrigger += 1 }
                        }
                        .padding(.trailing, 16).padding(.bottom, 42)
                    }
                    .padding(.bottom, landscape ? 0 : panelHeight + 12)
                    .padding(.trailing, landscape ? sideWidth + 12 : 0)

                VStack(spacing: 8) {
                    header.padding(.vertical, 12)
                        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 22))
                    if displaysRisk {
                        RiskPill(risk: model.risk, critical: model.brakeWarningActive,
                                 meter: model.dangerMeter, large: false)
                            .padding(8)
                            .background(BrakeTheme.background.opacity(0.94), in: RoundedRectangle(cornerRadius: 18))
                    }
                    if navigation.isNavigating { maneuverCard }
                }
                .frame(width: landscape ? geometry.size.width - sideWidth - 36 : geometry.size.width - 24)
                .padding(12)

                VStack {
                    if !landscape { Spacer(minLength: 0) }
                    destinationPanel
                        .frame(width: landscape ? sideWidth : nil,
                               height: landscape ? max(100, geometry.size.height - 24) : panelHeight)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: landscape ? .trailing : .bottom)
                .padding(landscape ? 12 : 8)
            }
            .animation(.easeInOut(duration: 0.22), value: panelExpanded)
        }
    }

    @ViewBuilder private func plannerSheet(_ sheet: PlannerSheet) -> some View {
        switch sheet {
        case .preferences: NavigationRoutingPreferencesSheet(navigation: navigation)
        case .itinerary: NavigationItinerarySheet(navigation: navigation)
        case .places:
            NavigationPlaceSearchSheet(route: navigation.route, origin: navigation.mapLocation,
                initialCategory: placeCategory, actionTitle: navigation.hasPlannedRoute && !navigation.hasArrived ? "Add stop" : "Route here",
                select: { result in
                    if navigation.hasPlannedRoute && !navigation.hasArrived { navigation.addStop(result: result) }
                    else { navigation.chooseDestination(result) }
                    panelExpanded = true
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

    private var destinationPanel: some View {
        VStack(spacing: 0) {
            Button {
                searchFocused = false
                panelExpanded.toggle()
            } label: {
                VStack(spacing: 5) {
                    Capsule().fill(BrakeTheme.muted.opacity(0.5)).frame(width: 36, height: 4)
                    HStack {
                        Text(navigation.isNavigating ? "YOUR DRIVE" : "WHERE TO?")
                            .font(.brake(size: 10, weight: .bold)).tracking(2)
                        Spacer()
                        Image(systemName: panelExpanded ? "chevron.down" : "chevron.up")
                    }
                    .foregroundStyle(BrakeTheme.muted)
                }
                .padding(.horizontal, 18).frame(height: 44)
            }
            .buttonStyle(.plain)
            .accessibilityLabel(panelExpanded ? "Collapse navigation panel" : "Expand navigation panel")
            ScrollView {
                VStack(spacing: 12) {
                    if navigation.isNavigating {
                        activeJourneyCard
                        activeControls
                        if panelExpanded {
                            trafficCard
                            HStack(spacing: 10) {
                                shareDestination
                                plannerButton(navigation.hasArrived ? "Parking" : "Find a stop", symbol: navigation.hasArrived ? "parkingsign.circle" : "plus.circle") {
                                    openPlaceSearch(navigation.hasArrived ? .parking : nil)
                                }
                                plannerButton("Stops", symbol: "list.number") { presentedSheet = .itinerary }
                            }
                            if let error = navigation.errorMessage { activeErrorCard(error) }
                            Text(cameraTrackingText).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                        }
                    } else {
                        if !navigation.hasPlannedRoute { searchCard }
                        if let error = navigation.errorMessage { errorCard(error) }
                        if let title = navigation.resolvingDestination {
                            HStack { ProgressView(); Text("Finding \(title)…").font(.brake(.subheadline)) }
                                .frame(maxWidth: .infinity, alignment: .leading).brakePanel()
                        } else if navigation.isCalculating {
                            HStack { ProgressView(); Text("Finding your route…").font(.brake(.subheadline)) }
                                .frame(maxWidth: .infinity, alignment: .leading).brakePanel()
                        } else if navigation.destinationTitle != nil && !navigation.hasPlannedRoute {
                            Text(navigation.status).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                        }
                        if navigation.hasPlannedRoute {
                            routePreview
                            startControls
                            alternateRoutesCard
                            plannerActions
                            Text(navigation.status).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        if navigation.query.isEmpty && !navigation.hasPlannedRoute {
                            placeCategories
                            Text("A clear route. A calmer drive.")
                                .font(.brake(.headline)).foregroundStyle(BrakeTheme.accent)
                            Text("Search for a destination while parked. Suggestions appear as you type.")
                                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                        }
                    }
                }
                .padding(.horizontal, 12).padding(.bottom, 14)
            }
            .scrollIndicators(.hidden)
            .scrollDismissesKeyboard(.interactively)
        }
        .background(BrakeTheme.background.opacity(0.97), in: RoundedRectangle(cornerRadius: 26))
        .overlay(RoundedRectangle(cornerRadius: 26).stroke(BrakeTheme.accent.opacity(0.15), lineWidth: 1))
        .shadow(color: .black.opacity(0.35), radius: 18, y: -4)
    }

    private var header: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 5) {
                Label("Navigation", systemImage: "location.north.line.fill")
                    .font(.brake(size: 21, weight: .semibold))
                    .foregroundStyle(BrakeTheme.text)
                HStack(spacing: 5) {
                    Circle().fill(cameraTrackingColor).frame(width: 5, height: 5)
                    Text(cameraTrackingText).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                }
            }
            Spacer(minLength: 0)
            Button(action: openCamera) {
                Label("Camera", systemImage: "video")
                    .font(.brake(.subheadline, weight: .semibold))
                    .padding(.horizontal, 13)
                    .frame(minHeight: 44)
                    .background(BrakeTheme.panel, in: Capsule())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Open Camera page")
        }
        .padding(.horizontal, 20)
    }

    private var cameraTrackingText: String {
        if model.demo { return "Simulated driving readings" }
        if model.replaying { return "Recorded drive playing" }
        if model.stopping { return "Saving camera recording" }
        if !model.tripActive { return "Camera tracking is off" }
        if model.cameraReady && !model.changingCameraGeometry { return "Camera tracking active" }
        let status = model.cameraStatus.lowercased()
        if model.changingCameraGeometry || status.contains("paused") || status.contains("interrupted") || status.contains("denied") {
            return "Camera tracking paused"
        }
        return "Camera tracking starting"
    }

    private var cameraTrackingColor: Color {
        model.tripActive && model.cameraReady && !model.changingCameraGeometry ? BrakeTheme.accent : BrakeTheme.muted
    }

    private var searchCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Image(systemName: "magnifyingglass").foregroundStyle(BrakeTheme.muted)
                TextField("Address, place, or city", text: $navigation.query)
                    .font(.brake(.subheadline))
                    .textInputAutocapitalization(.words)
                    .autocorrectionDisabled(false)
                    .submitLabel(.search)
                    .focused($searchFocused)
                    .onSubmit { submitSearch() }
                    .accessibilityLabel("Destination search")
                if navigation.isSearching || navigation.suggestionsLoading {
                    ProgressView().tint(BrakeTheme.accent)
                } else if !navigation.query.isEmpty {
                    Button { navigation.query = "" } label: {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(BrakeTheme.muted)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Clear destination search")
                }
            }
            .padding(13)
            .background(BrakeTheme.background, in: RoundedRectangle(cornerRadius: 12))
            Picker("Destination options", selection: $searchSection) {
                Text("Search").tag(SearchSection.search)
                Text("Recents").tag(SearchSection.recents)
                Text("Saved").tag(SearchSection.favorites)
                Text("Offline").tag(SearchSection.offline)
            }.pickerStyle(.segmented)
            if searchSection == .recents {
                recentDestinationsCard
            } else if searchSection == .favorites {
                favoritesCard
            } else if searchSection == .offline {
                offlineRoutesCard
            } else {
            if let error = navigation.suggestionError {
                Text(error).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            }
            ForEach(navigation.suggestions) { suggestion in
                Button {
                    searchFocused = false
                    panelExpanded = true
                    navigation.chooseSuggestion(suggestion)
                    UISelectionFeedbackGenerator().selectionChanged()
                } label: {
                    HStack(spacing: 12) {
                        Image(systemName: "mappin.and.ellipse")
                            .foregroundStyle(BrakeTheme.accent)
                            .frame(width: 28)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(suggestion.title).font(.brake(.subheadline, weight: .semibold)).foregroundStyle(BrakeTheme.text)
                            if !suggestion.subtitle.isEmpty {
                                Text(suggestion.subtitle).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted).lineLimit(2)
                            }
                        }
                        Spacer(minLength: 0)
                        Image(systemName: "arrow.up.left").font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                    }
                    .frame(maxWidth: .infinity, minHeight: 48, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityElement(children: .combine)
                Divider().overlay(BrakeTheme.muted.opacity(0.15))
            }
            Button(action: submitSearch) {
                Text(navigation.isSearching ? "Searching…" : "Search destinations")
                    .font(.brake(.subheadline, weight: .semibold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .background(BrakeTheme.accent.opacity(0.1), in: Capsule())
            }
            .buttonStyle(.plain)
            .disabled(navigation.query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || navigation.isSearching)
            .opacity(navigation.query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || navigation.isSearching ? 0.4 : 1)
            ForEach(navigation.results) { result in
                Button {
                    searchFocused = false
                    navigation.chooseDestination(result)
                    UISelectionFeedbackGenerator().selectionChanged()
                } label: {
                    HStack(spacing: 12) {
                        Image(systemName: "mappin.circle.fill").foregroundStyle(BrakeTheme.amber)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(result.title).font(.brake(.subheadline, weight: .semibold)).foregroundStyle(BrakeTheme.text)
                            if !result.subtitle.isEmpty {
                                Text(result.subtitle).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        Spacer(minLength: 0)
                        Image(systemName: "chevron.right").font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                    }
                    .padding(.vertical, 10)
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityElement(children: .combine)
            }
            }
        }
        .brakePanel()
    }

    private var recentDestinationsCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            if navigation.recentDestinations.isEmpty {
                Label("No recent destinations", systemImage: "clock")
                    .foregroundStyle(BrakeTheme.muted).padding(.vertical, 12)
            } else {
                ForEach(navigation.recentDestinations) { recent in
                    HStack(spacing: 8) {
                        Button {
                            searchFocused = false; panelExpanded = true
                            navigation.chooseRecent(recent)
                            UISelectionFeedbackGenerator().selectionChanged()
                        } label: {
                            HStack(spacing: 12) {
                                Image(systemName: "clock.arrow.circlepath").foregroundStyle(BrakeTheme.accent)
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(recent.title).font(.brake(.subheadline, weight: .semibold)).foregroundStyle(BrakeTheme.text)
                                    if !recent.subtitle.isEmpty {
                                        Text(recent.subtitle).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted).lineLimit(2)
                                    }
                                }
                                Spacer(minLength: 0)
                            }.frame(minHeight: 48).contentShape(Rectangle())
                        }.buttonStyle(.plain)
                        Button { navigation.removeRecent(recent.id) } label: {
                            Image(systemName: "xmark").font(.brake(.caption)).frame(width: 44, height: 44)
                        }.buttonStyle(.plain).foregroundStyle(BrakeTheme.muted)
                            .accessibilityLabel("Remove \(recent.title) from recents")
                    }
                }
                Button("Clear recents") { confirmingClearRecents = true }
                    .font(.brake(.caption)).frame(minHeight: 44)
            }
        }
    }

    private func submitSearch() {
        guard !navigation.query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !navigation.isSearching else { return }
        searchFocused = false
        navigation.search()
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
                        panelExpanded = true
                    } label: {
                        HStack(spacing: 12) {
                            Image(systemName: "arrow.down.circle.fill").foregroundStyle(BrakeTheme.accent)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(saved.title).font(.brake(.subheadline, weight: .semibold)).foregroundStyle(BrakeTheme.text)
                                Text("\(distanceText(saved.distance)) ? \(timeText(saved.expectedTravelTime))")
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

    private var routePreview: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: "mappin.and.ellipse").foregroundStyle(BrakeTheme.amber)
                Text(navigation.destinationTitle ?? "Your destination")
                    .font(.brake(.headline))
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                Button { navigation.toggleFavoriteCurrentDestination() } label: {
                    Image(systemName: currentDestinationSaved ? "star.fill" : "star")
                        .font(.brake(.subheadline, weight: .semibold)).frame(width: 44, height: 44)
                }.buttonStyle(.plain).foregroundStyle(BrakeTheme.accent)
                    .accessibilityLabel(currentDestinationSaved ? "Remove saved destination" : "Save destination")
                Button { navigation.clearDestination() } label: {
                    Image(systemName: "xmark").font(.brake(.subheadline, weight: .semibold)).frame(width: 32, height: 32)
                }
                .buttonStyle(.plain)
                .foregroundStyle(BrakeTheme.muted)
                .accessibilityLabel("Clear planned destination")
            }
            journeyMetrics(time: navigation.activeRoute?.expectedTravelTime, distance: navigation.activeRoute?.distance)
            if let attribution = navigation.offlineAttribution, !attribution.isEmpty {
                Text(attribution).font(.brake(.caption2)).foregroundStyle(BrakeTheme.muted)
            }
            if navigation.routeUsesLastKnownLocation {
                Label("Route starts at your last known GPS location", systemImage: "location.circle")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            }
        }
        .brakePanel()
    }

    private var startControls: some View {
        VStack(alignment: .leading, spacing: 12) {
            Toggle("Spoken directions", isOn: $navigation.spokenDirections)
                .font(.brake(.subheadline))
            Button(action: startDrive) {
                Label("Start navigation", systemImage: "location.north.fill")
                    .font(.brake(size: 16, weight: .bold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 15)
                    .foregroundStyle(BrakeTheme.background)
                    .background(BrakeTheme.accent, in: Capsule())
            }
            .buttonStyle(.plain)
            .disabled(!canStart)
            .opacity(canStart ? 1 : 0.4)
            if model.demo || model.replaying {
                Text("Stop the simulated drive or replay before starting navigation.")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.warning)
            } else if !model.tripActive {
                Text("Starting navigation also starts your camera trip.")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            }
        }
        .brakePanel()
    }

    private var maneuverCard: some View {
        HStack(alignment: .center, spacing: 14) {
            Image(systemName: navigation.hasArrived ? "checkmark.circle.fill" : navigation.isRejoiningRoute
                  ? "arrow.triangle.turn.up.right.diamond" : NavigationManeuver.symbol(for: navigation.instruction))
                .font(.brake(size: 25, weight: .semibold))
                .foregroundStyle(BrakeTheme.accent)
                .frame(width: 32)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 5) {
                if navigation.hasArrived {
                    Text("You’ve arrived").font(.brake(.headline)).foregroundStyle(BrakeTheme.accent)
                    Text(navigation.destinationTitle ?? "Destination reached.")
                        .font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                } else if navigation.isRejoiningRoute {
                    HStack {
                        if navigation.isCalculating { ProgressView().tint(BrakeTheme.accent) }
                        Text(navigation.isCalculating ? "Finding a new route…" : "Rejoining your route")
                            .font(.brake(.headline))
                    }
                    Text(navigation.destinationTitle ?? "Your destination stays selected.")
                        .font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
                } else {
                    if let distance = navigation.maneuverDistance, distance.isFinite {
                        Text(distance < 20 && !navigation.isPaused ? "Now" : "In \(distanceText(distance))")
                            .font(.brake(size: 22, weight: .semibold)).monospacedDigit()
                    }
                    Text(navigation.instruction.isEmpty ? "Follow the route" : navigation.instruction)
                        .font(.brake(.headline))
                        .fixedSize(horizontal: false, vertical: true)
                    if let next = navigation.upcomingInstruction {
                        Label("Then \(next)", systemImage: NavigationManeuver.symbol(for: next))
                            .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                            .lineLimit(2)
                    }
                    if let notice = navigation.routeNotice, !notice.isEmpty {
                        Text(notice).font(.brake(.caption)).foregroundStyle(BrakeTheme.warning)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .overlay(alignment: .topTrailing) {
            if navigation.userPaused {
                Text("Paused").font(.brake(.caption, weight: .semibold)).padding(6)
                    .background(BrakeTheme.raised, in: Capsule())
            }
        }
        .brakePanel()
    }

    private var activeJourneyCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let destination = navigation.destinationTitle {
                Text(destination).font(.brake(.caption, weight: .semibold)).foregroundStyle(BrakeTheme.muted).lineLimit(1)
            }
            journeyMetrics(time: navigation.hasArrived ? 0 : navigation.remainingTime,
                           distance: navigation.hasArrived ? 0 : navigation.remainingDistance)
            if let total = navigation.activeRoute?.distance, total > 0,
               let remaining = navigation.remainingDistance, remaining.isFinite {
                ProgressView(value: min(1, max(0, 1 - remaining / total)))
                    .tint(BrakeTheme.accent).accessibilityLabel("Route progress")
            }
            if navigation.stopAndGo {
                Label("Stop-and-go traffic detected", systemImage: "car.fill")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.warning)
            } else if let notice = navigation.trafficNotice {
                Label(notice, systemImage: "car.fill").font(.brake(.caption)).foregroundStyle(BrakeTheme.warning).lineLimit(2)
            }
        }
        .brakePanel()
    }

    private var activeControls: some View {
        let layout = dynamicTypeSize.isAccessibilitySize
            ? AnyLayout(VStackLayout(spacing: 8)) : AnyLayout(HStackLayout(spacing: 10))
        return layout {
            Button { navigation.spokenDirections.toggle() } label: {
                Label(navigation.spokenDirections ? "Mute" : "Unmute", systemImage: navigation.spokenDirections ? "speaker.wave.2" : "speaker.slash")
                    .font(.brake(.subheadline, weight: .semibold))
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .background(BrakeTheme.panel, in: Capsule())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(navigation.spokenDirections ? "Mute spoken directions" : "Enable spoken directions")
            if !navigation.hasArrived {
                Button {
                    if navigation.userPaused { navigation.resumeGuidance() }
                    else { navigation.pauseGuidance() }
                } label: {
                    Label(navigation.userPaused ? "Resume" : "Pause", systemImage: navigation.userPaused ? "play.fill" : "pause.fill")
                        .font(.brake(.subheadline, weight: .semibold))
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .background(BrakeTheme.panel, in: Capsule())
                }.buttonStyle(.plain)
            }
            Button { navigation.stopGuidance() } label: {
                Label(navigation.hasArrived ? "Done" : "Stop", systemImage: navigation.hasArrived ? "checkmark" : "stop.fill")
                    .font(.brake(.subheadline, weight: .semibold))
                    .foregroundStyle(BrakeTheme.danger)
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .background(BrakeTheme.danger.opacity(0.12), in: Capsule())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(navigation.hasArrived ? "Finish navigation" : "Stop navigation")
            .accessibilityHint("Your camera trip continues.")
        }
    }

    private func errorCard(_ error: String) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Label(error, systemImage: "exclamationmark.triangle")
                .font(.brake(.subheadline)).foregroundStyle(BrakeTheme.warning)
                .fixedSize(horizontal: false, vertical: true)
            if navigation.destinationTitle != nil {
                Button("Try route again") { navigation.retryRoute() }
                    .font(.brake(.subheadline, weight: .semibold))
                    .disabled(navigation.isCalculating)
            }
        }
        .brakePanel()
    }

    private func activeErrorCard(_ error: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(error).font(.brake(.caption)).foregroundStyle(BrakeTheme.warning)
                .lineLimit(3).fixedSize(horizontal: false, vertical: true)
            Button { navigation.retryRoute() } label: {
                Label(navigation.isCalculating ? "Finding route…" : "Try route again", systemImage: "arrow.clockwise")
                    .font(.brake(.caption, weight: .semibold)).frame(minHeight: 32)
            }
            .buttonStyle(.plain)
            .disabled(navigation.isCalculating)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 14))
    }

    private func journeyMetrics(time: Double?, distance: Double?) -> some View {
        HStack(alignment: .top, spacing: 12) {
            journeyValue("Remaining", value: distance.map(distanceText) ?? "—")
            Spacer(minLength: 0)
            journeyValue("Est. drive time", value: time.map(timeText) ?? "—")
            Spacer(minLength: 0)
            journeyValue("Est. arrival", value: navigation.hasArrived ? "Arrived" : time.map { (navigation.isNavigating ? Date() : max(Date(), navigation.departureDate ?? Date())).addingTimeInterval($0).formatted(date: .omitted, time: .shortened) } ?? "—")
        }
    }

    private func journeyValue(_ title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(value).font(.brake(size: 18, weight: .semibold)).monospacedDigit().lineLimit(1).minimumScaleFactor(0.7)
            Text(title).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
        }
        .accessibilityElement(children: .combine)
    }

    private func distanceText(_ meters: Double) -> String {
        guard meters.isFinite else { return "—" }
        let value = max(0, meters)
        if useMPH {
            return value >= 160.9344 ? String(format: "%.1f mi", value / 1609.344)
                : String(format: "%.0f ft", value * 3.280839895)
        }
        return value >= 1000 ? String(format: "%.1f km", value / 1000) : String(format: "%.0f m", value)
    }

    private func timeText(_ seconds: Double) -> String {
        guard seconds.isFinite else { return "—" }
        if seconds <= 0 { return "0 min" }
        let minutes = max(1, Int(ceil(max(0, seconds) / 60)))
        return minutes < 60 ? "\(minutes) min" : "\(minutes / 60) hr \(minutes % 60) min"
    }
}
