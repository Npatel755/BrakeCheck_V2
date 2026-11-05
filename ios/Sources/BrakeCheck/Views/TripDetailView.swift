import BrakeCheckCore
import CoreLocation
import MapKit
import SwiftUI

/// The trip page (design pick #8): the route on a map with the moments BrakeSense flagged, the
/// totals, a "What happened" list and any saved clips. Trips from before route saving existed show
/// their totals without a map.
struct TripDetailView: View {
    let model: DriveModel
    let trip: TripMetrics
    let useMPH: Bool
    @State private var route: TripRoute?
    @State private var gpxURL: URL?
    @State private var loaded = false
    @State private var routeError: String?
    @State private var loadRevision = 0
    @ObservedObject private var premium = PremiumStore.shared

    private var routeDistanceMeters: Double? { route?.distanceMeters }

    private var incidents: [IncidentManifest] { model.incidents.filter { trip.includes($0) } }

    private var title: String {
        if let destination = route?.destination, !destination.isEmpty { return "To \(destination)" }
        if Calendar.current.isDateInToday(trip.startedAt) { return "Today's drive" }
        if Calendar.current.isDateInYesterday(trip.startedAt) { return "Yesterday's drive" }
        return "\(trip.startedAt.formatted(.dateTime.weekday(.wide))) drive"
    }

