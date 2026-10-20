import CoreLocation
import SwiftUI

struct NavigationCoordinateDestinationSheet: View {
    @ObservedObject var navigation: NavigationService
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var latitude = ""
    @State private var longitude = ""

    private var coordinate: CLLocationCoordinate2D? {
        guard let lat = Double(latitude.trimmingCharacters(in: .whitespacesAndNewlines)),
              let lon = Double(longitude.trimmingCharacters(in: .whitespacesAndNewlines)),
              lat.isFinite, lon.isFinite else { return nil }
        let coordinate = CLLocationCoordinate2D(latitude: lat, longitude: lon)
        return CLLocationCoordinate2DIsValid(coordinate) ? coordinate : nil
    }

    var body: some View {
        SwiftUI.NavigationStack {
            Form {
                Section {
                    TextField("Name (optional)", text: $title)
                    TextField("Latitude, e.g. 40.7128", text: $latitude)
                        .keyboardType(.numbersAndPunctuation).autocorrectionDisabled().textInputAutocapitalization(.never)
                    TextField("Longitude, e.g. -74.0060", text: $longitude)
                        .keyboardType(.numbersAndPunctuation).autocorrectionDisabled().textInputAutocapitalization(.never)
                } header: { Text("Destination") }
                footer: { Text("Use decimal degrees. Latitude is −90 to 90; longitude is −180 to 180. Offline roads must cover your location and destination.") }
            }
            .scrollContentBackground(.hidden)
            .background(BrakeTheme.background)
            .foregroundStyle(BrakeTheme.text)
            .navigationTitle("Route to coordinates").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Plan route") {
                        guard let coordinate else { return }
                        navigation.chooseCoordinate(latitude: coordinate.latitude, longitude: coordinate.longitude,
                            title: title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "Coordinate destination" : title)
                        dismiss()
                    }.disabled(coordinate == nil)
                }
            }
        }.font(.brake(.body)).tint(BrakeTheme.accent)
    }
}
