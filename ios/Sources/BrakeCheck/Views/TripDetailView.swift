import BrakeCheckCore
import CoreLocation
import MapKit
import SwiftUI

/// The trip page (design pick #8): the route on a map with the moments BrakeCheck flagged, the
/// totals, a "What happened" list and any saved clips. Trips from before route saving existed show
/// their totals without a map.
struct TripDetailView: View {
    @ObservedObject var model: DriveModel
    let trip: TripMetrics
    let useMPH: Bool
    @State private var route: TripRoute?
    @State private var loaded = false
    @ObservedObject private var premium = PremiumStore.shared

    private var routeDistanceMeters: Double? {
        guard let points = route?.points, points.count > 1 else { return nil }
        let total = zip(points, points.dropFirst()).reduce(0.0) { distance, pair in
            let start = CLLocation(latitude: pair.0.latitude, longitude: pair.0.longitude)
            let end = CLLocation(latitude: pair.1.latitude, longitude: pair.1.longitude)
            let segment = start.distance(from: end)
            return distance + (segment < 250 ? segment : 0)
        }
        return total > 0 ? total : nil
    }

    private var incidents: [IncidentManifest] {
        model.incidents.filter { $0.triggerTime >= trip.startedAt && $0.triggerTime <= trip.endedAt }
    }

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
                whatHappened
                if !premium.isSubscribed {
                    NavigationLink {
                        PremiumView()
                            .navigationTitle("Premium")
                            .navigationBarTitleDisplayMode(.inline)
                    } label: {
                        HStack(spacing: 12) {
                            Image(systemName: "sparkles")
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Interested in BrakeCheck Premium?")
                                    .font(.brake(.subheadline, weight: .bold))
                                Text("See extra route insights and the monthly plan")
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
        .task {
            route = TripRoute.load(trip.id)
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

    private var stats: some View {
        HStack(alignment: .top, spacing: 8) {
            stat("\(Int((trip.durationSeconds / 60).rounded()))", "minutes", BrakeTheme.text)
            stat(trip.maxSpeedMPH.map { String(format: "%.0f", $0 * (useMPH ? 1 : 1.609344)) } ?? "--",
                 useMPH ? "max mph" : "max km/h", BrakeTheme.text)
            stat("\(trip.estimatedLeadHardBrakes)", trip.estimatedLeadHardBrakes == 1 ? "hard brake" : "hard brakes", BrakeTheme.warning)
            stat("\(trip.estimatedCloseCalls)", trip.estimatedCloseCalls == 1 ? "close call" : "close calls", BrakeTheme.danger)
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
                    HStack(spacing: 12) {
                        Circle().fill(color(event.kind)).frame(width: 10, height: 10)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(headline(event)).font(.brake(size: 15, weight: .semibold))
                            Text(detail(event)).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                        }
                        Spacer(minLength: 0)
                    }
                    .frame(minHeight: 54)
                    .accessibilityElement(children: .combine)
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
        kind == .hardBrake ? BrakeTheme.warning : BrakeTheme.danger
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
        map.addOverlay(path)
        if let first = coordinates.first { map.addAnnotation(Pin(first, title: "Start", kind: .start)) }
        if let last = coordinates.last { map.addAnnotation(Pin(last, title: "End", kind: .end)) }
        for event in route.events {
            guard let latitude = event.latitude, let longitude = event.longitude else { continue }
            map.addAnnotation(Pin(CLLocationCoordinate2D(latitude: latitude, longitude: longitude),
                                  title: event.kind.title, kind: event.kind == .hardBrake ? .brake : .alert))
        }
        map.setVisibleMapRect(path.boundingMapRect,
            edgePadding: UIEdgeInsets(top: 32, left: 32, bottom: 32, right: 32), animated: false)
    }

    private final class Pin: NSObject, MKAnnotation {
        enum Kind { case start, end, brake, alert }
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
            }
            view.canShowCallout = true
            return view
        }
    }
}
