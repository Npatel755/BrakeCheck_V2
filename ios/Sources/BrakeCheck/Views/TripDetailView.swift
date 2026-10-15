import BrakeCheckCore
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
            let path = route.points.map { CLLocationCoordinate2D(latitude: $0.latitude, longitude: $0.longitude) }
            let marked = route.events.filter { $0.latitude != nil && $0.longitude != nil }
            Map(initialPosition: .automatic, interactionModes: [.pan, .zoom]) {
                MapPolyline(coordinates: path).stroke(BrakeTheme.accentText, lineWidth: 5)
                Annotation("Start", coordinate: path[0]) {
                    Circle().fill(BrakeTheme.panel).frame(width: 14, height: 14)
                        .overlay(Circle().stroke(BrakeTheme.accentText, lineWidth: 3))
                }
                Annotation("End", coordinate: path[path.count - 1]) {
                    Circle().fill(BrakeTheme.accentText).frame(width: 16, height: 16)
                }
                ForEach(marked) { event in
                    Annotation(event.kind.title, coordinate: CLLocationCoordinate2D(latitude: event.latitude ?? 0, longitude: event.longitude ?? 0)) {
                        Circle().fill(color(event.kind)).frame(width: 18, height: 18)
                            .overlay(Circle().stroke(.white, lineWidth: 2))
                    }
                }
            }
            .mapStyle(.standard(pointsOfInterest: .excludingAll))
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
