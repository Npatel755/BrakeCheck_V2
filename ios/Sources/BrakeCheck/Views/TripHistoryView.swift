import BrakeCheckCore
import SwiftUI
import UIKit

struct TripHistoryView: View {
    @ObservedObject var model: DriveModel
    let useMPH: Bool
    @State private var csvURL: URL?
    @State private var exportError: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                if model.savedTrips.isEmpty {
                    ContentUnavailableView("No saved trips", systemImage: "chart.xyaxis.line",
                        description: Text("Your drive summary appears here after you tap End trip."))
                } else {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Your driving history").font(.title2.bold())
                        HStack(spacing: 20) {
                            metric("Completed trips", "\(model.savedTrips.count)")
                            metric("Time driving", tripDuration(model.savedTrips.reduce(0) { $0 + $1.durationSeconds }))
                        }
                    }.brakePanel()
                    ForEach(model.savedTrips) { trip in
                        NavigationLink {
                            TripSummaryView(model: model, trip: trip, useMPH: useMPH)
                                .modifier(DedicatedPageStyle())
                        } label: {
                            VStack(alignment: .leading, spacing: 12) {
                                HStack {
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(trip.startedAt.formatted(.dateTime.weekday(.wide).month(.abbreviated).day())).font(.headline)
                                        Text("\(trip.startedAt.formatted(date: .omitted, time: .shortened)) – \(trip.endedAt.formatted(date: .omitted, time: .shortened))")
                                            .font(.caption).foregroundStyle(BrakeTheme.muted)
                                    }
                                    Spacer()
                                    Image(systemName: "chevron.right").foregroundStyle(BrakeTheme.accent)
                                }
                                HStack(spacing: 20) {
                                    metric("Duration", tripDuration(trip.durationSeconds))
                                    metric("Max. GPS speed", tripSpeed(trip.maxSpeedMPH, useMPH: useMPH))
                                }
                                Text("\(trip.estimatedLeadHardBrakes) estimated lead brakes · \(trip.estimatedCloseCalls) estimated closing events")
                                    .font(.caption).foregroundStyle(BrakeTheme.muted)
                            }.brakePanel()
                        }.buttonStyle(.plain)
                    }
                }
                Text("Saved on this iPhone. Camera event counts are estimates, not confirmed collisions or a driving score.")
                    .font(.caption).foregroundStyle(BrakeTheme.muted)
            }.padding(20)
        }
        .foregroundStyle(BrakeTheme.text)
        .navigationTitle("Trip history")
        .toolbar {
            if !model.savedTrips.isEmpty {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        ShareLink(item: model.tripHistoryURL) { Label("Export JSON", systemImage: "curlybraces") }
                        Button { exportCSV() } label: { Label("Export spreadsheet CSV", systemImage: "tablecells") }
                    } label: { Label("Export trip history", systemImage: "square.and.arrow.up") }
                }
            }
        }
        .sheet(isPresented: Binding(get: { csvURL != nil }, set: { if !$0 { csvURL = nil } })) {
            if let csvURL { TripExportSheet(url: csvURL) }
        }
        .alert("Export unavailable", isPresented: Binding(get: { exportError != nil }, set: { if !$0 { exportError = nil } })) {
            Button("OK", role: .cancel) { exportError = nil }
        } message: { Text(exportError ?? "Could not create the export.") }
    }
    private func exportCSV() {
        do {
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("BrakeCheck-trip-history.csv")
            try TripMetricsCSV.encode(model.savedTrips).write(to: url, atomically: true, encoding: .utf8)
            csvURL = url
        } catch { exportError = error.localizedDescription }
    }
}

private struct TripSummaryView: View {
    @ObservedObject var model: DriveModel
    let trip: TripMetrics
    let useMPH: Bool
    private var incidents: [IncidentManifest] {
        model.incidents.filter { $0.triggerTime >= trip.startedAt && $0.triggerTime <= trip.endedAt }
    }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text(trip.startedAt.formatted(date: .complete, time: .shortened)).font(.title2.bold())
                VStack(spacing: 14) {
                    LabeledContent("Started", value: trip.startedAt.formatted(date: .omitted, time: .shortened))
                    LabeledContent("Ended", value: trip.endedAt.formatted(date: .omitted, time: .shortened))
                    LabeledContent("Duration", value: tripDuration(trip.durationSeconds))
                    LabeledContent("Maximum GPS speed", value: tripSpeed(trip.maxSpeedMPH, useMPH: useMPH))
                }.brakePanel()
                VStack(alignment: .leading, spacing: 14) {
                    Label("Camera estimates", systemImage: "viewfinder").font(.headline)
                    LabeledContent("Estimated lead hard brakes", value: "\(trip.estimatedLeadHardBrakes)")
                    LabeledContent("Estimated closing events", value: "\(trip.estimatedCloseCalls)")
                    Text("Closing events count estimated closing time below 2 seconds. These counts depend on camera visibility and tracking; they do not prove a collision or fault.")
                        .font(.caption).foregroundStyle(BrakeTheme.muted)
                }.brakePanel()
                VStack(alignment: .leading, spacing: 14) {
                    Label("Saved incident footage", systemImage: "film.stack").font(.headline)
                    if incidents.isEmpty {
                        Text("No incident footage currently saved for this trip.").font(.subheadline).foregroundStyle(BrakeTheme.muted)
                    }
                    ForEach(incidents) { incident in
                        NavigationLink {
                            IncidentDetailView(model: model, id: incident.id)
                        } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(incident.reason).font(.subheadline.weight(.medium))
                                    Text("\(incident.triggerTime.formatted(date: .omitted, time: .shortened)) · \(incident.segments.count) clips")
                                        .font(.caption).foregroundStyle(BrakeTheme.muted)
                                }
                                Spacer()
                                Image(systemName: "chevron.right")
                            }.foregroundStyle(BrakeTheme.accent)
                        }
                    }
                }.brakePanel()
            }.padding(20)
        }.foregroundStyle(BrakeTheme.text).navigationTitle("Trip summary")
    }
}

private func metric(_ title: String, _ value: String) -> some View {
    VStack(alignment: .leading, spacing: 4) {
        Text(value).font(.title3.weight(.semibold)).monospacedDigit()
        Text(title).font(.caption).foregroundStyle(BrakeTheme.muted)
    }.frame(maxWidth: .infinity, alignment: .leading)
}
private func tripDuration(_ seconds: Double) -> String {
    let minutes = Int(seconds / 60)
    if seconds < 60 { return "\(Int(seconds)) sec" }
    return minutes >= 60 ? "\(minutes / 60) hr \(minutes % 60) min" : "\(minutes) min"
}
private func tripSpeed(_ mph: Double?, useMPH: Bool) -> String {
    mph.map { String(format: "%.0f %@", $0 * (useMPH ? 1 : 1.609344), useMPH ? "mph" : "km/h") } ?? "Unavailable"
}
private struct TripExportSheet: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: [url], applicationActivities: nil)
    }
    func updateUIViewController(_ controller: UIActivityViewController, context: Context) { }
}
