import CoreLocation
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

    private var canStart: Bool {
        navigation.route != nil && !navigation.isCalculating && navigation.hasFreshLocation
            && !model.demo && !model.replaying && !model.stopping
    }

    private var displaysRisk: Bool {
        model.tripActive || model.brakeWarningActive || model.demo || model.replaying
    }

    var body: some View {
        GeometryReader { geometry in
            let landscape = geometry.size.width > geometry.size.height
            VStack(spacing: landscape ? 8 : 12) {
                header
                if displaysRisk {
                    RiskPill(risk: model.risk, critical: model.brakeWarningActive,
                             searching: (model.cameraReady || model.replaying) && !model.demo && model.risk.distance == nil,
                             paused: model.trackingCoasting, selectionStatus: model.leadSelectionStatus,
                             meter: model.dangerMeter, large: false)
                        .padding(.vertical, model.brakeWarningActive ? 6 : 0)
                        .padding(.horizontal, 16)
                }
                if navigation.isNavigating {
                    guidanceLayout(landscape: landscape)
                } else {
                    planningLayout(landscape: landscape)
                }
            }
            .padding(.top, landscape ? 8 : 16)
        }
        .background(BrakeTheme.background)
        .foregroundStyle(.white)
        .tint(BrakeTheme.accent)
    }

    private var header: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 5) {
                Label("Navigation", systemImage: "location.north.line.fill")
                    .font(.system(size: 21, weight: .semibold))
                    .foregroundStyle(.white)
                HStack(spacing: 5) {
                    Circle().fill(cameraTrackingColor).frame(width: 5, height: 5)
                    Text(cameraTrackingText).font(.caption).foregroundStyle(BrakeTheme.muted)
                }
            }
            Spacer(minLength: 0)
            Button(action: openCamera) {
                Label("Camera", systemImage: "video")
                    .font(.subheadline.weight(.semibold))
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
                        Text("Finding a driving route…").font(.subheadline)
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
                        Text("Plan your next drive.").font(.headline)
                        Text("Search for a destination while parked. Your camera tracking stays on when you switch pages during a trip.")
                            .font(.subheadline)
                            .foregroundStyle(BrakeTheme.muted)
                    }
                    .brakePanel()
                }
                if !navigation.status.isEmpty {
                    Text(navigation.status)
                        .font(.caption)
                        .foregroundStyle(BrakeTheme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Text("Maps and route searches require a network connection.")
                    .font(.caption)
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
            Text("Where to?").font(.system(size: 18, weight: .semibold))
            HStack(spacing: 10) {
                Image(systemName: "magnifyingglass").foregroundStyle(BrakeTheme.muted)
                TextField("Address, place, or city", text: $navigation.query)
                    .font(.subheadline)
                    .textInputAutocapitalization(.words)
                    .autocorrectionDisabled()
                    .submitLabel(.search)
                    .focused($searchFocused)
                    .onSubmit { submitSearch() }
                    .accessibilityLabel("Destination search")
                if navigation.isSearching {
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
            Button(action: submitSearch) {
                Text(navigation.isSearching ? "Searching…" : "Search destinations")
                    .font(.subheadline.weight(.semibold))
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
                } label: {
                    HStack(spacing: 12) {
                        Image(systemName: "mappin.circle.fill").foregroundStyle(BrakeTheme.amber)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(result.title).font(.subheadline.weight(.semibold)).foregroundStyle(.white)
                            if !result.subtitle.isEmpty {
                                Text(result.subtitle).font(.caption).foregroundStyle(BrakeTheme.muted)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        Spacer(minLength: 0)
                        Image(systemName: "chevron.right").font(.caption).foregroundStyle(BrakeTheme.muted)
                    }
                    .padding(.vertical, 10)
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityElement(children: .combine)
            }
        }
        .brakePanel()
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
                    .font(.headline)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                Button { navigation.clearDestination() } label: {
                    Image(systemName: "xmark").font(.subheadline.weight(.semibold)).frame(width: 32, height: 32)
                }
                .buttonStyle(.plain)
                .foregroundStyle(BrakeTheme.muted)
                .accessibilityLabel("Clear planned destination")
            }
            journeyMetrics(time: navigation.route?.expectedTravelTime, distance: navigation.route?.distance)
        }
        .brakePanel()
    }

    private var startControls: some View {
        VStack(alignment: .leading, spacing: 12) {
            Toggle("Spoken directions", isOn: $navigation.spokenDirections)
                .font(.subheadline)
            Button(action: startDrive) {
                Label("Start navigation", systemImage: "location.north.fill")
                    .font(.system(size: 16, weight: .bold))
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
                    .font(.caption).foregroundStyle(BrakeTheme.warning)
            } else if !navigation.hasFreshLocation {
                Text("Waiting for a fresh GPS location before starting.")
                    .font(.caption).foregroundStyle(BrakeTheme.warning)
            } else if !model.tripActive {
                Text("Starting navigation also starts your camera trip.")
                    .font(.caption).foregroundStyle(BrakeTheme.muted)
            }
        }
        .brakePanel()
    }

    @ViewBuilder private func guidanceLayout(landscape: Bool) -> some View {
        if landscape {
            HStack(alignment: .top, spacing: 12) {
                routeMap(follow: !navigation.isPaused && navigation.hasFreshLocation)
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
                routeMap(follow: !navigation.isPaused && navigation.hasFreshLocation)
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
            Image(systemName: navigation.hasArrived ? "checkmark.circle.fill" : navigation.isPaused ? "location.slash" : "location.north.fill")
                .font(.system(size: 25, weight: .semibold))
                .foregroundStyle(navigation.isPaused ? BrakeTheme.warning : BrakeTheme.accent)
                .frame(width: 32)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 5) {
                if navigation.hasArrived {
                    Text("You’ve arrived").font(.headline).foregroundStyle(BrakeTheme.accent)
                    Text(navigation.destinationTitle ?? "Destination reached.")
                        .font(.subheadline).foregroundStyle(BrakeTheme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                } else if navigation.isPaused || !navigation.hasFreshLocation {
                    Text("Navigation paused").font(.headline).foregroundStyle(BrakeTheme.warning)
                    Text(navigation.status.isEmpty ? "Waiting for a fresh GPS location." : navigation.status)
                        .font(.caption).foregroundStyle(BrakeTheme.muted)
                } else {
                    if let distance = navigation.maneuverDistance, distance.isFinite {
                        Text(distance < 20 ? "Now" : "In \(distanceText(distance))")
                            .font(.system(size: 22, weight: .semibold)).monospacedDigit()
                    }
                    Text(navigation.instruction.isEmpty ? "Follow the route" : navigation.instruction)
                        .font(.subheadline.weight(.medium))
                        .fixedSize(horizontal: false, vertical: true)
                    if let notice = navigation.routeNotice, !notice.isEmpty {
                        Text(notice).font(.caption).foregroundStyle(BrakeTheme.warning)
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
                Text(destination).font(.caption.weight(.semibold)).foregroundStyle(BrakeTheme.muted).lineLimit(1)
            }
            journeyMetrics(time: navigation.hasArrived ? 0 : navigation.isPaused || !navigation.hasFreshLocation ? nil : navigation.remainingTime,
                           distance: navigation.hasArrived ? 0 : navigation.isPaused || !navigation.hasFreshLocation ? nil : navigation.remainingDistance)
        }
        .brakePanel()
    }

    private var activeControls: some View {
        HStack(spacing: 10) {
            Button { navigation.spokenDirections.toggle() } label: {
                Label(navigation.spokenDirections ? "Mute" : "Unmute", systemImage: navigation.spokenDirections ? "speaker.wave.2" : "speaker.slash")
                    .font(.subheadline.weight(.semibold))
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .background(BrakeTheme.panel, in: Capsule())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(navigation.spokenDirections ? "Mute spoken directions" : "Enable spoken directions")
            Button { navigation.stopGuidance() } label: {
                Label(navigation.hasArrived ? "Done" : "Stop", systemImage: navigation.hasArrived ? "checkmark" : "stop.fill")
                    .font(.subheadline.weight(.semibold))
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
                        .font(.system(size: 17, weight: .semibold))
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
                .font(.subheadline).foregroundStyle(BrakeTheme.warning)
                .fixedSize(horizontal: false, vertical: true)
            if navigation.destinationTitle != nil {
                Button("Try route again") { navigation.retryRoute() }
                    .font(.subheadline.weight(.semibold))
                    .disabled(navigation.isCalculating)
            }
        }
        .brakePanel()
    }

    private func activeErrorCard(_ error: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(error).font(.caption).foregroundStyle(BrakeTheme.warning)
                .lineLimit(3).fixedSize(horizontal: false, vertical: true)
            Button { navigation.retryRoute() } label: {
                Label(navigation.isCalculating ? "Finding route…" : "Try route again", systemImage: "arrow.clockwise")
                    .font(.caption.weight(.semibold)).frame(minHeight: 32)
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
            Text(value).font(.system(size: 18, weight: .semibold)).monospacedDigit().lineLimit(1).minimumScaleFactor(0.7)
            Text(title).font(.caption).foregroundStyle(BrakeTheme.muted)
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
    var route: MKRoute?
    var location: CLLocation?
    var followsLocation: Bool
    var recenterTrigger: Int

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> MKMapView {
        let map = MKMapView()
        map.delegate = context.coordinator
        map.overrideUserInterfaceStyle = .dark
        map.mapType = .standard
        map.pointOfInterestFilter = .excludingAll
        map.isScrollEnabled = false
        map.isZoomEnabled = true
        map.isRotateEnabled = false
        map.isPitchEnabled = false
        map.showsCompass = false
        map.showsScale = true
        map.showsUserLocation = false
        return map
    }

    func updateUIView(_ map: MKMapView, context: Context) {
        let coordinator = context.coordinator
        let routeChanged = coordinator.route !== route
        let recenter = coordinator.recenterTrigger != recenterTrigger
        let followChanged = coordinator.wasFollowing != followsLocation
        if routeChanged {
            map.removeOverlays(map.overlays)
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
        let validLocation = location.flatMap { CLLocationCoordinate2DIsValid($0.coordinate) ? $0 : nil }
        if let location = validLocation {
            if coordinator.userPosition == nil {
                let annotation = MKPointAnnotation()
                annotation.title = "Your location"
                coordinator.userPosition = annotation
                map.addAnnotation(annotation)
            }
            coordinator.userPosition?.coordinate = location.coordinate
        } else if let annotation = coordinator.userPosition {
            map.removeAnnotation(annotation)
            coordinator.userPosition = nil
        }
        let locationChanged = coordinator.lastLocationTimestamp != validLocation?.timestamp
        if followsLocation, let location = validLocation,
           locationChanged || routeChanged || followChanged || recenter {
            let distance = recenter || followChanged || !coordinator.hasCentered ? 700 : max(180, min(5000, map.camera.altitude))
            let heading = location.course.isFinite && location.course >= 0 ? location.course : 0
            map.setCamera(MKMapCamera(lookingAtCenter: location.coordinate, fromDistance: distance, pitch: 0, heading: heading), animated: coordinator.hasCentered && !routeChanged)
            coordinator.hasCentered = true
        } else if !followsLocation && (routeChanged || recenter || !coordinator.hasCentered) {
            if let route {
                map.setVisibleMapRect(route.polyline.boundingMapRect,
                                      edgePadding: UIEdgeInsets(top: 40, left: 30, bottom: 44, right: 30), animated: false)
                coordinator.hasCentered = true
            } else if let location = validLocation {
                map.setRegion(MKCoordinateRegion(center: location.coordinate, latitudinalMeters: 1800, longitudinalMeters: 1800), animated: false)
                coordinator.hasCentered = true
            }
        }
        coordinator.lastLocationTimestamp = validLocation?.timestamp
        coordinator.wasFollowing = followsLocation
        coordinator.recenterTrigger = recenterTrigger
    }

    final class Coordinator: NSObject, MKMapViewDelegate {
        var route: MKRoute?
        var destination: MKPointAnnotation?
        var userPosition: MKPointAnnotation?
        var lastLocationTimestamp: Date?
        var recenterTrigger = 0
        var wasFollowing = false
        var hasCentered = false

        func mapView(_ mapView: MKMapView, rendererFor overlay: MKOverlay) -> MKOverlayRenderer {
            guard let line = overlay as? MKPolyline else { return MKOverlayRenderer(overlay: overlay) }
            let renderer = MKPolylineRenderer(polyline: line)
            renderer.strokeColor = UIColor(red: 0.63, green: 0.98, blue: 0.78, alpha: 1)
            renderer.lineWidth = 6
            return renderer
        }

        func mapView(_ mapView: MKMapView, viewFor annotation: MKAnnotation) -> MKAnnotationView? {
            let isUser = (annotation as AnyObject) === userPosition
            let identifier = isUser ? "NavigationPosition" : "NavigationDestination"
            let view = (mapView.dequeueReusableAnnotationView(withIdentifier: identifier) as? MKMarkerAnnotationView)
                ?? MKMarkerAnnotationView(annotation: annotation, reuseIdentifier: identifier)
            view.annotation = annotation
            view.markerTintColor = isUser ? .systemBlue : UIColor(red: 1, green: 0.69, blue: 0.125, alpha: 1)
            view.glyphImage = UIImage(systemName: isUser ? "location.fill" : "flag.fill")
            view.displayPriority = .required
            view.canShowCallout = false
            return view
        }
    }
}
