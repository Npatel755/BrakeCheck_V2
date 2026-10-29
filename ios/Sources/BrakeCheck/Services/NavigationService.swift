import AVFoundation
import BrakeCheckCore
import Combine
import CoreLocation
import MapKit

extension NavigationService: @preconcurrency MKLocalSearchCompleterDelegate {
    func completerDidUpdateResults(_ completer: MKLocalSearchCompleter) {
        guard self.completer === completer, foreground, pageVisible, liveModeAllowed,
              query.trimmingCharacters(in: .whitespacesAndNewlines) == completer.queryFragment else { return }
        suggestionsLoading = false
        suggestionError = nil
        suggestions = completer.results.prefix(8).map { NavigationSuggestion(completion: $0) }
    }

    func completer(_ completer: MKLocalSearchCompleter, didFailWithError error: Error) {
        guard self.completer === completer else { return }
        suggestionsLoading = false
        suggestions = []
        suggestionError = "Suggestions unavailable. You can still submit a full address."
    }
}

struct NavigationSearchResult: Identifiable {
    let id = UUID()
    let title: String
    let subtitle: String
    let item: MKMapItem
}

struct NavigationSuggestion: Identifiable {
    let id = UUID()
    let completion: MKLocalSearchCompletion
    var title: String { completion.title }
    var subtitle: String { completion.subtitle }
}

/// Foreground navigation has its own location stream. These fixes never enter
/// the collision estimator, the measured-speed filter, or the trip speed log.
@MainActor
final class NavigationService: NSObject, ObservableObject {
    @Published var query = "" {
        didSet {
            guard query != oldValue else { return }
            cancelSearch()
            results = []
            updateSuggestions()
        }
    }
    @Published private(set) var results: [NavigationSearchResult] = []
    @Published private(set) var recentDestinations: [RecentDestination] = []
    @Published private(set) var suggestions: [NavigationSuggestion] = []
    @Published private(set) var suggestionsLoading = false
    @Published private(set) var suggestionError: String?
    private var completer: MKLocalSearchCompleter?
    private var completionTask: Task<Void, Never>?
    @Published private(set) var isSearching = false
    @Published private(set) var isCalculating = false
    @Published private(set) var destinationTitle: String?
    @Published private(set) var route: MKRoute?
    @Published private(set) var alternatives: [MKRoute] = []
    @Published private(set) var selectedRouteIndex = 0
    var alternativeTravelTimes: [TimeInterval] { plannedAlternatives.map(\.expectedTravelTime) }
    var alternativeDistances: [Double] { plannedAlternatives.map(\.distance) }
    @Published private(set) var activeRoute: SavedNavigationRoute?
    @Published private(set) var routePath: MKPolyline?
    @Published private(set) var itineraryStops: [RecentDestination] = []
    @Published private(set) var favorites: [RecentDestination] = []
    @Published private(set) var savedRoutes: [SavedNavigationRoute] = []
    @Published private(set) var avoidTolls = UserDefaults.standard.bool(forKey: "navigation.avoidTolls")
    @Published private(set) var avoidHighways = UserDefaults.standard.bool(forKey: "navigation.avoidHighways")
    @Published private(set) var departureDate: Date?
    @Published private(set) var userPaused = false
    @Published private(set) var offlineGraphReady = false
    @Published var preferOfflineRouting = false
    @Published private(set) var offlineGraphName: String?
    @Published private(set) var offlineAttribution: String?
    @Published private(set) var offlineImportWarning: String?
    @Published private(set) var positionEstimated = false
    @Published private(set) var routeConfidence = 0.0
    @Published private(set) var trafficNotice: String?
    @Published private(set) var betterRoute: MKRoute?
    @Published private(set) var trafficHistory: [NavigationTrafficObservation] = []
    @Published private(set) var stopAndGo = false
    @Published private(set) var displayedLocation: CLLocation?
    var hasPlannedRoute: Bool { activeRoute != nil }
    var canStartGuidance: Bool { hasPlannedRoute && hasFreshLocation && !isCalculating && !requiresNewPlan }
    var destinationCoordinate: CLLocationCoordinate2D? { destination?.placemark.coordinate }
    var shareURL: URL? {
        guard let coordinate = destinationCoordinate else { return nil }
        var parts = URLComponents(string: "https://maps.apple.com/")!
        parts.queryItems = [URLQueryItem(name: "daddr", value: "\(coordinate.latitude),\(coordinate.longitude)"),
                            URLQueryItem(name: "q", value: destinationTitle)]
        return parts.url
    }
    var shareText: String? {
        guard let title = destinationTitle else { return nil }
        let departure = isNavigating ? Date() : (departureDate ?? Date())
        let arrival = remainingTime.map { departure.addingTimeInterval($0).formatted(date: .omitted, time: .shortened) }
        return title + (arrival.map { " · Estimated arrival " + $0 } ?? "")
    }
    @Published private(set) var isNavigating = false
    @Published private(set) var hasArrived = false
    @Published private(set) var instruction = "Choose a destination"
    @Published private(set) var routeNotice: String?
    @Published private(set) var upcomingInstruction: String?
    @Published private(set) var isRejoiningRoute = false
    @Published private(set) var maneuverDistance: Double?
    @Published private(set) var remainingDistance: Double?
    @Published private(set) var remainingTime: Double?
    @Published private(set) var status = "Search for an address or place."
    @Published private(set) var errorMessage: String?
    @Published private(set) var location: CLLocation?
    @Published private(set) var lastKnownLocation: CLLocation?
    @Published private(set) var resolvingDestination: String?
    @Published private(set) var routeUsesLastKnownLocation = false
    var mapLocation: CLLocation? { displayedLocation ?? (hasFreshLocation ? location : lastKnownLocation) }
    @Published private(set) var hasFreshLocation = false
    @Published private(set) var isPaused = false
    @Published var spokenDirections = UserDefaults.standard.object(forKey: "navigation.spokenDirections") as? Bool ?? true {
        didSet {
            UserDefaults.standard.set(spokenDirections, forKey: "navigation.spokenDirections")
            if !spokenDirections { stopSpeaking() }
        }
    }
    var useMPH = true
    /// The model synchronously checks safety-audio ownership and live state.
    var canSpeak: (() -> Bool)?

    private let manager = CLLocationManager()
    private let speech = AVSpeechSynthesizer()
    private var currentUtterance: AVSpeechUtterance?
    private var ownsAudioSession = false
    @Published private(set) var foreground = true
    private var liveModeAllowed = true
    @Published private(set) var pageVisible = false
    private var locationRunning = false
    private var timer: Timer?
    private var interruptionObserver: NSObjectProtocol?
    private var destination: MKMapItem?
    private var destinationID = UUID()
    private var searchRequest: MKLocalSearch?
    private var searchID = UUID()
    private var directionsRequest: MKDirections?
    private var directionsID = UUID()
    private var needsRoute = false
    private var requiresNewPlan = false
    private var graphImportID = UUID()
    private var routeRevision = UUID()
    private var routeProgress: NavigationProgressTracker?
    private var steps: [NavigationRouteStep] = []
    private var plannedAlternatives: [SavedNavigationRoute] = []
    private var offlineRouter: OfflineNavigationRouter?
    private var planningTask: Task<Void, Never>?
    private var trafficTask: Task<Void, Never>?
    private var trafficDirections: MKDirections?
    private var trafficRequestID = UUID()
    private var etaEstimator = NavigationETAEstimator()
    private var presentationEstimator = NavigationPresentationEstimator()
    private var lastTrafficCheck = Date.distantPast
    private var trafficTimeMultiplier = 1.0
    private var tripDeparture: Date?
    private var tripBaseline = 0.0
    private var tripDistance = 0.0
    private var tripHistoryKey: String?
    private var tripPausedSeconds = 0.0
    private var pausedAt: Date?
    private var reachedStopIDs: Set<UUID> = []
    private var stopArrivalCounts: [UUID: Int] = [:]
    private var tripReplanned = false
    private var movementSamples: [(Date, Double)] = []
    private var lastFixTimestamp: Date?
    private var locationEligibleAfter = Date.distantFuture
    private var lastSpokenPrompt: String?
    private var voiceProgress = NavigationVoiceProgress()
    private var activeVoicePrompt: UUID?
    private var lastRerouteAt = Date.distantPast
    private var recovery = NavigationRecovery()

