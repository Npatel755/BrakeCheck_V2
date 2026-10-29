import BrakeCheckCore
import SwiftUI
import UIKit

struct TripHistoryView: View {
    @ObservedObject var model: DriveModel
    @ObservedObject private var premium = PremiumStore.shared
    let useMPH: Bool
    @State private var csvURL: URL?
    @State private var exportError: String?

    var body: some View {
        // Free keeps the last 7 days on screen; older trips stay saved and come back with Premium.
        let history = TripHistoryPolicy.split(model.savedTrips, now: Date(), premium: premium.isSubscribed)
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                if model.savedTrips.isEmpty {
                    ContentUnavailableView("No saved trips", systemImage: "chart.xyaxis.line",
                        description: Text("Your drive summary appears here after you tap End trip."))
                } else {
                    VStack(alignment: .leading, spacing: 12) {
                        Text(premium.isSubscribed ? "Your driving history" : "Your last 7 days")
                            .font(.brake(.title2, weight: .bold))
                        HStack(spacing: 20) {
                            metric("Completed trips", "\(history.visible.count)")
                            metric("Time driving", tripDuration(history.visible.reduce(0) { $0 + $1.durationSeconds }))
                        }
                    }.brakePanel()
                    if premium.isSubscribed { trends }
                    ForEach(history.visible) { trip in
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
                                    Image(systemName: "chevron.right").foregroundStyle(BrakeTheme.accentText)
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
                    if history.lockedCount > 0 { lockedTrips(history.lockedCount) }
                }
                Text("Saved on this iPhone. Camera event counts are estimates, not confirmed collisions or a driving score.")
                    .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
            }.padding(20)
        }
        .foregroundStyle(BrakeTheme.text)
        .navigationTitle("Trip history")
        .toolbar {
            // Exports always include every saved trip: the data stays the driver's, Premium or not.
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

    /// Premium: the last four calendar weeks, newest first.
    private var trends: some View {
        let weeks = TripTrends.weekly(model.savedTrips, now: Date())
        return VStack(alignment: .leading, spacing: 12) {
            Label("Weekly trends", systemImage: "chart.xyaxis.line")
                .font(.brake(.headline))
            ForEach(Array(weeks.enumerated()), id: \.offset) { index, week in
                HStack(spacing: 12) {
                    Text(weekLabel(index, week.weekStart))
                        .font(.brake(.subheadline, weight: .semibold))
                        .frame(width: 92, alignment: .leading)
                    Text(week.trips == 0 ? "No trips" : "\(week.trips) \(week.trips == 1 ? "trip" : "trips") · \(tripDuration(week.drivingSeconds))")
                        .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                    Spacer(minLength: 8)
                    Text(week.hardBrakesPerHour.map { String(format: "%.1f / hr", $0) } ?? "–")
                        .font(.brake(.subheadline, weight: .semibold)).monospacedDigit()
                }
            }
            Text("Estimated hard brakes by the car ahead, per hour of driving. Camera estimates, not a driving score.")
                .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
        }.brakePanel()
    }

    private func weekLabel(_ index: Int, _ start: Date) -> String {
        switch index {
        case 0: return "This week"
        case 1: return "Last week"
        default: return "Week of " + start.formatted(.dateTime.month(.abbreviated).day())
        }
    }

    private func lockedTrips(_ count: Int) -> some View {
        NavigationLink {
            PremiumView(context: .history)
                .navigationTitle("Premium")
                .navigationBarTitleDisplayMode(.inline)
        } label: {
            HStack(spacing: 14) {
                Image(systemName: "lock.fill")
                    .font(.brake(size: 18, weight: .semibold))
                    .foregroundStyle(BrakeTheme.accentText)
                    .frame(width: 28)
                VStack(alignment: .leading, spacing: 3) {
                    Text("\(count) older \(count == 1 ? "trip" : "trips")").font(.brake(.headline))
                    Text("Still saved on this iPhone. See your full history and weekly trends with Premium.")
                        .font(.brake(.caption)).foregroundStyle(BrakeTheme.muted)
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right").foregroundStyle(BrakeTheme.accentText)
            }.brakePanel()
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("history.locked")
    }

    private func exportCSV() {
        do {
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("BrakeSense-trip-history.csv")
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
