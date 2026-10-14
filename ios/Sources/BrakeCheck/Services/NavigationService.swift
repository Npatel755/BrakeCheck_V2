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
    var mapLocation: CLLocation? { hasFreshLocation ? location : lastKnownLocation }
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
    private var routeRevision = UUID()
    private var routeProgress: NavigationProgressTracker?
    private var steps: [MKRoute.Step] = []
    private var lastFixTimestamp: Date?
    private var locationEligibleAfter = Date.distantFuture
    private var lastSpokenPrompt: String?
    private var spokenStages: [Int: Int] = [:]
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
        routeProgress = nil
        steps = []
        needsRoute = false
        hasArrived = false
        maneuverDistance = nil
        remainingDistance = nil
        remainingTime = nil
        instruction = "Choose a destination"
        errorMessage = nil
        status = "Search for an address or place."
    }

    func startGuidance() {
        guard foreground, liveModeAllowed, route != nil, !isCalculating, hasFreshLocation else {
            errorMessage = "A route and fresh GPS location are needed to start navigation."
            return
        }
        isNavigating = true
        hasArrived = false
        isPaused = false
        lastSpokenPrompt = nil
        spokenStages = [:]
        resetProgress()
        reconcileLocation()
        if let location { updateProgress(location) }
    }

    /// Ending navigation leaves the route preview and the camera trip intact.
    func stopGuidance() {
        stopSpeaking()
        isNavigating = false
        isRejoiningRoute = false
        upcomingInstruction = nil
        recovery.reset()
        lastRerouteAt = .distantPast
        isPaused = false
        hasArrived = false
        cancelDirections()
        needsRoute = false
        lastSpokenPrompt = nil
        spokenStages = [:]
        routeNotice = nil
        resetProgress()
        maneuverDistance = nil
        remainingDistance = route?.distance
        remainingTime = route?.expectedTravelTime
        instruction = route == nil ? "Choose a destination" : "Route preview"
        status = route == nil ? "Search for an address or place." : "Ready when you are."
        reconcileLocation()
    }

    func stopSpeaking(deactivateSession: Bool = true) {
        // Invalidate ownership before stopSpeaking emits cancellation callbacks.
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
        guard foreground, liveModeAllowed, let destination, !isCalculating,
              recovery.canAttempt(at: Date().timeIntervalSinceReferenceDate) else { return }
        if let cached = manager.location { cacheLocation(cached) }
        guard let location = isNavigating ? (hasFreshLocation ? location : nil) : mapLocation else {
            status = "Finding a starting location. No previous GPS fix is available yet."
            return
        }
        routeUsesLastKnownLocation = !hasFreshLocation
        cancelDirections()
        let request = MKDirections.Request()
        request.source = MKMapItem(placemark: MKPlacemark(coordinate: location.coordinate))
        request.destination = destination
        request.transportType = .automobile
        request.requestsAlternateRoutes = false
        let directions = MKDirections(request: request)
        let token = directionsID
        let targetID = destinationID
        directionsRequest = directions
        isCalculating = true
        needsRoute = false
        status = isNavigating ? "Rerouting…" : "Calculating a driving route…"
        if isNavigating { isRejoiningRoute = true; stopSpeaking() }
        directions.calculate { [weak self] response, error in
            Task { @MainActor [weak self] in
                guard let self, self.directionsID == token, self.destinationID == targetID,
                      self.foreground, self.liveModeAllowed else { return }
                self.isCalculating = false
                self.directionsRequest = nil
                guard let route = response?.routes.first, route.distance.isFinite, route.distance > 0,
                      route.expectedTravelTime.isFinite, route.expectedTravelTime >= 0,
                      route.steps.allSatisfy({ $0.distance.isFinite && $0.distance >= 0 }),
                      route.polyline.pointCount >= 2 else {
                    self.errorMessage = "No driving route is available. Check your connection and try another destination or retry."
                    self.status = "Route unavailable"
                    if error != nil {
                        self.recovery.failed(at: Date().timeIntervalSinceReferenceDate)
                        self.needsRoute = true
                    }
                    if self.isNavigating { self.pauseMeasurements("Route unavailable · retry when connected") }
                    return
                }
                self.route = route
                self.recovery.reset()
                self.isRejoiningRoute = false
                self.routeRevision = UUID()
                self.steps = route.steps
                self.hasArrived = false
                self.lastSpokenPrompt = nil
                self.spokenStages = [:]
                self.resetProgress()
                self.remainingDistance = route.distance
                self.remainingTime = route.expectedTravelTime
                self.instruction = "Route preview"
                self.status = self.isNavigating ? "Navigation active" : "Route ready. Start navigation when parked."
                self.errorMessage = nil
                if self.isNavigating, self.hasFreshLocation, let fix = self.location { self.updateProgress(fix) }
            }
        }
    }

    private func resetProgress() {
        guard let route else { routeProgress = nil; return }
        let coordinates = UnsafeBufferPointer(start: route.polyline.points(), count: route.polyline.pointCount)
            .map { point -> NavigationCoordinate in
                let coordinate = point.coordinate
                return NavigationCoordinate(latitude: coordinate.latitude, longitude: coordinate.longitude)
            }
        routeProgress = NavigationProgressTracker(coordinates: coordinates,
            routeDistance: route.distance, stepDistances: steps.map(\.distance))
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
        location = fix
        hasFreshLocation = true
        if needsRoute { calculateRoute() }
        if isNavigating {
            if hasArrived { status = "Destination reached · end navigation when ready" }
            else { updateProgress(fix) }
        }
        else if !isSearching && !isCalculating {
            status = route == nil ? "Search for an address or place." : "Route ready. Start navigation when parked."
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
        guard isNavigating, !hasArrived, !isCalculating, var progress = routeProgress else { return }
        guard let snapshot = progress.update(coordinate: NavigationCoordinate(
                latitude: fix.coordinate.latitude, longitude: fix.coordinate.longitude),
                horizontalAccuracy: fix.horizontalAccuracy,
                timestamp: fix.timestamp.timeIntervalSinceReferenceDate,
                now: Date().timeIntervalSinceReferenceDate) else {
            pauseMeasurements("GPS paused · waiting for a reliable route position")
            return
        }
        routeProgress = progress
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
        if recovery.retryAt != nil {
            needsRoute = false
            recovery.reset()
            errorMessage = nil
        }
        if let route {
            remainingTime = route.expectedTravelTime * min(1, max(0, snapshot.remainingMeters / route.distance))
        }
        if snapshot.arrived {
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
        let effectiveIndex = steps.indices.dropFirst(index).first { !steps[$0].instructions.isEmpty } ?? index
        let step = steps.indices.contains(effectiveIndex) ? steps[effectiveIndex] : nil
        instruction = step?.instructions.isEmpty == false ? step!.instructions : "Continue on your route"
        upcomingInstruction = steps.indices.dropFirst(effectiveIndex + 1)
            .first(where: { !steps[$0].instructions.isEmpty }).map { steps[$0].instructions }
        routeNotice = step?.notice
        maneuverDistance = snapshot.distanceToStep
        status = "Navigation active"
        let threshold = max(40, min(100, max(0, fix.speed) * 6))
        let stage = snapshot.distanceToStep <= 15 ? 2 : snapshot.distanceToStep <= threshold ? 1 : 0
        guard stage > (spokenStages[effectiveIndex] ?? -1) else { return }
        let prefix = stage == 2 || index == 0 ? "" : "In \(spokenDistance(snapshot.distanceToStep)), "
        if announce(prefix + instruction, key: "\(routeRevision):\(effectiveIndex):\(stage)") {
            spokenStages[effectiveIndex] = stage
        }
    }

    private func checkFreshness() {
        let fresh = location.map { (0...2).contains(Date().timeIntervalSince($0.timestamp)) } ?? false
        hasFreshLocation = fresh
        if !fresh, isNavigating, !hasArrived { pauseMeasurements("GPS paused · waiting for a fresh location") }
        if fresh, foreground, liveModeAllowed, needsRoute, !isCalculating,
           recovery.canAttempt(at: Date().timeIntervalSinceReferenceDate) { calculateRoute() }
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

    private func completeSpeech(_ utterance: AVSpeechUtterance) {
        guard currentUtterance === utterance else { return }
        currentUtterance = nil
        let owned = ownsAudioSession
        ownsAudioSession = false
        if owned, canSpeak?() == true {
            try? AVAudioSession.sharedInstance().setActive(false, options: [.notifyOthersOnDeactivation])
        }
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
        directionsID = UUID()
        directionsRequest?.cancel()
        directionsRequest = nil
        isCalculating = false
        // A selected destination may resume planning after a transient pause.
        if destination != nil, route == nil || wasCalculating { needsRoute = true }
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
        Task { @MainActor [weak self] in self?.completeSpeech(utterance) }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        Task { @MainActor [weak self] in self?.completeSpeech(utterance) }
    }
}
