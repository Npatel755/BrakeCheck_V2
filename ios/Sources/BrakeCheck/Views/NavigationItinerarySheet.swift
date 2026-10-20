import SwiftUI

struct NavigationItinerarySheet: View {
    @ObservedObject var navigation: NavigationService
    @Environment(\.dismiss) private var dismiss
    @State private var addingStop = false

    var body: some View {
        SwiftUI.NavigationStack {
            List {
                Section { Label("Your location", systemImage: "location.fill") }
                Section("Stops in order") {
                    if navigation.itineraryStops.isEmpty {
                        Text("Add a place to visit before your destination.")
                            .font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted)
                    }
                    ForEach(Array(navigation.itineraryStops.enumerated()), id: \.element.id) { index, stop in
                        HStack(spacing: 12) {
                            Text("\(index + 1)").font(.brake(.caption, weight: .bold))
                                .frame(width: 24, height: 24)
                                .background(BrakeTheme.accent.opacity(0.2), in: Circle())
                            VStack(alignment: .leading, spacing: 3) {
                                Text(stop.title).font(.brake(.subheadline, weight: .semibold))
                                Text(stop.subtitle).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted).lineLimit(2)
                            }
                        }.padding(.vertical, 4)
                    }
                    .onDelete { offsets in
                        let ids = offsets.compactMap { navigation.itineraryStops.indices.contains($0) ? navigation.itineraryStops[$0].id : nil }
                        ids.forEach { navigation.removeStop(id: $0) }
                    }
                    .onMove { navigation.moveStop(from: $0, to: $1) }
                    Button { addingStop = true } label: { Label("Add a stop", systemImage: "plus.circle.fill") }
                        .disabled(navigation.itineraryStops.count >= 12 || navigation.hasArrived)
                }
                Section {
                    Label(navigation.destinationTitle ?? "Destination", systemImage: "flag.fill")
                } header: { Text("Final destination") }
                footer: { Text("Use Edit to remove or reorder stops. Changes recalculate your trip. Up to 12 intermediate stops are supported.") }
            }
            .scrollContentBackground(.hidden)
            .background(BrakeTheme.background)
            .foregroundStyle(BrakeTheme.text)
            .navigationTitle("Your itinerary").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } }
                ToolbarItem(placement: .primaryAction) { EditButton() }
            }
            .sheet(isPresented: $addingStop) {
                NavigationPlaceSearchSheet(route: navigation.route, origin: navigation.mapLocation,
                    select: { navigation.addStop(result: $0) }, routePath: navigation.routePath)
            }
        }
        .font(.brake(.body))
        .tint(BrakeTheme.accent)
    }
}
