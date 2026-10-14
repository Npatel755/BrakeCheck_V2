import Foundation
import CoreLocation
import BrakeCheckCore

/// BrakeCheck GPS speed service.
///
/// Provides two independent speed streams:
/// 1. Filtered GPS speed for existing BrakeCheck calculations.
/// 2. Experimental GPS/IMU fusion for comparison.
///
/// Create and use on the main thread.
final class SpeedService: NSObject, CLLocationManagerDelegate {

    // MARK: - Services

    private let manager = CLLocationManager()
    private var filter = GPSFilter()
    private let fusion = MotionSpeedFusion()

    // MARK: - State

    private var requested = false
    private var lastStatus = ""

    private var lastAcceptedUptime: TimeInterval?
    private var lastProcessedFixTime: Date?
    private var speedHistory = FrameAlignedSpeedHistory()

    // MARK: - Configuration

    /// Maximum age of an incoming GPS fix.
    private let maxFixAge: TimeInterval = 2.0

    /// Maximum age of a filtered measurement.
    private let maxFilteredAge: TimeInterval = 1.5

    /// Allow the adaptive filter to evaluate noisy GPS fixes.
    private let maxHorizontalAccuracy: CLLocationAccuracy = 50.0
    private let maxSpeedAccuracy: CLLocationSpeedAccuracy = 8.0

    // MARK: - Callbacks

    /// Existing BrakeCheck GPS speed.
    var onSpeed: ((GPSSpeedSample?) -> Void)?

    var onStatus: ((String) -> Void)?
    var latestLocation: CrashLocation?

    /// Experimental fused-speed estimate.
    /// Do not use for validated collision warnings.
    var onFusedSpeed: ((MotionSpeedEstimate?) -> Void)?

    var onFusionStatus: ((String) -> Void)?

    // MARK: - Initialization

    override init() {
        super.init()

        manager.delegate = self

        manager.activityType = .automotiveNavigation
        manager.desiredAccuracy = kCLLocationAccuracyBestForNavigation
        manager.distanceFilter = kCLDistanceFilterNone

        manager.pausesLocationUpdatesAutomatically = false

        // Foreground operation.
        manager.allowsBackgroundLocationUpdates = false
        manager.showsBackgroundLocationIndicator = false

        fusion.onEstimate = { [weak self] estimate in
            self?.onFusedSpeed?(estimate)
        }

        fusion.onStatus = { [weak self] message in
            self?.onFusionStatus?(message)
        }
    }

    // MARK: - Start Tracking

    func start() {
        dispatchPrecondition(condition: .onQueue(.main))

        guard !requested else { return }

        requested = true

        clearGPS()
        fusion.start()

        guard CLLocationManager.locationServicesEnabled() else {
            status("Location Services are disabled.")
            return
        }

        switch manager.authorizationStatus {

        case .notDetermined:
            status("Requesting location permission...")
            manager.requestWhenInUseAuthorization()

        case .authorizedAlways, .authorizedWhenInUse:
            startAuthorized()

        case .denied, .restricted:
            status("Enable location access in iPhone Settings.")

        @unknown default:
            status("Unknown GPS authorization state.")
        }
    }

    // MARK: - Authorized Tracking

    private func startAuthorized() {
        guard requested else { return }

        guard manager.accuracyAuthorization == .fullAccuracy else {
            manager.stopUpdatingLocation()
            clearGPS()
            onFusedSpeed?(nil)

            status("Enable Precise Location for BrakeCheck.")
            return
        }

        manager.startUpdatingLocation()

        status("Acquiring GPS speed...")
    }

    // MARK: - Stop Tracking

    func stop() {
        dispatchPrecondition(condition: .onQueue(.main))

        requested = false

        manager.stopUpdatingLocation()
        fusion.stop()

        clearGPS()
        onFusedSpeed?(nil)

        status("GPS stopped")
    }

    // MARK: - Latest Valid GPS Measurement

    /// Returns a filtered GPS sample only while sufficiently fresh.
    /// Does not extrapolate speed into the future.
    func reading(at uptime: TimeInterval) -> GPSSpeedSample? {

        guard requested else { return nil }
        // Inference can finish after the next GPS callback. Match capture
        // time against retained measurements, not just the latest fix.
        return speedHistory.reading(at: uptime, maximumAge: maxFilteredAge)
    }

    /// Bounded display continuity. Predictions never enter onSpeed, the GPS
    /// log, vehicle direction checks, or the collision estimator as fixes.
    func estimate(at uptime: TimeInterval) -> GPSSpeedEstimate? {
        guard requested else { return nil }
        return speedHistory.estimate(at: uptime)
    }

    // MARK: - Optional Motion Calibration

    /// Motion-based prediction stays disabled until an axis is configured.
    func configureForwardAxis(
        _ axis: MotionSpeedFusion.ForwardAxis?
    ) {
        dispatchPrecondition(condition: .onQueue(.main))

        fusion.configureForwardAxis(axis)
    }

    // MARK: - Authorization Changes

    func locationManagerDidChangeAuthorization(
        _ manager: CLLocationManager
    ) {
        guard requested else { return }

        switch manager.authorizationStatus {

        case .authorizedAlways, .authorizedWhenInUse:
            startAuthorized()

        case .denied, .restricted:
            manager.stopUpdatingLocation()
            clearGPS()
            onFusedSpeed?(nil)

            status("GPS permission denied or restricted.")

        case .notDetermined:
            status("Waiting for location permission...")

        @unknown default:
            status("Unknown GPS permission state.")
        }
    }

    // MARK: - GPS Updates