    private var subtitle: String {
        "\(trip.startedAt.formatted(.dateTime.month(.abbreviated).day())) · "
            + "\(trip.startedAt.formatted(date: .omitted, time: .shortened)) – \(trip.endedAt.formatted(date: .omitted, time: .shortened))"
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(title).font(.brake(size: 28, weight: .bold)).fixedSize(horizontal: false, vertical: true)
                    Text(subtitle).font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
                }
                VStack(spacing: 0) {
                    map.frame(height: 240)
                    stats.padding(.horizontal, 18).padding(.vertical, 14)
                }
                .background(BrakeTheme.panel)
                .clipShape(RoundedRectangle(cornerRadius: 24, style: .continuous))
                if let routeError {
                    VStack(alignment: .leading, spacing: 8) {
                        Label("Trip map unavailable", systemImage: "exclamationmark.triangle")
                        Text(routeError).font(.brake(.caption))
                        Button("Retry loading map") { loadRevision += 1 }
                    }.brakePanel()
                }
                whatHappened
                if !premium.isSubscribed {
                    NavigationLink {
                        PremiumView(model: model)
                            .navigationTitle("Premium")
                            .navigationBarTitleDisplayMode(.inline)
                    } label: {
                        HStack(spacing: 12) {
                            Image(systemName: "sparkles")
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Route distance and average speed")
                                    .font(.brake(.subheadline, weight: .bold))
                                Text("Part of BrakeSense Premium, with Go alerts and weekly trends")
                                    .font(.brake(.caption))
                            }
                            Spacer(minLength: 0)
                            Image(systemName: "chevron.right")
                        }
                        .foregroundStyle(BrakeTheme.accentText)
                        .padding(16)
                        .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 18))
                    }
                    .buttonStyle(.plain)
                }
                if premium.isSubscribed, let routeDistanceMeters {
                    VStack(alignment: .leading, spacing: 8) {
                        Label("Premium trip insights", systemImage: "sparkles")
                            .font(.brake(.headline))
                        Text(String(format: "Approximate route distance: %.1f %@",
                            routeDistanceMeters / (useMPH ? 1609.344 : 1000), useMPH ? "mi" : "km"))
                        if trip.durationSeconds > 0 {
                            Text(String(format: "Average trip speed: %.0f %@",
                                routeDistanceMeters / trip.durationSeconds * (useMPH ? 2.236936 : 3.6),
                                useMPH ? "mph" : "km/h"))
                        }
                        Text("Based on sampled GPS route points; signal gaps can lower these estimates.")
                            .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(18)
                    .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 20))
                }
                if !incidents.isEmpty { clips }
                Text("Saved on this iPhone. Camera events are estimates, not confirmed collisions or a driving score.")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            }
            .padding(20)
        }
        .background(BrakeTheme.background)
        .foregroundStyle(BrakeTheme.text)
        .navigationTitle("Trip")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if let gpxURL {
                ShareLink(item: gpxURL, preview: SharePreview(title, image: Image(systemName: "map"))) {
                    Label("Share route", systemImage: "square.and.arrow.up")
                }
                .accessibilityIdentifier("trip.shareGPX")
            }
        }
        .task(id: loadRevision) {
            do {
                let loadedRoute = try await model.loadTripRoute(trip.id)
                guard !Task.isCancelled else { return }
                route = loadedRoute; routeError = nil
                gpxURL = loadedRoute.flatMap(writeGPX)
            } catch {
                guard !Task.isCancelled else { return }
                route = nil; routeError = error.localizedDescription
            }
            loaded = true
        }
    }

    // MARK: - Map

    @ViewBuilder private var map: some View {
        if let route, route.points.count >= 2 {
            let marked = route.events.filter { $0.latitude != nil && $0.longitude != nil }
            TripRouteMapView(route: route)
            .accessibilityLabel("Route map with \(marked.count) flagged moment\(marked.count == 1 ? "" : "s")")
        } else {
            ZStack {
                BrakeTheme.raised
                VStack(spacing: 8) {
                    Image(systemName: "map").font(.brake(size: 28, weight: .medium)).foregroundStyle(BrakeTheme.muted)
                    Text(!loaded ? "Loading the route…" : route == nil ? "No route saved for this trip" : "Not enough GPS to draw the route")
                        .font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
                }
            }
        }
    }

    // MARK: - Totals

    /// Writes the trail as GPX into Caches so Share can hand a file to Maps, Files or Messages.
    private func writeGPX(_ route: TripRoute) -> URL? {
        let name = "BrakeSense trip \(trip.startedAt.formatted(.dateTime.year().month(.twoDigits).day(.twoDigits).hour().minute()))"
        guard let document = route.gpxDocument(name: name),
              let caches = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first else { return nil }
        let safeName = name.replacingOccurrences(of: "[/:]", with: "-", options: .regularExpression)
        let url = caches.appendingPathComponent("\(safeName).gpx")
        do {
            try document.write(to: url, atomically: true, encoding: .utf8)
            return url
        } catch {
            return nil
        }
    }

    private var stats: some View {
        HStack(alignment: .top, spacing: 8) {
            stat("\(Int((trip.durationSeconds / 60).rounded()))", "minutes", BrakeTheme.text)
            stat(trip.maxSpeedMPH.map { String(format: "%.0f", $0 * (useMPH ? 1 : 1.609344)) } ?? "--",
                 useMPH ? "max mph" : "max km/h", BrakeTheme.text)
            stat("\(trip.totalHardBrakes)", trip.totalHardBrakes == 1 ? "hard brake" : "hard brakes", BrakeTheme.warning)
            stat("\(trip.estimatedCloseCalls)", trip.estimatedCloseCalls == 1 ? "close call" : "close calls", BrakeTheme.danger)
            if let score = TripSafetyScore(trip) {
                stat("\(score.value)", score.label.lowercased(), score.value >= 70 ? BrakeTheme.accentText : BrakeTheme.warning)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func stat(_ value: String, _ label: String, _ color: Color) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value).font(.brake(size: 26, weight: .bold)).monospacedDigit().foregroundStyle(color)
            Text(label).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - What happened

    private var whatHappened: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("WHAT HAPPENED").font(.brake(size: 11, weight: .bold)).tracking(1.2).foregroundStyle(BrakeTheme.muted)
                .padding(.top, 14).padding(.bottom, 4)
            if let events = route?.events, !events.isEmpty {
                ForEach(Array(events.enumerated()), id: \.element.id) { index, event in
                    if let clip = clip(for: event) {
                        NavigationLink {
                            IncidentDetailView(model: model, id: clip.id)
                        } label: {
                            eventRow(event, hasClip: true)
                        }
                        .buttonStyle(.plain)
                        .accessibilityHint("Opens the saved clip")
                    } else {
                        eventRow(event, hasClip: false)
                    }
                    if index < events.count - 1 {
                        Rectangle().fill(BrakeTheme.text.opacity(0.06)).frame(height: 1)
                    }
                }
            } else {
                Text(route == nil ? "Flagged moments are saved for trips from this version on."
                                  : "Nothing flagged on this drive.")
                    .font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
                    .padding(.vertical, 14)
            }
        }
        .padding(.horizontal, 18)
        .padding(.bottom, 4)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
    }

    private func eventRow(_ event: TripRoute.Event, hasClip: Bool) -> some View {
        HStack(spacing: 12) {
            Circle().fill(color(event.kind)).frame(width: 10, height: 10)
            VStack(alignment: .leading, spacing: 2) {
                Text(headline(event)).font(.brake(size: 15, weight: .semibold))
                Text(detail(event)).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            }
            Spacer(minLength: 0)
            if hasClip {
                Label("Watch clip", systemImage: "play.rectangle.fill")
                    .font(.brake(size: 13, weight: .bold)).foregroundStyle(BrakeTheme.accentText)
            }
        }
        .frame(minHeight: 54)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }

    /// The saved clip whose trigger fell within 20 s of the moment, if any.
    private func clip(for event: TripRoute.Event) -> IncidentManifest? {
        incidents.filter { abs($0.triggerTime.timeIntervalSince(event.time)) <= 20 }
            .min { abs($0.triggerTime.timeIntervalSince(event.time)) < abs($1.triggerTime.timeIntervalSince(event.time)) }
    }

    private func headline(_ event: TripRoute.Event) -> String {
        if event.kind == .closeCall, let seconds = event.closingSeconds {
            return "Close call · \(String(format: "%.1f", seconds)) s gap"
        }
        return event.kind.title
    }

    private func detail(_ event: TripRoute.Event) -> String {
        var parts = [event.time.formatted(date: .omitted, time: .shortened)]
        if let mph = event.speedMPH {
            parts.append(useMPH ? String(format: "%.0f mph", mph) : String(format: "%.0f km/h", mph * 1.609344))
        }
        return parts.joined(separator: " · ")
    }

    private func color(_ kind: TripRoute.Kind) -> Color {
        if kind.isLaneAlert { return BrakeTheme.accentText }
        return kind == .hardBrake || kind == .ownHardBrake ? BrakeTheme.warning : BrakeTheme.danger
    }

    // MARK: - Clips

    private var clips: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("SAVED CLIPS").font(.brake(size: 11, weight: .bold)).tracking(1.2).foregroundStyle(BrakeTheme.muted)
            ForEach(incidents) { incident in
                NavigationLink {
                    IncidentDetailView(model: model, id: incident.id)
                } label: {
                    HStack(spacing: 10) {
                        Image(systemName: "play.rectangle.fill").font(.brake(size: 18, weight: .semibold))
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Watch \(incident.reason.lowercased().hasPrefix("manual") ? "your saved clip" : "the clip")")
                                .font(.brake(size: 16, weight: .bold))
                            Text("\(incident.triggerTime.formatted(date: .omitted, time: .shortened)) · \(incident.reason)")
                                .font(.brake(.caption)).opacity(0.8).lineLimit(1)
                        }
                        Spacer(minLength: 0)
                        Image(systemName: "chevron.right").font(.brake(size: 13, weight: .bold))
                    }
                    .foregroundStyle(BrakeTheme.background)
                    .padding(.horizontal, 16)
                    .frame(maxWidth: .infinity, minHeight: 60)
                    .background(BrakeTheme.accent, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                    .shadow(color: BrakeTheme.buttonShadow, radius: 10, y: 5)
                }
                .buttonStyle(.plain)
            }
        }
    }
}

