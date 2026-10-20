import CoreLocation
import MapKit
import SwiftUI

enum NavigationPlaceCategory: String, CaseIterable, Identifiable {
    case gas = "Gas", charging = "EV charging", parking = "Parking", food = "Food", coffee = "Coffee"
    var id: String { rawValue }
    var symbol: String {
        switch self {
        case .gas: return "fuelpump.fill"
        case .charging: return "bolt.car.fill"
        case .parking: return "parkingsign.circle.fill"
        case .food: return "fork.knife"
        case .coffee: return "cup.and.saucer.fill"
        }
    }
    var searchTerm: String {
        switch self {
        case .gas: return "Gas stations"
        case .charging: return "Electric vehicle charging stations"
        case .parking: return "Parking"
        case .food: return "Restaurants"
        case .coffee: return "Coffee shops"
        }
    }
    var poiCategory: MKPointOfInterestCategory {
        switch self {
        case .gas: return .gasStation
        case .charging: return .evCharger
        case .parking: return .parking
        case .food: return .restaurant
        case .coffee: return .cafe
        }
    }
}

/// Browsing never changes the current destination until the driver selects a place.
struct NavigationPlaceSearchSheet: View {
    var route: MKRoute?
    var origin: CLLocation?
    var initialCategory: NavigationPlaceCategory?
    var actionTitle = "Add stop"
    var select: (NavigationSearchResult) -> Void
    var routePath: MKPolyline?
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var category: NavigationPlaceCategory?
    @State private var alongRoute = true
    @State private var results: [NavigationSearchResult] = []
    @State private var isSearching = false
    @State private var status = "Search for a place or choose a category."
    @State private var requests: [MKLocalSearch] = []
    @State private var searchID = UUID()
    @State private var submittedQuery = ""
    @State private var task: Task<Void, Never>?
    @FocusState private var focused: Bool