    func locationManager(
        _ manager: CLLocationManager,
        didUpdateLocations locations: [CLLocation]
    ) {
        guard requested else { return }

        guard manager.accuracyAuthorization == .fullAccuracy else {
            clearGPS()
            onFusedSpeed?(nil)

            status("Precise Location is required.")
            return
        }

        // Select the newest GPS measurement.
        guard let location = locations.max(
            by: { $0.timestamp < $1.timestamp }
        ) else {
            return
        }

        // Reject duplicate or out-of-order GPS fixes.
        if let previous = lastProcessedFixTime,
           location.timestamp <= previous {
            return
        }

        lastProcessedFixTime = location.timestamp

        let now = Date()
        let uptime = ProcessInfo.processInfo.systemUptime

        let age = now.timeIntervalSince(location.timestamp)

        let speed = location.speed
        let speedAccuracy = location.speedAccuracy
        let horizontalAccuracy = location.horizontalAccuracy

        let simulated =
            location.sourceInformation?.isSimulatedBySoftware ?? false
        // A valid location can exist even when iOS cannot estimate speed.
        if age >= -0.5, age <= 15, horizontalAccuracy >= 0, horizontalAccuracy <= 100, !simulated {
            latestLocation = CrashLocation(latitude: location.coordinate.latitude,
                longitude: location.coordinate.longitude, measuredAt: location.timestamp)
        }

        // MARK: - Validate Raw GPS

        guard age.isFinite,
              (-0.5...maxFixAge).contains(age),

              speed.isFinite,
              (0...90).contains(speed),

              speedAccuracy.isFinite,
              (0...maxSpeedAccuracy).contains(speedAccuracy),

              horizontalAccuracy.isFinite,
              (0...maxHorizontalAccuracy).contains(horizontalAccuracy),

              !simulated else {

            publishRecentOrUnavailable(
                at: uptime,
                reason: "GPS signal weak or measurement invalid."
            )

            return
        }

        // MARK: - Construct GPS Fix

        let fix = GPSFix(
            speed: speed,
            speedAccuracy: speedAccuracy,
            horizontalAccuracy: horizontalAccuracy,
            timestamp: location.timestamp.timeIntervalSinceReferenceDate,
            simulated: simulated
        )

        // MARK: - Adaptive GPS Filtering

        let gpsReading = filter.ingest(
            fix,
            wallNow: now.timeIntervalSinceReferenceDate,
            uptimeNow: uptime
        )

        if let gpsReading {

            // The filter's timestamp represents the measurement time.
            lastAcceptedUptime = gpsReading.measurementUptime
            speedHistory.append(gpsReading)

            onSpeed?(gpsReading)
            status(filter.status)

        } else {
            if filter.status == "Low-speed GPS uncertain; waiting for confirmation" {
                // A valid new fix contradicts the old stop. A dropout bridge
                // must not put that zero back into display or collision history.
                speedHistory.reset()
                lastAcceptedUptime = nil
                onSpeed?(nil)
                status(filter.status)
            } else {
                publishRecentOrUnavailable(at: uptime, reason: "GPS filter: \(filter.status)")
            }
        }

        // MARK: - Experimental GPS/IMU Fusion

        // Supply the independent filter with the raw measurement
        // and its uncertainty, not the already-filtered GPS speed.
        fusion.acceptGPS(
            speed: speed,
            accuracy: speedAccuracy,
            age: max(0, age)
        )

        // MARK: - Diagnostics

        #if DEBUG

        let mph = speed * 2.2369362921

        print("========== BRAKECHECK GPS ==========")
        print(String(format: "Raw GPS: %.2f mph", mph))
        print(String(format: "Speed uncertainty: ±%.2f m/s", speedAccuracy))
        print(String(format: "Horizontal accuracy: %.1f m", horizontalAccuracy))
        print(String(format: "Fix age: %.2f seconds", age))
        print("Filtered measurement accepted: \(gpsReading != nil)")

        if let gpsReading {
            print(
                String(
                    format: "Filtered speed: %.2f mph",
                    gpsReading.metersPerSecond * 2.2369362921
                )
            )
        }

        print("Filter status: \(filter.status)")
        print("===================================")

        #endif
    }

    // MARK: - Temporary GPS Dropouts

    /// Preserves a recent sample for display during a brief dropout.
    /// Its original measurement timestamp is never changed.
    private func publishRecentOrUnavailable(
        at uptime: TimeInterval,
        reason: String
    ) {

        if let recent = reading(at: uptime) {

            onSpeed?(recent)

            let age = uptime - recent.measurementUptime

            status(
                String(
                    format: "GPS delayed · last reading %.1f s old",
                    max(0, age)
                )
            )

        } else {

            onSpeed?(nil)
            status(reason)
        }
    }

    // MARK: - GPS Errors

    func locationManager(
        _ manager: CLLocationManager,
        didFailWithError error: Error
    ) {

        let nsError = error as NSError

        if nsError.domain == kCLErrorDomain,
           nsError.code == CLError.locationUnknown.rawValue {

            // Temporary GPS loss. Core Location may recover.
            // Do not destroy the filter's history.

            publishRecentOrUnavailable(
                at: ProcessInfo.processInfo.systemUptime,
                reason: "GPS signal temporarily unavailable."
            )

            return
        }

        clearGPS()
        onFusedSpeed?(nil)

        status("GPS error: \(error.localizedDescription)")
    }

    // MARK: - Reset GPS

    private func clearGPS() {
        latestLocation = nil

        filter.reset()
        speedHistory.reset()

        lastAcceptedUptime = nil
        lastProcessedFixTime = nil

        onSpeed?(nil)
    }

    // MARK: - Status

    private func status(_ message: String) {

        guard message != lastStatus else {
            return
        }

        lastStatus = message
        onStatus?(message)
    }
}
