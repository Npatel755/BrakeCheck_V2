import BrakeCheckCore
import SwiftUI
import UIKit

struct TripHistoryView: View {
    @ObservedObject var model: DriveModel
    let useMPH: Bool
    @State private var csvURL: URL?
    @State private var exportError: String?
    @EnvironmentObject private var plus: PlusStore
    @State private var showingPlus = false

    /// Newest first. Free keeps the most recent trips visible; older ones stay saved for Plus.
    private var visibleTrips: ArraySlice<TripMetrics> {
        model.savedTrips.prefix(PlusPolicy.visibleTripCount(total: model.savedTrips.count, isPlus: plus.isPlus))
    }
    private var lockedTrips: Int { PlusPolicy.lockedTripCount(total: model.savedTrips.count, isPlus: plus.isPlus) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                if model.savedTrips.isEmpty {
                    ContentUnavailableView("No saved trips", systemImage: "chart.xyaxis.line",
                        description: Text("Your drive summary appears here after you tap End trip."))
                } else {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Your driving history").font(.brake(.title2, weight: .bold))
                        HStack(spacing: 20) {
                            metric("Completed trips", "\(model.savedTrips.count)")
                            metric("Time driving", tripDuration(model.savedTrips.reduce(0) { $0 + $1.durationSeconds }))
                        }
                    }.brakePanel()
                    ForEach(visibleTrips) { trip in
                        NavigationLink {
                            TripDetailView(model: model, trip: trip, useMPH: useMPH)
                                .modifier(DedicatedPageStyle())
                        } label: {
                            VStack(alignment: .leading, spacing: 12) {
                                HStack {
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(trip.startedAt.formatted(.dateTime.weekday(.wide).month(.abbreviated).day())).font(.brake(.headline))
                                        Text("\(trip.startedAt.formatted(date: .omitted, time: .shortened)) – \(trip.endedAt.formatted(date: .omitted, time: .shortened))")
                                            .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                                    }
                                    Spacer()
                                    Image(systemName: "chevron.right").foregroundStyle(BrakeTheme.accent)
                                }
                                HStack(spacing: 20) {
                                    metric("Duration", tripDuration(trip.durationSeconds))
                                    metric("Max. GPS speed", tripSpeed(trip.maxSpeedMPH, useMPH: useMPH))
                                }
                                Text("\(trip.estimatedLeadHardBrakes) estimated lead brakes · \(trip.estimatedCloseCalls) estimated closing events")
                                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                            }.brakePanel()
                        }.buttonStyle(.plain)
                    }
                    if lockedTrips > 0 {
                        Button { showingPlus = true } label: {
                            HStack(spacing: 14) {
                                Image(systemName: "lock.fill").foregroundStyle(BrakeTheme.accent).accessibilityHidden(true)
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(lockedTrips == 1 ? "1 older trip" : "\(lockedTrips) older trips").font(.brake(.headline))
                                    Text("Saved on this iPhone. See your full history with BrakeCheck Plus.")
                                        .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                                }
                                Spacer()
                                Image(systemName: "chevron.right").foregroundStyle(BrakeTheme.accent)
                            }.brakePanel()
                        }.buttonStyle(.plain)
                    }
                }
                Text("Saved on this iPhone. Camera event counts are estimates, not confirmed collisions or a driving score.")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
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
        .sheet(isPresented: $showingPlus) { PlusPaywallView().environmentObject(plus) }
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

private func metric(_ title: String, _ value: String) -> some View {
    VStack(alignment: .leading, spacing: 4) {
        Text(value).font(.brake(.title3, weight: .semibold)).monospacedDigit()
        Text(title).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
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