    var body: some View {
        SwiftUI.NavigationStack {
            List {
                Section {
                    HStack {
                        Image(systemName: "magnifyingglass").foregroundStyle(BrakeTheme.muted)
                        TextField("Place, address, or category", text: $query)
                            .submitLabel(.search).focused($focused)
                            .onSubmit { category = nil; search() }
                            .onChange(of: query) { _, value in
                                if value.trimmingCharacters(in: .whitespacesAndNewlines) != submittedQuery {
                                    cancelSearch()
                                    results = []
                                    status = "Submit your search to find places."
                                }
                            }
                        if isSearching { ProgressView() }
                        else { Button { category = nil; search() } label: { Image(systemName: "arrow.right.circle.fill") }
                                .disabled(query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) }
                    }
                    if route != nil || routePath != nil { Toggle("Search along my route", isOn: $alongRoute).onChange(of: alongRoute) { _, _ in search() } }
                    ScrollView(.horizontal) {
                        HStack(spacing: 8) {
                            ForEach(NavigationPlaceCategory.allCases) { option in
                                Button {
                                    category = option
                                    query = option.searchTerm
                                    search()
                                } label: {
                                    Label(option.rawValue, systemImage: option.symbol)
                                        .font(.brake(.caption, weight: .semibold)).padding(.horizontal, 10).padding(.vertical, 10)
                                        .background(category == option ? BrakeTheme.accent.opacity(0.18) : BrakeTheme.panel, in: Capsule())
                                }.buttonStyle(.plain)
                            }
                        }
                    }.scrollIndicators(.hidden)
                }
                Section {
                    if results.isEmpty {
                        Text(status).font(.brake(.subheadline)).foregroundStyle(BrakeTheme.muted).padding(.vertical, 16)
                    } else {
                        ForEach(results) { result in
                            Button {
                                cancelSearch()
                                select(result)
                                dismiss()
                            } label: {
                                HStack(spacing: 12) {
                                    Image(systemName: category?.symbol ?? "mappin.circle.fill")
                                        .foregroundStyle(BrakeTheme.accent).frame(width: 28)
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(result.title).font(.brake(.subheadline, weight: .semibold)).foregroundStyle(BrakeTheme.text)
                                        Text(result.subtitle).font(.brake(.caption)).foregroundStyle(BrakeTheme.muted).lineLimit(2)
                                    }
                                    Spacer(minLength: 4)
                                    Image(systemName: actionTitle == "Add stop" ? "plus.circle.fill" : "arrow.up.right.circle.fill")
                                        .foregroundStyle(BrakeTheme.accent)
                                }.padding(.vertical, 6)
                            }
                            .accessibilityLabel("\(actionTitle): \(result.title), \(result.subtitle)")
                        }
                    }
                } header: { Text("Places") } footer: {
                    Text((route != nil || routePath != nil) && alongRoute ? "Places within 3 km of the remaining route. Adding a stop recalculates your trip; opening hours and charging availability should be checked with the provider."
                        : "Results are near your GPS location. Opening hours and charging availability should be checked with the provider.")
                }
            }
            .scrollContentBackground(.hidden)
            .background(BrakeTheme.background)
            .foregroundStyle(BrakeTheme.text)
            .navigationTitle(actionTitle == "Add stop" ? "Find a stop" : "Explore nearby")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } } }
            .task {
                if let initialCategory { category = initialCategory; query = initialCategory.searchTerm; search() }
            }
            .onDisappear { cancelSearch() }
        }
        .font(.brake(.body))
        .tint(BrakeTheme.accent)
    }

    private func cancelSearch() {
        searchID = UUID()
        task?.cancel()
        requests.forEach { $0.cancel() }
        requests = []
        isSearching = false
    }

    @MainActor private func search() {
        let text = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        cancelSearch()
        submittedQuery = text
        focused = false
        let token = searchID
        let points = remainingRoutePoints()
        var centers: [CLLocationCoordinate2D] = []
        if alongRoute, !points.isEmpty {
            // At most three sequential requests limit network work on long trips.
            for fraction in [0.0, 0.45, 0.85] {
                let coordinate = points[min(points.count - 1, Int(Double(points.count - 1) * fraction))].coordinate
                if centers.allSatisfy({ MKMapPoint($0).distance(to: MKMapPoint(coordinate)) > 5_000 }) { centers.append(coordinate) }
            }
        } else if let origin { centers = [origin.coordinate] }
        if centers.isEmpty {
            status = "Wait for GPS to search nearby places."
            return
        }
        isSearching = true
        results = []
        status = "Searching places…"
        task = Task { @MainActor in
            var found: [NavigationSearchResult] = []
            var failures = 0
            for center in centers {
                guard !Task.isCancelled, searchID == token else { return }
                let request = MKLocalSearch.Request()
                request.naturalLanguageQuery = text
                request.resultTypes = category == nil ? [.address, .pointOfInterest] : [.pointOfInterest]
                request.region = MKCoordinateRegion(center: center, latitudinalMeters: 24_000, longitudinalMeters: 24_000)
                if let category { request.pointOfInterestFilter = MKPointOfInterestFilter(including: [category.poiCategory]) }
                let search = MKLocalSearch(request: request)
                requests.append(search)
                do {
                    let response = try await search.start()
                    guard !Task.isCancelled, searchID == token else { return }
                    for item in response.mapItems {
                        let coordinate = item.placemark.coordinate
                        guard CLLocationCoordinate2DIsValid(coordinate) else { continue }
                        if alongRoute, !points.isEmpty, distanceToRoute(MKMapPoint(coordinate), points: points) > 3_000 { continue }
                        guard !found.contains(where: { $0.title == item.name && MKMapPoint($0.item.placemark.coordinate).distance(to: MKMapPoint(coordinate)) < 150 }) else { continue }
                        let subtitle = [item.placemark.thoroughfare, item.placemark.locality, item.placemark.administrativeArea]
                            .compactMap { $0 }.joined(separator: ", ")
                        found.append(NavigationSearchResult(title: item.name ?? text, subtitle: subtitle, item: item))
                    }
                } catch { failures += 1 }
            }
            guard searchID == token, !Task.isCancelled else { return }
            if let origin {
                found.sort { MKMapPoint($0.item.placemark.coordinate).distance(to: MKMapPoint(origin.coordinate))
                    < MKMapPoint($1.item.placemark.coordinate).distance(to: MKMapPoint(origin.coordinate)) }
            }
            results = Array(found.prefix(25))
            requests = []
            isSearching = false
            status = failures == centers.count ? "Search couldn’t finish. Check your connection and try again."
                : "No matching places found nearby. Try a different search or turn off Search along my route."
        }
    }

    private func remainingRoutePoints() -> [MKMapPoint] {
        guard let line = routePath ?? route?.polyline, line.pointCount > 1 else { return [] }
        let points = Array(UnsafeBufferPointer(start: line.points(), count: line.pointCount))
        guard let origin else { return points }
        let position = MKMapPoint(origin.coordinate)
        let closest = points.indices.min { points[$0].distance(to: position) < points[$1].distance(to: position) } ?? 0
        return Array(points.dropFirst(closest))
    }

    private func distanceToRoute(_ point: MKMapPoint, points: [MKMapPoint]) -> Double {
        guard let first = points.first else { return .infinity }
        var minimum = point.distance(to: first)
        for index in 1..<points.count {
            let start = points[index - 1], end = points[index]
            let dx = end.x - start.x, dy = end.y - start.y
            let squared = dx * dx + dy * dy
            let fraction = squared > 0 ? min(1, max(0, ((point.x - start.x) * dx + (point.y - start.y) * dy) / squared)) : 0
            minimum = min(minimum, point.distance(to: MKMapPoint(x: start.x + dx * fraction, y: start.y + dy * fraction)))
        }
        return minimum
    }
}