    override init() {
        super.init()
        if let data = UserDefaults.standard.data(forKey: "navigation.recents"),
           let saved = try? JSONDecoder().decode([RecentDestination].self, from: data) {
            recentDestinations = RecentDestinations.normalized(saved)
        }
        if let values = UserDefaults.standard.array(forKey: "navigation.lastGPS") as? [Double], values.count == 4,
           values.allSatisfy({ $0.isFinite }), (0...100).contains(values[2]),
           values[3] <= Date().timeIntervalSince1970 {
            let coordinate = CLLocationCoordinate2D(latitude: values[0], longitude: values[1])
            if CLLocationCoordinate2DIsValid(coordinate) {
                lastKnownLocation = CLLocation(coordinate: coordinate, altitude: 0,
                    horizontalAccuracy: values[2], verticalAccuracy: -1, timestamp: Date(timeIntervalSince1970: values[3]))
            }
        }
        loadNavigationLibrary()
        manager.delegate = self
        manager.activityType = .automotiveNavigation
        manager.desiredAccuracy = kCLLocationAccuracyBestForNavigation
        manager.distanceFilter = kCLDistanceFilterNone
        manager.pausesLocationUpdatesAutomatically = false
        manager.allowsBackgroundLocationUpdates = false
        speech.delegate = self
        interruptionObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification, object: nil, queue: .main
        ) { [weak self] notification in
            guard let raw = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  raw == AVAudioSession.InterruptionType.began.rawValue else { return }
            Task { @MainActor [weak self] in self?.stopSpeaking(deactivateSession: false) }
        }
    }

    func setPageVisible(_ visible: Bool) {
        pageVisible = visible
        if visible { updateSuggestions() } else { cancelSuggestions() }
        reconcileLocation()
    }

    func setForeground(_ active: Bool) {
        guard foreground != active else { return }
        foreground = active
        if !active {
            cancelSuggestions()
            cancelSearch()
            cancelDirections()
            cancelTrafficRefresh()
            displayedLocation = nil
            presentationEstimator.reset()
            stopSpeaking()
            location = nil
            hasFreshLocation = false
            lastFixTimestamp = nil
            pauseMeasurements("Navigation paused. Return to BrakeCheck to resume.")
        }
        if active { updateSuggestions() }
        reconcileLocation()
    }

    func setLiveModeAllowed(_ allowed: Bool) {
        liveModeAllowed = allowed
        if !allowed {
            cancelSuggestions()
            stopGuidance()
            cancelSearch()
            cancelDirections()
            location = nil
            hasFreshLocation = false
            status = "Navigation is available for a live drive, outside replay or simulation."
        }
        reconcileLocation()
    }

    func prepareLocation() {
        reconcileLocation()
    }

    func search() {
        let text = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard foreground, liveModeAllowed, !text.isEmpty else { return }
        cancelSearch()
        errorMessage = nil
        results = []
        let request = MKLocalSearch.Request()
        request.naturalLanguageQuery = text
        request.resultTypes = [.address, .pointOfInterest]
        performSearch(request, text: text, chooseSingle: false)
    }

    func chooseSuggestion(_ suggestion: NavigationSuggestion) {
        guard foreground, liveModeAllowed else { return }
        query = suggestion.title
        let text = query.trimmingCharacters(in: .whitespacesAndNewlines)
        cancelSearch()
        resolvingDestination = suggestion.title
        results = []
        errorMessage = nil
        performSearch(MKLocalSearch.Request(completion: suggestion.completion), text: text, chooseSingle: true)
    }

    private func performSearch(_ request: MKLocalSearch.Request, text: String, chooseSingle: Bool) {
        cancelSuggestions()
        if hasFreshLocation, let location {
            request.region = MKCoordinateRegion(center: location.coordinate,
                latitudinalMeters: 50_000, longitudinalMeters: 50_000)
        }
        let search = MKLocalSearch(request: request)
        let token = searchID
        searchRequest = search
        isSearching = true
        status = "Searching places…"
        search.start { [weak self] response, error in
            Task { @MainActor [weak self] in
                guard let self, self.searchID == token, self.foreground,
                      self.liveModeAllowed, self.query.trimmingCharacters(in: .whitespacesAndNewlines) == text else { return }
                self.isSearching = false
                self.searchRequest = nil
                self.resolvingDestination = nil
                guard let response else {
                    self.errorMessage = "Search could not finish. Check your connection and try again."
                    self.status = "Search unavailable"
                    return
                }
                self.results = response.mapItems.prefix(12).map { item in
                    NavigationSearchResult(title: item.name ?? "Destination",
                        subtitle: item.placemark.title ?? "", item: item)
                }
                self.status = self.results.isEmpty ? "No matching places. Try a fuller address." : "Choose your destination."
                if chooseSingle, self.results.count == 1, let result = self.results.first {
                    self.chooseDestination(result)
                }
            }
        }
    }

    func chooseDestination(_ result: NavigationSearchResult) {
        guard foreground, liveModeAllowed else { return }
        let coordinate = result.item.placemark.coordinate
        recentDestinations = RecentDestinations.adding(RecentDestination(title: result.title,
            subtitle: result.subtitle, latitude: coordinate.latitude, longitude: coordinate.longitude),
            to: recentDestinations)
        saveRecents()
        stopGuidance()
        cancelSearch()
        cancelSuggestions()
        cancelDirections()
        destination = result.item
        destinationTitle = result.title
        destinationID = UUID()
        route = nil
        activeRoute = nil
        routePath = nil
        alternatives = []
        plannedAlternatives = []
        itineraryStops = []
        reachedStopIDs = []
        routeProgress = nil
        results = []
        hasArrived = false
        instruction = "Planning your route"
        errorMessage = nil
        needsRoute = true
        reconcileLocation()
        calculateRoute()
    }

    func chooseRecent(_ recent: RecentDestination) {
        guard recent.valid else { return }
        query = recent.title
        let item = MKMapItem(placemark: MKPlacemark(coordinate: CLLocationCoordinate2D(
            latitude: recent.latitude, longitude: recent.longitude)))
        item.name = recent.title
        chooseDestination(NavigationSearchResult(title: recent.title, subtitle: recent.subtitle, item: item))
    }

    func chooseCoordinate(latitude: Double, longitude: Double, title: String = "Map destination") {
        let point = CLLocationCoordinate2D(latitude: latitude, longitude: longitude)
        guard CLLocationCoordinate2DIsValid(point) else { errorMessage = "Enter a valid latitude and longitude."; return }
        let item = MKMapItem(placemark: MKPlacemark(coordinate: point)); item.name = title
        chooseDestination(NavigationSearchResult(title: title, subtitle: "\(latitude), \(longitude)", item: item))
    }

    func removeRecent(_ id: UUID) {
        recentDestinations.removeAll { $0.id == id }; saveRecents()
    }
    func clearRecents() { recentDestinations = []; saveRecents() }
    private func saveRecents() {
        if let data = try? JSONEncoder().encode(recentDestinations) {
            UserDefaults.standard.set(data, forKey: "navigation.recents")
        }
    }

    private func cancelSuggestions() {
        completionTask?.cancel(); completionTask = nil
        completer?.delegate = nil
        completer?.cancel(); completer = nil
        suggestions = []
        suggestionsLoading = false
        suggestionError = nil
    }

    private func updateSuggestions() {
        cancelSuggestions()
        let text = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard foreground, liveModeAllowed, pageVisible, !isNavigating, text.count >= 2 else { return }
        suggestionsLoading = true
        completionTask = Task { @MainActor [weak self] in
            do { try await Task.sleep(nanoseconds: 250_000_000) } catch { return }
            guard let self, !Task.isCancelled, self.query.trimmingCharacters(in: .whitespacesAndNewlines) == text else { return }
            let completer = MKLocalSearchCompleter()
            completer.resultTypes = [.address, .pointOfInterest]
            completer.delegate = self
            if self.hasFreshLocation, let location = self.location {
                completer.region = MKCoordinateRegion(center: location.coordinate, latitudinalMeters: 50_000, longitudinalMeters: 50_000)
            }
            self.completer = completer
            completer.queryFragment = text
        }
    }

    func retryRoute() {
        guard foreground, liveModeAllowed, destination != nil else { return }
        errorMessage = nil
        recovery.reset()
        requiresNewPlan = true
        needsRoute = true
        reconcileLocation()
        calculateRoute()
    }

    func clearDestination() {
        stopGuidance()
        cancelDirections()
        cancelSearch()
        destinationID = UUID()
        destination = nil
        destinationTitle = nil
        route = nil
        activeRoute = nil
        routePath = nil
        alternatives = []
        plannedAlternatives = []
        itineraryStops = []
        reachedStopIDs = []
        routeProgress = nil
        steps = []
        needsRoute = false
        requiresNewPlan = false
        hasArrived = false
        maneuverDistance = nil
        remainingDistance = nil
        remainingTime = nil
        instruction = "Choose a destination"
        errorMessage = nil
        status = "Search for an address or place."
    }

    func startGuidance() {
        guard foreground, liveModeAllowed, canStartGuidance else {
            errorMessage = "A route and fresh GPS location are needed to start navigation."
            return
        }
        isNavigating = true
        userPaused = false
        tripDeparture = Date()
        tripBaseline = activeRoute?.expectedTravelTime ?? 0
        tripDistance = activeRoute?.distance ?? 0
        tripHistoryKey = activeRoute?.historyKey
        tripPausedSeconds = 0
        tripReplanned = false
        pausedAt = nil
        reachedStopIDs = []
        stopArrivalCounts = [:]
        etaEstimator.reset()
        trafficTimeMultiplier = 1
        lastTrafficCheck = .distantPast
        hasArrived = false
        isPaused = false
        lastSpokenPrompt = nil
        voiceProgress.reset()
        resetProgress()
        reconcileLocation()
        if let location { updateProgress(location) }
    }

    /// Ending navigation leaves the route preview and the camera trip intact.
    func stopGuidance() {
        stopSpeaking()
        presentationEstimator.reset(); displayedLocation = nil; positionEstimated = false
        routeConfidence = 0
        movementSamples = []; stopAndGo = false
        isNavigating = false
        userPaused = false
        pausedAt = nil
        cancelTrafficRefresh()
        tripDeparture = nil
        betterRoute = nil
        trafficNotice = nil
        isRejoiningRoute = false
        upcomingInstruction = nil
        recovery.reset()
        lastRerouteAt = .distantPast
        isPaused = false
        hasArrived = false
        cancelDirections()
        needsRoute = requiresNewPlan && destination != nil
        lastSpokenPrompt = nil
        voiceProgress.reset()
        routeNotice = nil
        resetProgress()
        maneuverDistance = nil
        remainingDistance = activeRoute?.distance
        remainingTime = activeRoute?.expectedTravelTime
        instruction = activeRoute == nil ? "Choose a destination" : "Route preview"
        status = requiresNewPlan ? "Updating the route for your stops and preferences."
            : activeRoute == nil ? "Search for an address or place." : "Ready when you are."
        reconcileLocation()
    }

    func stopSpeaking(deactivateSession: Bool = true) {
        // Invalidate ownership before stopSpeaking emits cancellation callbacks.
        if currentUtterance != nil { lastSpokenPrompt = nil }
        voiceProgress.cancel()
        activeVoicePrompt = nil
        currentUtterance = nil
        let owned = ownsAudioSession
        ownsAudioSession = false
        speech.stopSpeaking(at: .immediate)
        if deactivateSession, owned {
            try? AVAudioSession.sharedInstance().setActive(false, options: [.notifyOthersOnDeactivation])
        }
    }

    private func reconcileLocation() {
        let wanted = foreground && liveModeAllowed && (pageVisible || isNavigating)
        guard wanted else {
            manager.stopUpdatingLocation()
            locationRunning = false
            locationEligibleAfter = Date.distantFuture
            timer?.invalidate()
            timer = nil
            location = nil
            hasFreshLocation = false
            lastFixTimestamp = nil
            return
        }
        if timer == nil {
            let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
                Task { @MainActor [weak self] in self?.checkFreshness() }
            }
            self.timer = timer
            RunLoop.main.add(timer, forMode: .common)
        }
        guard CLLocationManager.locationServicesEnabled() else {
            errorMessage = "Turn on Location Services to plan a route from your position."
            return
        }
        switch manager.authorizationStatus {
        case .notDetermined:
            manager.requestWhenInUseAuthorization()
        case .authorizedAlways, .authorizedWhenInUse:
            guard manager.accuracyAuthorization == .fullAccuracy else {
                manager.stopUpdatingLocation()
                locationRunning = false
                location = nil
                hasFreshLocation = false
                pauseMeasurements("Enable Precise Location for route guidance.")
                errorMessage = "Enable Precise Location for BrakeCheck in iPhone Settings."
                return
            }
            if !locationRunning {
                locationRunning = true
                locationEligibleAfter = Date()
                manager.startUpdatingLocation()
                status = isNavigating ? "Resuming · waiting for fresh GPS" : "Finding your location…"
            }
        case .denied, .restricted:
            lastKnownLocation = nil
            UserDefaults.standard.removeObject(forKey: "navigation.lastGPS")
            manager.stopUpdatingLocation()
            locationRunning = false
            location = nil
            hasFreshLocation = false
            pauseMeasurements("Location permission is needed for navigation.")
            errorMessage = "Allow Location and Precise Location for BrakeCheck in iPhone Settings."
        @unknown default:
            errorMessage = "Location permission could not be determined."
        }
    }

    private func calculateRoute() {
        guard foreground, liveModeAllowed, let destination, !isCalculating, !userPaused,
              recovery.canAttempt(at: Date().timeIntervalSinceReferenceDate) else { return }
        if let cached = manager.location { cacheLocation(cached) }
        guard let origin = isNavigating ? (hasFreshLocation ? location : nil) : mapLocation else {
            status = "Finding a starting location. No previous GPS fix is available yet."
            return
        }
        routeUsesLastKnownLocation = !hasFreshLocation
        cancelDirections()
        let token = directionsID
        let targetID = destinationID
        let stops = itineraryStops.filter { !reachedStopIDs.contains($0.id) }
        let targets = stops.map { recent -> MKMapItem in
            let item = MKMapItem(placemark: MKPlacemark(coordinate: CLLocationCoordinate2D(
                latitude: recent.latitude, longitude: recent.longitude)))
            item.name = recent.title
            return item
        } + [destination]
        isCalculating = true
        needsRoute = false
        status = isNavigating ? "Finding a new route..." : "Comparing driving routes..."
        if isNavigating { stopSpeaking() }
        if preferOfflineRouting {
            tryOfflineRoute(origin: origin, stops: stops, targetID: targetID, token: token)
            return
        }
        planningTask = Task { @MainActor [weak self] in
            guard let self else { return }
            do {
                var legs: [[MKRoute]] = []
                var source = MKMapItem(placemark: MKPlacemark(coordinate: origin.coordinate))
                var departure = self.isNavigating ? Date() : (self.departureDate ?? Date())
                for (index, target) in targets.enumerated() {
                    try Task.checkCancellation()
                    let request = self.directionsRequest(from: source, to: target, departure: departure,
                                                         alternatives: index == 0)
                    let directions = MKDirections(request: request)
                    self.directionsRequest = directions
                    let response = try await directions.calculate()
                    guard self.directionsID == token, self.destinationID == targetID,
                          self.foreground, self.liveModeAllowed else { return }
                    let routes = response.routes.filter(Self.validAppleRoute).sorted { $0.expectedTravelTime < $1.expectedTravelTime }
                    guard let fastest = routes.first else { throw NavigationPlanningError.noRoute }
                    legs.append(routes)
                    departure = departure.addingTimeInterval(fastest.expectedTravelTime)
                    source = target
                }
                guard !Task.isCancelled, self.directionsID == token, self.destinationID == targetID else { return }
                self.isCalculating = false
                self.directionsRequest = nil
                let choices = legs[0]
                self.alternatives = choices
                self.plannedAlternatives = choices.map { first in
                    self.archive(legs: [first] + legs.dropFirst().compactMap(\.first), stops: stops)
                }
                self.installRoute(self.plannedAlternatives[0], apple: choices[0], selectedIndex: 0)
            } catch {
                guard !Task.isCancelled, self.directionsID == token, self.destinationID == targetID else { return }
                self.directionsRequest = nil
                self.tryOfflineRoute(origin: origin, stops: stops, targetID: targetID, token: token)
            }
        }
    }

    private enum NavigationPlanningError: Error { case noRoute }
    private static func validAppleRoute(_ route: MKRoute) -> Bool {
        route.distance.isFinite && route.distance > 0 && route.expectedTravelTime.isFinite
            && route.expectedTravelTime >= 0 && route.steps.allSatisfy { $0.distance.isFinite && $0.distance >= 0 }
            && route.polyline.pointCount >= 2
    }
    private func directionsRequest(from: MKMapItem, to: MKMapItem, departure: Date,
                                   alternatives: Bool) -> MKDirections.Request {
        let request = MKDirections.Request()
        request.source = from; request.destination = to; request.transportType = .automobile
        request.requestsAlternateRoutes = alternatives
        request.departureDate = departure
        request.tollPreference = avoidTolls ? .avoid : .any
        request.highwayPreference = avoidHighways ? .avoid : .any
        return request
    }
    private static func coordinates(_ line: MKPolyline) -> [NavigationCoordinate] {
        UnsafeBufferPointer(start: line.points(), count: line.pointCount).map {
            NavigationCoordinate(latitude: $0.coordinate.latitude, longitude: $0.coordinate.longitude)
        }
    }
    private func archive(legs: [MKRoute], stops: [RecentDestination]) -> SavedNavigationRoute {
        let coordinates = legs.flatMap { Self.coordinates($0.polyline) }
        let instructions = legs.flatMap { leg in leg.steps.enumerated().map { index, step in
            let notices = ([step.notice] + (index == 0 ? leg.advisoryNotices.map(Optional.some) : []))
                .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
            return NavigationRouteStep(instruction: step.instructions, distance: step.distance,
                expectedTravelTime: leg.distance > 0 ? leg.expectedTravelTime * step.distance / leg.distance : 0,
                coordinates: Self.coordinates(step.polyline),
                notice: notices.isEmpty ? nil : notices)
        } }
        var cumulativeDistance = 0.0
        let routedStops = stops.enumerated().map { index, stop -> NavigationRouteStop in
            cumulativeDistance += legs[index].distance
            let endpoint = Self.coordinates(legs[index].polyline).last
                ?? NavigationCoordinate(latitude: stop.latitude, longitude: stop.longitude)
            return NavigationRouteStop(id: stop.id, title: stop.title, coordinate: endpoint,
                routeDistanceFromStart: cumulativeDistance)
        }
        return SavedNavigationRoute(title: destinationTitle ?? "Driving route", coordinates: coordinates,
            steps: instructions, distance: legs.reduce(0) { $0 + $1.distance },
            expectedTravelTime: legs.reduce(0) { $0 + $1.expectedTravelTime },
            stops: routedStops,
            preferences: .init(avoidTolls: avoidTolls, avoidMotorways: avoidHighways))
    }
    private func installRoute(_ saved: SavedNavigationRoute, apple: MKRoute?, selectedIndex: Int = 0) {
        guard saved.isValid else { routeFailed(); return }
        if isNavigating { tripReplanned = true }
        cancelTrafficRefresh()
        betterRoute = nil; trafficNotice = nil; stopArrivalCounts = [:]
        route = apple
        activeRoute = saved
        selectedRouteIndex = selectedIndex
        var coordinates = saved.coordinates.map { CLLocationCoordinate2D(latitude: $0.latitude, longitude: $0.longitude) }
        routePath = MKPolyline(coordinates: &coordinates, count: coordinates.count)
        steps = saved.steps
        routeRevision = UUID()
        recovery.reset(); needsRoute = false
        requiresNewPlan = false
        isRejoiningRoute = false
        hasArrived = false
        stopSpeaking()
        lastSpokenPrompt = nil; voiceProgress.reset()
        etaEstimator.reset()
        trafficTimeMultiplier = 1
        resetProgress()
        remainingDistance = saved.distance; remainingTime = saved.expectedTravelTime
        if saved.source != "Apple Maps", let pattern = NavigationTrafficHistory.pattern(for: saved.historyKey,
            departure: departureDate ?? Date(), observations: trafficHistory) {
            remainingTime = saved.expectedTravelTime * pattern.multiplier
        }
        instruction = "Route preview"
        routeNotice = saved.steps.compactMap(\.notice).first
        status = isNavigating ? "Navigation active" : "Route ready. Start navigation when parked."
        errorMessage = nil
        persistCurrentRoute(saved)
        if isNavigating, hasFreshLocation, let fix = location { updateProgress(fix) }
    }
    private func resetProgress() { routeProgress = activeRoute?.progressTracker() }
    private func routeFailed(_ message: String? = nil) {
        isCalculating = false
        errorMessage = message ?? "No route is available. Connect to Maps or import a road graph covering this trip."
        recovery.failed(at: Date().timeIntervalSinceReferenceDate)
        needsRoute = true
        if isNavigating { pauseMeasurements("Route unavailable. Reconnect or use a downloaded road region.") }
        else { status = "Route unavailable" }
    }
    private func tryOfflineRoute(origin: CLLocation, stops: [RecentDestination], targetID: UUID, token: UUID) {
        guard let router = offlineRouter, let destinationCoordinate else { routeFailed(); return }
        let start = NavigationCoordinate(latitude: origin.coordinate.latitude, longitude: origin.coordinate.longitude)
        let end = NavigationCoordinate(latitude: destinationCoordinate.latitude, longitude: destinationCoordinate.longitude)
        let preferences = NavigationRoutePreferences(avoidTolls: avoidTolls, avoidMotorways: avoidHighways)
        let title = destinationTitle ?? "Offline route"
        let heading = origin.course >= 0 && origin.speed >= 2 ? origin.course : nil
        planningTask = Task { @MainActor [weak self] in
            let result = await Task.detached(priority: .userInitiated) { () -> Result<SavedNavigationRoute, Error> in
                do {
                    let intermediate = stops.map { NavigationRouteStop(id: $0.id, title: $0.title,
                        coordinate: .init(latitude: $0.latitude, longitude: $0.longitude)) }
                    var result = try router.route(from: start,
                        stops: intermediate + [NavigationRouteStop(title: title, coordinate: end)],
                        preferences: preferences, initialBearing: heading, title: title)
                    result.stops.removeLast()
                    return .success(result)
                } catch { return .failure(error) }
            }.value
            guard let self, self.directionsID == token, self.destinationID == targetID,
                  self.foreground, !Task.isCancelled else { return }
            self.isCalculating = false
            switch result {
            case .success(let saved):
                self.alternatives = []; self.plannedAlternatives = []
                self.installRoute(saved, apple: nil)
                self.status = "Offline route ready. Road data may have changed since download."
            case .failure(let error): self.routeFailed(error.localizedDescription)
            }
        }
    }

    private func acceptLocations(_ locations: [CLLocation]) {
        guard foreground, liveModeAllowed, locationRunning,
              let fix = locations.max(by: { $0.timestamp < $1.timestamp }) else { return }
        cacheLocation(fix)
        let age = Date().timeIntervalSince(fix.timestamp)
        guard age.isFinite, (0...2).contains(age), CLLocationCoordinate2DIsValid(fix.coordinate),
              fix.horizontalAccuracy.isFinite, (0...35).contains(fix.horizontalAccuracy),
              fix.timestamp >= locationEligibleAfter,
              lastFixTimestamp.map({ fix.timestamp > $0 }) ?? true else { return }
        lastFixTimestamp = fix.timestamp
        if !isNavigating || isCalculating { updatePresentation(fix: fix) }
        location = fix
        hasFreshLocation = true
        if needsRoute { calculateRoute() }
        if isNavigating && !userPaused {
            if hasArrived { status = "Destination reached · end navigation when ready" }
            else { updateProgress(fix) }
        }
        else if !isNavigating && !isSearching && !isCalculating {
            status = activeRoute == nil ? "Search for an address or place." : "Route ready. Start navigation when parked."
        }
    }

    private func cacheLocation(_ fix: CLLocation) {
        let age = Date().timeIntervalSince(fix.timestamp)
        guard age.isFinite, age >= 0, CLLocationCoordinate2DIsValid(fix.coordinate),
              fix.horizontalAccuracy.isFinite, (0...100).contains(fix.horizontalAccuracy),
              lastKnownLocation.map({ fix.timestamp > $0.timestamp }) ?? true else { return }
        lastKnownLocation = fix
        UserDefaults.standard.set([fix.coordinate.latitude, fix.coordinate.longitude,
            fix.horizontalAccuracy, fix.timestamp.timeIntervalSince1970], forKey: "navigation.lastGPS")
    }

    private func updateProgress(_ fix: CLLocation) {
        guard isNavigating, !userPaused, !hasArrived, !isCalculating, var progress = routeProgress else { return }
        guard !requiresNewPlan else {
            pauseMeasurements("Waiting for an updated route for your stops and preferences.")
            return
        }
        guard let snapshot = progress.update(coordinate: NavigationCoordinate(
                latitude: fix.coordinate.latitude, longitude: fix.coordinate.longitude),
                horizontalAccuracy: fix.horizontalAccuracy,
                timestamp: fix.timestamp.timeIntervalSinceReferenceDate,
                now: Date().timeIntervalSinceReferenceDate,
                course: fix.speed >= 2 && fix.course >= 0 ? fix.course : nil) else {
            pauseMeasurements("GPS paused · waiting for a reliable route position")
            return
        }
        routeProgress = progress
        routeConfidence = snapshot.confidence
        if !snapshot.offRoute, snapshot.confidence >= 0.45, let snapped = snapshot.snappedCoordinate {
            updatePresentation(fix: fix, coordinate: snapped, bearing: snapshot.snappedBearing)
        } else { updatePresentation(fix: fix) }
        isPaused = false
        if !snapshot.offRoute, snapshot.distanceFromRoute > max(35, fix.horizontalAccuracy * 2 + 10) {
            pauseMeasurements("Checking route position · waiting for reliable GPS")
            return
        }
        if snapshot.offRoute {
            isRejoiningRoute = true
            isPaused = true
            stopSpeaking()
            status = "Off route · finding new directions"
            if Date().timeIntervalSince(lastRerouteAt) >= 20,
               recovery.canAttempt(at: Date().timeIntervalSinceReferenceDate) {
                lastRerouteAt = Date()
                needsRoute = true
                calculateRoute()
            }
            return
        }
        remainingDistance = snapshot.remainingMeters
        isRejoiningRoute = false
        if recovery.retryAt != nil && !requiresNewPlan {
            needsRoute = false
            recovery.reset()
            errorMessage = nil
        }
        if let saved = activeRoute {
            let pattern = NavigationTrafficHistory.pattern(for: saved.historyKey, departure: Date(), observations: trafficHistory)
            remainingTime = etaEstimator.update(remainingDistance: snapshot.remainingMeters,
                routeDistance: saved.distance, expectedTravelTime: saved.expectedTravelTime * trafficTimeMultiplier,
                timestamp: fix.timestamp.timeIntervalSinceReferenceDate,
                historicalMultiplier: saved.source == "Apple Maps" ? 1 : (pattern?.multiplier ?? 1))?.remainingTime
        }
        markReachedStop(fix, progress: snapshot.progressMeters)
        updateTrafficMovement(fix)
        if snapshot.arrived && itineraryStops.allSatisfy({ reachedStopIDs.contains($0.id) }) {
            recordCompletedTrip()
            hasArrived = true
            instruction = "You’ve arrived"
            upcomingInstruction = nil
            routeNotice = nil
            maneuverDistance = nil
            remainingDistance = 0
            remainingTime = 0
            status = "Destination reached · end navigation when ready"
            announce("You have arrived at your destination.", key: "\(routeRevision):arrived")
            return
        }
        let index = min(max(0, snapshot.stepIndex), max(0, steps.count - 1))
        let effectiveIndex = steps.indices.dropFirst(index).first { !steps[$0].instruction.isEmpty } ?? index
        let step = steps.indices.contains(effectiveIndex) ? steps[effectiveIndex] : nil
        instruction = step?.instruction.isEmpty == false ? step!.instruction : "Continue on your route"
        upcomingInstruction = steps.indices.dropFirst(effectiveIndex + 1)
            .first(where: { !steps[$0].instruction.isEmpty }).map { steps[$0].instruction }
        routeNotice = step?.notice
        let spokenMeters = NavigationVoiceGuidance.distanceToManeuver(projectedStep: index,
            instructionStep: effectiveIndex, distanceToStep: snapshot.distanceToStep,
            stepDistances: steps.map(\.distance), routeDistance: activeRoute?.distance ?? 0)
            ?? snapshot.distanceToStep
        maneuverDistance = spokenMeters
        status = "Navigation active"
        guard let stage = NavigationVoiceGuidance.stage(distance: spokenMeters, speed: fix.speed),
              voiceProgress.shouldAnnounce(step: effectiveIndex, stage: stage) else { return }
        let prefix = stage == 3 || effectiveIndex == 0 ? "" : "In \(spokenDistance(spokenMeters)), "
        if announce(prefix + instruction, key: "\(routeRevision):\(effectiveIndex):\(stage)") {
            activeVoicePrompt = voiceProgress.begin(step: effectiveIndex, stage: stage)
        }
    }

    private func checkFreshness() {
        let fresh = location.map { (0...2).contains(Date().timeIntervalSince($0.timestamp)) } ?? false
        hasFreshLocation = fresh
        updatePresentationEstimate()
        if !fresh, isNavigating, !userPaused, !hasArrived { pauseMeasurements("GPS paused · waiting for a fresh location") }
        if fresh, foreground, liveModeAllowed, !userPaused, needsRoute, !isCalculating,
           recovery.canAttempt(at: Date().timeIntervalSinceReferenceDate) { calculateRoute() }
        if fresh, isNavigating, !userPaused, !hasArrived, !isCalculating, !isRejoiningRoute,
           Date().timeIntervalSince(lastTrafficCheck) >= 180 { refreshTraffic() }
    }

    private func pauseMeasurements(_ reason: String) {
        stopSpeaking()
        if isNavigating && !hasArrived {
            isPaused = true
            // Keep the last measured guidance on screen. A stale fix never
            // advances route progress or triggers a spoken maneuver.
        }
        status = reason
    }

    private func spokenDistance(_ meters: Double) -> String {
        if useMPH {
            let feet = meters * 3.28084
            return feet >= 2640 ? String(format: "%.1f miles", meters / 1609.344)
                : "\(Int((feet / 50).rounded()) * 50) feet"
        }
        return meters >= 1000 ? String(format: "%.1f kilometers", meters / 1000)
            : "\(max(10, Int((meters / 10).rounded()) * 10)) meters"
    }

    @discardableResult
    private func announce(_ text: String, key: String) -> Bool {
        guard foreground, liveModeAllowed, isNavigating, spokenDirections, hasFreshLocation,
              !isPaused, !isCalculating, key != lastSpokenPrompt, canSpeak?() == true else { return false }
        stopSpeaking(deactivateSession: false)
        // No queued turns: every prompt is based on the latest accepted fix.
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(.playback, mode: .spokenAudio, options: [.duckOthers])
            try session.setActive(true)
        } catch { return false }
        let utterance = AVSpeechUtterance(string: text)
        SpeechVoicePreferences.configure(utterance, language: Locale.preferredLanguages.first ?? "en-US")
        currentUtterance = utterance
        ownsAudioSession = true
        lastSpokenPrompt = key
        speech.speak(utterance)
        return true
    }

    private func completeSpeech(_ utterance: AVSpeechUtterance, completed: Bool) {
        guard currentUtterance === utterance else { return }
        if completed {
            if let activeVoicePrompt { voiceProgress.finish(activeVoicePrompt) }
        } else {
            voiceProgress.cancel()
            lastSpokenPrompt = nil
        }
        activeVoicePrompt = nil
        currentUtterance = nil
        let owned = ownsAudioSession
        ownsAudioSession = false
        if owned, canSpeak?() == true {
            try? AVAudioSession.sharedInstance().setActive(false, options: [.notifyOthersOnDeactivation])
        }
    }

    func selectRoute(at index: Int) {
        guard alternatives.indices.contains(index), plannedAlternatives.indices.contains(index), !isCalculating else { return }
        installRoute(plannedAlternatives[index], apple: alternatives[index], selectedIndex: index)
    }

    func updateRoutingPreferences(avoidTolls: Bool, avoidHighways: Bool, departureDate: Date?) {
        self.avoidTolls = avoidTolls; self.avoidHighways = avoidHighways
        self.departureDate = departureDate
        UserDefaults.standard.set(avoidTolls, forKey: "navigation.avoidTolls")
        UserDefaults.standard.set(avoidHighways, forKey: "navigation.avoidHighways")
        cancelDirections(); recovery.reset()
        requiresNewPlan = destination != nil
        if destination != nil { needsRoute = true; calculateRoute() }
    }

    func addStop(result: NavigationSearchResult) {
        guard destination != nil, itineraryStops.count < 12 else {
            errorMessage = "Choose a final destination first. A trip supports up to 12 intermediate stops."
            return
        }
        let point = result.item.placemark.coordinate
        let stop = RecentDestination(title: result.title, subtitle: result.subtitle,
            latitude: point.latitude, longitude: point.longitude)
        guard stop.valid else { return }
        itineraryStops.append(stop)
        replanItinerary()
    }
    func removeStop(id: UUID) {
        itineraryStops.removeAll { $0.id == id }; reachedStopIDs.remove(id); replanItinerary()
    }
    func moveStop(from offsets: IndexSet, to target: Int) {
        guard offsets.allSatisfy(itineraryStops.indices.contains), (0...itineraryStops.count).contains(target) else { return }
        let moved = offsets.sorted().map { itineraryStops[$0] }
        let insertion = target - offsets.filter { $0 < target }.count
        for index in offsets.sorted(by: >) { itineraryStops.remove(at: index) }
        itineraryStops.insert(contentsOf: moved, at: min(insertion, itineraryStops.count))
        replanItinerary()
    }
    private func replanItinerary() {
        cancelDirections(); cancelTrafficRefresh(); recovery.reset(); betterRoute = nil
        requiresNewPlan = true
        needsRoute = true; calculateRoute()
    }
    private func markReachedStop(_ fix: CLLocation, progress: Double) {
        guard let next = activeRoute?.stops.first(where: { !reachedStopIDs.contains($0.id) }) else { return }
        guard fix.horizontalAccuracy <= 20,
              next.routeDistanceFromStart.map({ progress >= $0 - 35 }) ?? true,
              fix.distance(from: CLLocation(latitude: next.coordinate.latitude, longitude: next.coordinate.longitude)) <= 35 else {
            stopArrivalCounts[next.id] = 0; return
        }
        stopArrivalCounts[next.id, default: 0] += 1
        guard stopArrivalCounts[next.id, default: 0] >= 2 else { return }
        reachedStopIDs.insert(next.id); status = "Reached stop: " + next.title
    }

    func pauseGuidance() {
        guard isNavigating, !hasArrived, !userPaused else { return }
        userPaused = true; pausedAt = Date(); cancelDirections(); cancelTrafficRefresh()
        pauseMeasurements("Trip paused. Resume when ready.")
        presentationEstimator.reset(); displayedLocation = nil; positionEstimated = false
    }
    func resumeGuidance() {
        guard isNavigating, userPaused else { return }
        if let pausedAt { tripPausedSeconds += max(0, Date().timeIntervalSince(pausedAt)) }
        pausedAt = nil; userPaused = false; etaEstimator.reset()
        if hasFreshLocation, let location {
            if needsRoute { calculateRoute() } else { updateProgress(location) }
        } else { pauseMeasurements("Resuming. Waiting for fresh GPS.") }
        reconcileLocation()
    }

    func toggleFavoriteCurrentDestination() {
        guard let destinationCoordinate, let title = destinationTitle else { return }
        if let match = favorites.first(where: {
            abs($0.latitude - destinationCoordinate.latitude) < 0.0001 && abs($0.longitude - destinationCoordinate.longitude) < 0.0001
        }) { removeFavorite(id: match.id); return }
        favorites.insert(RecentDestination(title: title, subtitle: destination?.placemark.title ?? "",
            latitude: destinationCoordinate.latitude, longitude: destinationCoordinate.longitude), at: 0)
        favorites = Array(favorites.prefix(100)); saveFavorites()
    }
    func chooseFavorite(_ recent: RecentDestination) { chooseRecent(recent) }
    func removeFavorite(id: UUID) { favorites.removeAll { $0.id == id }; saveFavorites() }
    private func saveFavorites() {
        if let data = try? JSONEncoder().encode(favorites) { UserDefaults.standard.set(data, forKey: "navigation.favorites") }
    }

    private static var libraryFolder: URL {
        let folder = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Navigation", isDirectory: true)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        return folder
    }
    private func loadNavigationLibrary() {
        if let data = UserDefaults.standard.data(forKey: "navigation.favorites"),
           let places = try? JSONDecoder().decode([RecentDestination].self, from: data) {
            favorites = Array(places.filter(\.valid).prefix(100))
        }
        if let data = try? Data(contentsOf: Self.libraryFolder.appendingPathComponent("routes.json")),
           let routes = try? NavigationRouteArchive.decode(data) { savedRoutes = routes }
        if let data = UserDefaults.standard.data(forKey: "navigation.trafficHistory"),
           let observations = try? JSONDecoder().decode([NavigationTrafficObservation].self, from: data) {
            trafficHistory = NavigationTrafficHistory.normalized(observations)
        }
        let graphURL = Self.libraryFolder.appendingPathComponent("roads.json")
        guard FileManager.default.fileExists(atPath: graphURL.path) else { return }
        importOfflineGraph(from: graphURL)
    }
    func saveCurrentRoute() {
        guard let activeRoute else { return }
        persistCurrentRoute(activeRoute)
        status = "Route saved. Instructions and progress work offline; map tiles need their own cache."
    }
    private func persistCurrentRoute(_ route: SavedNavigationRoute) {
        savedRoutes.removeAll { $0.id == route.id }
        savedRoutes.insert(route, at: 0)
        savedRoutes = Array(savedRoutes.prefix(20))
        if let data = try? NavigationRouteArchive.encode(savedRoutes) {
            try? data.write(to: Self.libraryFolder.appendingPathComponent("routes.json"), options: .atomic)
        }
    }
    func removeSavedRoute(id: UUID) {
        savedRoutes.removeAll { $0.id == id }
        if let data = try? NavigationRouteArchive.encode(savedRoutes) {
            try? data.write(to: Self.libraryFolder.appendingPathComponent("routes.json"), options: .atomic)
        }
    }
    func loadSavedRoute(_ saved: SavedNavigationRoute) {
        guard saved.isValid, foreground, liveModeAllowed, let last = saved.coordinates.last else { return }
        stopGuidance(); cancelDirections(); cancelSearch(); cancelSuggestions()
        destinationID = UUID()
        let item = MKMapItem(placemark: MKPlacemark(coordinate: CLLocationCoordinate2D(latitude: last.latitude, longitude: last.longitude)))
        item.name = saved.title; destination = item; destinationTitle = saved.title
        itineraryStops = saved.stops.map { RecentDestination(id: $0.id, title: $0.title, subtitle: "Saved stop",
            latitude: $0.coordinate.latitude, longitude: $0.coordinate.longitude) }
        reachedStopIDs = []
        avoidTolls = saved.preferences.avoidTolls; avoidHighways = saved.preferences.avoidMotorways
        alternatives = []; plannedAlternatives = []; results = []
        installRoute(saved, apple: nil)
        needsRoute = false
        status = "Saved route loaded. Start near its route; offline rerouting requires a road graph."
        reconcileLocation()
    }
    func importOfflineGraph(from url: URL) {
        let importID = UUID(); graphImportID = importID
        Task { @MainActor [weak self] in
            let accessed = url.startAccessingSecurityScopedResource()
            defer { if accessed { url.stopAccessingSecurityScopedResource() } }
            do {
                let result = try await Task.detached(priority: .userInitiated) { () -> (OfflineRoadGraph, OfflineNavigationRouter, Data, String?) in
                    let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
                    guard size > 0, size <= 64_000_000 else { throw OfflineNavigationError.invalidGraph }
                    let data = try Data(contentsOf: url, options: .mappedIfSafe)
                    let graph: OfflineRoadGraph
                    var warning: String?
                    if let prepared = try? OfflineRoadGraph.decode(data) { graph = prepared }
                    else {
                        let imported = try OSMRoadGraphImporter.importData(data)
                        graph = imported.graph; warning = imported.warnings.joined(separator: " ")
                    }
                    return (graph, try OfflineNavigationRouter(graph: graph), try JSONEncoder().encode(graph), warning)
                }.value
                guard let self, self.graphImportID == importID else { return }
                try result.2.write(to: Self.libraryFolder.appendingPathComponent("roads.json"), options: .atomic)
                self.offlineRouter = result.1
                self.offlineGraphName = result.0.name; self.offlineAttribution = result.0.attribution
                self.offlineGraphReady = true
                self.offlineImportWarning = result.3
                self.status = "Offline roads ready: " + result.0.name
            } catch {
                if self?.graphImportID == importID { self?.errorMessage = "Could not import offline roads: " + error.localizedDescription }
            }
        }
    }

    private func updatePresentation(fix: CLLocation, coordinate: NavigationCoordinate? = nil, bearing: Double? = nil) {
        guard !userPaused else { return }
        let point = coordinate ?? NavigationCoordinate(latitude: fix.coordinate.latitude, longitude: fix.coordinate.longitude)
        var displayedPoint = point
        var course = bearing ?? (fix.course >= 0 && fix.speed >= 2 ? fix.course : nil)
        if !isNavigating, let router = offlineRouter, fix.horizontalAccuracy <= 20,
           let snap = try? router.snap(point, bearing: course,
                preferences: .init(avoidTolls: avoidTolls, avoidMotorways: avoidHighways), maximumDistance: 25), snap.confidence >= 0.65 {
            displayedPoint = snap.coordinate; course = snap.bearing
        }
        if presentationEstimator.ingest(coordinate: displayedPoint, bearing: course, speed: fix.speed,
            accuracy: fix.horizontalAccuracy, timestamp: fix.timestamp.timeIntervalSinceReferenceDate,
            now: Date().timeIntervalSinceReferenceDate) {
            displayedLocation = CLLocation(coordinate: .init(latitude: displayedPoint.latitude, longitude: displayedPoint.longitude),
                altitude: fix.altitude, horizontalAccuracy: fix.horizontalAccuracy, verticalAccuracy: fix.verticalAccuracy,
                course: course ?? -1, courseAccuracy: bearing == nil ? fix.courseAccuracy : 10,
                speed: fix.speed, speedAccuracy: fix.speedAccuracy, timestamp: fix.timestamp)
            positionEstimated = false
        }
    }
    private func updatePresentationEstimate() {
        guard foreground, !userPaused, isNavigating else { return }
        guard let estimate = presentationEstimator.estimate(now: Date().timeIntervalSinceReferenceDate) else {
            displayedLocation = nil; positionEstimated = false; return
        }
        guard estimate.isEstimated else { return }
        displayedLocation = CLLocation(coordinate: .init(latitude: estimate.coordinate.latitude, longitude: estimate.coordinate.longitude),
            altitude: 0, horizontalAccuracy: estimate.uncertaintyRadius, verticalAccuracy: -1,
            course: estimate.bearing ?? -1, speed: -1, timestamp: Date())
        positionEstimated = true
    }
    private func updateTrafficMovement(_ fix: CLLocation) {
        guard fix.speed.isFinite, fix.speed >= 0 else { return }
        movementSamples.append((fix.timestamp, fix.speed))
        movementSamples.removeAll { fix.timestamp.timeIntervalSince($0.0) > 120 }
        let stopped = movementSamples.filter { $0.1 < 1 }.count
        let moving = movementSamples.filter { $0.1 >= 3 }.count
        stopAndGo = movementSamples.count >= 20 && stopped >= 5 && moving >= 5
    }
    private func refreshTraffic() {
        guard trafficTask == nil, !preferOfflineRouting, let destination, let fix = location,
              !itineraryStops.contains(where: { !reachedStopIDs.contains($0.id) }) else { return }
        lastTrafficCheck = Date()
        let targetID = destinationID, revision = routeRevision
        let requestID = UUID(); trafficRequestID = requestID
        let request = directionsRequest(from: MKMapItem(placemark: MKPlacemark(coordinate: fix.coordinate)),
            to: destination, departure: Date(), alternatives: true)
        let directions = MKDirections(request: request); trafficDirections = directions
        trafficTask = Task { @MainActor [weak self] in
            defer {
                if self?.trafficRequestID == requestID { self?.trafficTask = nil; self?.trafficDirections = nil }
            }
            guard let response = try? await directions.calculate(), !Task.isCancelled,
                  let self, self.trafficRequestID == requestID, self.destinationID == targetID, self.routeRevision == revision,
                  self.foreground, self.isNavigating, !self.userPaused, !self.isRejoiningRoute,
                  let candidate = response.routes.filter(Self.validAppleRoute).min(by: { $0.expectedTravelTime < $1.expectedTravelTime }) else { return }
            let previousETA = self.remainingTime ?? candidate.expectedTravelTime
            let remaining = self.remainingDistance ?? 0
            let matching = response.routes.filter(Self.validAppleRoute).filter {
                abs($0.distance - remaining) <= max(100, remaining * 0.05) && self.followsCurrentRoute($0)
            }.min { $0.expectedTravelTime < $1.expectedTravelTime }
            var notice: String?
            if let matching {
                if matching.expectedTravelTime > previousETA + max(180, previousETA * 0.2) {
                    notice = "Travel time has increased by about \(Int(((matching.expectedTravelTime - previousETA) / 60).rounded())) min."
                }
                self.remainingTime = matching.expectedTravelTime
                if let saved = self.activeRoute, remaining > 1 {
                    let baseline = saved.expectedTravelTime * min(1, remaining / saved.distance)
                    if baseline > 0 { self.trafficTimeMultiplier = min(4, max(0.4, matching.expectedTravelTime / baseline)) }
                }
            }
            let activeETA = matching?.expectedTravelTime ?? previousETA
            if activeETA - candidate.expectedTravelTime >= max(120, activeETA * 0.1) {
                self.betterRoute = candidate
                notice = "A route may save about \(Int(((activeETA - candidate.expectedTravelTime) / 60).rounded())) min."
            } else { self.betterRoute = nil }
            self.trafficNotice = notice
        }
    }
    private func followsCurrentRoute(_ candidate: MKRoute) -> Bool {
        guard let line = routePath else { return false }
        let points = Self.coordinates(candidate.polyline)
        let current = Self.coordinates(line)
        guard !points.isEmpty, !current.isEmpty else { return false }
        let strideCount = max(1, points.count / 20)
        let currentMapPoints = current.map { MKMapPoint(CLLocationCoordinate2D(latitude: $0.latitude, longitude: $0.longitude)) }
        return stride(from: 0, to: points.count, by: strideCount).allSatisfy { index in
            let point = MKMapPoint(CLLocationCoordinate2D(latitude: points[index].latitude, longitude: points[index].longitude))
            return zip(currentMapPoints, currentMapPoints.dropFirst()).contains { start, end in
                let dx = end.x - start.x, dy = end.y - start.y
                let square = dx * dx + dy * dy
                let fraction = square > 0 ? min(1, max(0, ((point.x - start.x) * dx + (point.y - start.y) * dy) / square)) : 0
                let projection = MKMapPoint(x: start.x + dx * fraction, y: start.y + dy * fraction)
                return point.distance(to: projection) < 45
            }
        }
    }
    func acceptBetterRoute() {
        guard betterRoute != nil, isNavigating, !userPaused, hasFreshLocation,
              !itineraryStops.contains(where: { !reachedStopIDs.contains($0.id) }) else { return }
        // The suggestion may have been fetched minutes ago. Recalculate from
        // the current fix before accepting it so it cannot route backwards.
        betterRoute = nil; trafficNotice = nil
        retryRoute()
    }
    private func cancelTrafficRefresh() {
        trafficRequestID = UUID()
        trafficTask?.cancel(); trafficTask = nil; trafficDirections?.cancel(); trafficDirections = nil
    }
    private func recordCompletedTrip() {
        guard let departure = tripDeparture, let key = tripHistoryKey, !tripReplanned else { return }
        let duration = Date().timeIntervalSince(departure) - tripPausedSeconds
        let record = NavigationTrafficObservation(routeKey: key, departure: departure, duration: duration,
            baselineDuration: tripBaseline, distance: tripDistance)
        if record.isValid {
            trafficHistory = NavigationTrafficHistory.normalized([record] + trafficHistory)
            if let data = try? JSONEncoder().encode(trafficHistory) { UserDefaults.standard.set(data, forKey: "navigation.trafficHistory") }
        }
        tripDeparture = nil
    }

    private func cancelSearch() {
        searchID = UUID()
        searchRequest?.cancel()
        searchRequest = nil
        isSearching = false
        resolvingDestination = nil
    }

    private func cancelDirections() {
        let wasCalculating = isCalculating
        planningTask?.cancel(); planningTask = nil
        directionsID = UUID()
        directionsRequest?.cancel()
        directionsRequest = nil
        isCalculating = false
        // A selected destination may resume planning after a transient pause.
        if destination != nil, activeRoute == nil || wasCalculating { needsRoute = true }
    }
}

extension NavigationService: CLLocationManagerDelegate {
    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        Task { @MainActor [weak self] in self?.reconcileLocation() }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        Task { @MainActor [weak self] in self?.acceptLocations(locations) }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        Task { @MainActor [weak self] in
            guard let self, self.foreground, self.locationRunning else { return }
            self.hasFreshLocation = false
            self.location = nil
            self.pauseMeasurements("GPS unavailable · waiting for location")
        }
    }
}

extension NavigationService: AVSpeechSynthesizerDelegate {
    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor [weak self] in self?.completeSpeech(utterance, completed: true) }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        Task { @MainActor [weak self] in self?.completeSpeech(utterance, completed: false) }
    }
}
