import CoreLocation
import BrakeCheckCore
import MapKit
import SwiftUI
import UIKit

/// Route planning and guidance share the driving model, without owning its camera session.
struct NavigationView: View {
    @ObservedObject var navigation: NavigationService
    @ObservedObject var model: DriveModel
    var useMPH: Bool
    var startDrive: () -> Void
    var openCamera: () -> Void
    @FocusState private var searchFocused: Bool
    @State private var recenterTrigger = 0
    @State private var panelExpanded = false
    private enum SearchSection: String { case search = "Search", recents = "Recents" }
    @State private var searchSection: SearchSection = .search
    @State private var confirmingClearRecents = false
    @AppStorage("display.accent") private var accentChoice = BrakeTheme.defaultAccent

    private var canStart: Bool {
        navigation.route != nil && !navigation.isCalculating && navigation.mapLocation != nil
            && !model.demo && !model.replaying && !model.stopping
    }

    private var displaysRisk: Bool {
        model.tripActive || model.brakeWarningActive || model.demo || model.replaying
    }

    var body: some View {
        GeometryReader { geometry in
            // A portrait keyboard reduces height; it must not switch us to the landscape layout.
            let landscape = geometry.size.width > 550 && geometry.size.width > geometry.size.height
            let panelHeight = min(geometry.size.height * 0.65,
                navigation.isNavigating ? (panelExpanded ? 310.0 : 164.0)
                    : panelExpanded || searchFocused ? 450.0 : navigation.route != nil ? 310.0 : 230.0)
            let sideWidth = min(340.0, geometry.size.width * 0.46)
            ZStack(alignment: .topLeading) {
                NavigationMapView(route: navigation.route, location: navigation.mapLocation,
                    locationIsCurrent: navigation.hasFreshLocation,
                    followsLocation: navigation.isNavigating && navigation.mapLocation != nil,
                    recenterTrigger: recenterTrigger,
                    topInset: navigation.isNavigating ? (displaysRisk ? 230 : 160) : (displaysRisk ? 150 : 90),
                    isVisible: navigation.pageVisible && navigation.foreground)
                    .overlay(alignment: .bottomTrailing) {
                        Button { recenterTrigger += 1 } label: {
                            Image(systemName: "location.north.fill")
                                .font(.brake(size: 18, weight: .semibold))
                                .frame(width: 48, height: 48)
                                .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16))
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Recenter map")
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
        .onChange(of: searchFocused) { _, focused in if focused { panelExpanded = true } }
        .onChange(of: navigation.pageVisible) { _, visible in
            if !visible { searchFocused = false }
        }
        .onChange(of: navigation.query) { _, _ in searchSection = .search }
        .onChange(of: searchSection) { _, section in
            if section == .recents { searchFocused = false; panelExpanded = true }
        }
        .confirmationDialog("Clear recent destinations?", isPresented: $confirmingClearRecents, titleVisibility: .visible) {
            Button("Clear recents", role: .destructive) { navigation.clearRecents() }
        }
        .onChange(of: navigation.isNavigating) { _, _ in
            searchFocused = false
            panelExpanded = false
            recenterTrigger += 1
        }
        .background(BrakeTheme.background)
        .foregroundStyle(BrakeTheme.text)
        .tint(BrakeTheme.accentColor(named: accentChoice))
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
                        if panelExpanded {
                            activeControls
                            if let error = navigation.errorMessage { activeErrorCard(error) }
                            Text(cameraTrackingText).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                        }
                    } else {
                        if navigation.route == nil { searchCard }
                        if let error = navigation.errorMessage { errorCard(error) }
                        if let title = navigation.resolvingDestination {
                            HStack { ProgressView(); Text("Finding \(title)…").font(.brake(.subheadline)) }
                                .frame(maxWidth: .infinity, alignment: .leading).brakePanel()
                        } else if navigation.isCalculating {
                            HStack { ProgressView(); Text("Finding your route…").font(.brake(.subheadline)) }
                                .frame(maxWidth: .infinity, alignment: .leading).brakePanel()
                        } else if navigation.destinationTitle != nil && navigation.route == nil {
                            Text(navigation.status).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                        }
                        if navigation.route != nil { routePreview; startControls }
                        if navigation.query.isEmpty && navigation.route == nil {
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

    private func planningLayout(landscape: Bool) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                searchCard
                if let error = navigation.errorMessage { errorCard(error) }
                if navigation.isCalculating {
                    HStack(spacing: 10) {
                        ProgressView().tint(BrakeTheme.accent)
                        Text("Finding a driving route…").font(.brake(.subheadline))
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .brakePanel()
                }
                if navigation.route != nil {
                    routePreview
                    routeMap(follow: false)
                        .frame(height: landscape ? 240 : 280)
                        .clipShape(RoundedRectangle(cornerRadius: 20))
                    startControls
                } else if !navigation.isCalculating && navigation.results.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Plan your next drive.").font(.brake(.headline))
                        Text("Search for a destination while parked. Your camera tracking stays on when you switch pages during a trip.")
                            .font(.brake(.subheadline))
                            .foregroundStyle(BrakeTheme.muted)
                    }
                    .brakePanel()
                }
                if !navigation.status.isEmpty {
                    Text(navigation.status)
                        .font(.brake(.caption))
                        .foregroundStyle(BrakeTheme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Text("Maps and route searches require a network connection.")
                    .font(.brake(.caption))
                    .foregroundStyle(BrakeTheme.muted)
            }
            .padding(.horizontal, 20)
            .padding(.bottom, 20)
            .frame(maxWidth: 900)
            .frame(maxWidth: .infinity)
        }
        .scrollIndicators(.hidden)
        .scrollDismissesKeyboard(.interactively)
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
            }.pickerStyle(.segmented)
            if searchSection == .recents {
                recentDestinationsCard
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

    private var routePreview: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: "mappin.and.ellipse").foregroundStyle(BrakeTheme.amber)
                Text(navigation.destinationTitle ?? "Your destination")
                    .font(.brake(.headline))
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                Button { navigation.clearDestination() } label: {
                    Image(systemName: "xmark").font(.brake(.subheadline, weight: .semibold)).frame(width: 32, height: 32)
                }
                .buttonStyle(.plain)
                .foregroundStyle(BrakeTheme.muted)
                .accessibilityLabel("Clear planned destination")
            }
            journeyMetrics(time: navigation.route?.expectedTravelTime, distance: navigation.route?.distance)
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

    @ViewBuilder private func guidanceLayout(landscape: Bool) -> some View {
        if landscape {
            HStack(alignment: .top, spacing: 12) {
                routeMap(follow: navigation.mapLocation != nil)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .clipShape(RoundedRectangle(cornerRadius: 16))
                ScrollView {
                    VStack(spacing: 12) {
                        maneuverCard
                        if let error = navigation.errorMessage { activeErrorCard(error) }
                        activeJourneyCard
                        activeControls
                    }
                    .padding(.bottom, 8)
                }
                .scrollIndicators(.hidden)
                .frame(width: 290)
            }
            .padding(.horizontal, 12)
            .padding(.bottom, 8)
        } else {
            VStack(spacing: 10) {
                maneuverCard.padding(.horizontal, 16)
                routeMap(follow: navigation.mapLocation != nil)
                .frame(minHeight: 140, maxHeight: .infinity)
                VStack(spacing: 10) {
                    if let error = navigation.errorMessage { activeErrorCard(error) }
                    activeJourneyCard
                    activeControls
                }
                .padding(.horizontal, 16)
                .padding(.bottom, 12)
            }
        }
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
        .brakePanel()
    }

    private var activeJourneyCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let destination = navigation.destinationTitle {
                Text(destination).font(.brake(.caption, weight: .semibold)).foregroundStyle(BrakeTheme.muted).lineLimit(1)
            }
            journeyMetrics(time: navigation.hasArrived ? 0 : navigation.remainingTime,
                           distance: navigation.hasArrived ? 0 : navigation.remainingDistance)
        }
        .brakePanel()
    }

    private var activeControls: some View {
        HStack(spacing: 10) {
            Button { navigation.spokenDirections.toggle() } label: {
                Label(navigation.spokenDirections ? "Mute" : "Unmute", systemImage: navigation.spokenDirections ? "speaker.wave.2" : "speaker.slash")
                    .font(.brake(.subheadline, weight: .semibold))
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .background(BrakeTheme.panel, in: Capsule())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(navigation.spokenDirections ? "Mute spoken directions" : "Enable spoken directions")
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

    private func routeMap(follow: Bool) -> some View {
        NavigationMapView(route: navigation.route, location: navigation.location,
                          followsLocation: follow, recenterTrigger: recenterTrigger)
            .overlay(alignment: .topTrailing) {
                Button { recenterTrigger += 1 } label: {
                    Image(systemName: "location.fill")
                        .font(.brake(size: 17, weight: .semibold))
                        .foregroundStyle(BrakeTheme.accent)
                        .frame(width: 44, height: 44)
                        .background(BrakeTheme.panel, in: Circle())
                        .shadow(color: .black.opacity(0.3), radius: 4)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(navigation.isNavigating ? "Recenter navigation map" : "Show whole route")
                .padding(12)
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
            journeyValue("Est. arrival", value: navigation.hasArrived ? "Arrived" : time.map { Date().addingTimeInterval($0).formatted(date: .omitted, time: .shortened) } ?? "—")
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

/// Map panning is disabled so a horizontal drag can always change pages.
/// Pinch zoom remains available; attribution stays in the unobstructed map footer.
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