/// The xtool SDK exposes MapKit's UIKit map while its SwiftUI Map symbols are
/// unavailable. Keep the trip map interactive through this small native view.
private struct TripRouteMapView: UIViewRepresentable {
    let route: TripRoute

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> MKMapView {
        let map = MKMapView()
        map.delegate = context.coordinator
        map.pointOfInterestFilter = .excludingAll
        map.showsCompass = false
        return map
    }

    func updateUIView(_ map: MKMapView, context: Context) {
        guard !context.coordinator.loaded else { return }
        context.coordinator.loaded = true
        let coordinates = route.points.map {
            CLLocationCoordinate2D(latitude: $0.latitude, longitude: $0.longitude)
        }
        let path = MKPolyline(coordinates: coordinates, count: coordinates.count)
        for segment in route.continuousSegments where segment.count >= 2 {
            let measured = segment.map { CLLocationCoordinate2D(latitude: $0.latitude, longitude: $0.longitude) }
            map.addOverlay(MKPolyline(coordinates: measured, count: measured.count))
        }
        if let first = coordinates.first { map.addAnnotation(Pin(first, title: "Start", kind: .start)) }
        if let last = coordinates.last { map.addAnnotation(Pin(last, title: "End", kind: .end)) }
        for event in route.events {
            guard let latitude = event.latitude, let longitude = event.longitude else { continue }
            map.addAnnotation(Pin(CLLocationCoordinate2D(latitude: latitude, longitude: longitude),
                                  title: event.kind.title,
                                  kind: event.kind.isLaneAlert ? .lane
                                      : event.kind == .hardBrake || event.kind == .ownHardBrake ? .brake : .alert))
        }
        map.setVisibleMapRect(path.boundingMapRect,
            edgePadding: UIEdgeInsets(top: 32, left: 32, bottom: 32, right: 32), animated: false)
    }

    private final class Pin: NSObject, MKAnnotation {
        enum Kind { case start, end, brake, alert, lane }
        let coordinate: CLLocationCoordinate2D
        let title: String?
        let kind: Kind
        init(_ coordinate: CLLocationCoordinate2D, title: String, kind: Kind) {
            self.coordinate = coordinate; self.title = title; self.kind = kind
        }
    }

    final class Coordinator: NSObject, MKMapViewDelegate {
        var loaded = false
        func mapView(_ mapView: MKMapView, rendererFor overlay: MKOverlay) -> MKOverlayRenderer {
            let renderer = MKPolylineRenderer(overlay: overlay)
            renderer.strokeColor = BrakeTheme.accentUIColor(for: mapView.traitCollection.userInterfaceStyle)
            renderer.lineWidth = 5
            return renderer
        }
        func mapView(_ mapView: MKMapView, viewFor annotation: MKAnnotation) -> MKAnnotationView? {
            guard let pin = annotation as? Pin else { return nil }
            let view = MKMarkerAnnotationView(annotation: pin, reuseIdentifier: "trip-pin")
            switch pin.kind {
            case .start, .end:
                view.markerTintColor = BrakeTheme.accentUIColor(for: mapView.traitCollection.userInterfaceStyle)
            case .brake: view.markerTintColor = .systemOrange
            case .alert: view.markerTintColor = .systemRed
            case .lane: view.markerTintColor = .systemTeal
            }
            view.canShowCallout = true
            return view
        }
    }
}
