import BrakeCheckCore

import SwiftUI

import Combine

import UIKit

@MainActor

final class DriveModel: ObservableObject {

    let camera = CameraService()
    let safety = SafetyService()
    @Published var tripMinutes: Double = 0
    @Published var hardBrakes = 0
    @Published var closeCalls = 0
    @Published var maxSpeedMPH: Double = 0
    private var tripStarted = Date()
    private var wasCritical = false
    private var wasHardBrake = false
    private var wasCloseCall = false

    private let speedService = SpeedService()

    private let impactService = ImpactService()

    private var estimator = RiskEstimator()

    private var speedReading: GPSSpeedSample?

    private var targetID: UUID?

    private var lastTrackingUptime: TimeInterval = 0

    private var timer: Timer?

    private var foreground = true

    private var demoStarted: TimeInterval = 0

    private var backgroundTask: UIBackgroundTaskIdentifier = .invalid

    private var permissionPending = false

    @Published var tripActive = false

    @Published var stopping = false

    @Published var demo = false

    @Published var cameraReady = false

    @Published var speed: Double?

    @Published var gpsSimulated = false

    @Published var gpsAccuracy: Double?

    // Experimental speed estimate for UI only; never fed to RiskEstimator.
    @Published var fusedSpeed: Double?
    @Published var fusedSpeedUncertainty: Double?
    @Published var fusedSpeedSource = "GPS fusion unavailable"
    @Published var fusedGPSAge: TimeInterval?
    @Published var fusionStatus = "Fusion stopped"

    @Published var detections: [VehicleDetection] = []

    @Published var detectorStatus = "YOLO11n is loading…"

    @Published var rangeSource = "Camera range is an estimate"

    @Published var egoAcceleration: Double?

    @Published var risk = RiskSnapshot()

    @Published var trackingBox: CGRect?

    @Published var lanePath: LanePath?
    @Published var roadRegion: RoadRegion?
    @Published var trackingCoasting = false

    @Published var trackingStatus = "Automatic lead candidate selection starts with the camera."

    @Published var cameraStatus = "Camera stopped"

    @Published var gpsStatus = "GPS stopped"

    @Published var recorderStatus = "Recorder stopped"

    @Published var motionStatus = "Impact sensing off"

    @Published var incidents: [IncidentManifest] = []

    @Published var message: String?

    @Published var impactEnabled = false {

        didSet { updateImpactService() }

    }

    init() {

        speedService.onSpeed = { [weak self] reading in

            guard let self, self.tripActive, !self.demo else { return }

            self.speedReading = reading
            self.camera.tracker.updateEgoSpeed(reading)

            self.speed = reading?.metersPerSecond

            self.egoAcceleration = reading?.acceleration

            self.gpsAccuracy = reading?.accuracy

            self.gpsSimulated = reading?.simulated ?? false

            if reading == nil { self.invalidateRisk("GPS speed unavailable.") }

        }

        speedService.onFusedSpeed = { [weak self] estimate in
            guard let self, self.tripActive, !self.demo else { return }
            self.fusedSpeed = estimate?.metersPerSecond
            self.fusedSpeedUncertainty = estimate?.uncertaintyMetersPerSecond
            self.fusedSpeedSource = estimate?.source ?? "GPS fusion unavailable"
            self.fusedGPSAge = estimate?.gpsAgeSeconds
        }
        speedService.onFusionStatus = { [weak self] status in
            self?.fusionStatus = status
        }

        speedService.onStatus = { [weak self] status in self?.gpsStatus = status }

        impactService.onStatus = { [weak self] status in self?.motionStatus = status }

        impactService.onCandidate = { [weak self] g in

            guard let self, self.tripActive, !self.demo, self.foreground else { return }

            self.camera.markIncident(reason: String(format: "Experimental impact candidate, %.1f g", g))

            if self.safety.settings.autoCheckin {
                self.safety.beginCheckin(location: self.speedService.latestLocation,
                    speedMPH: self.speed.map { $0 * 2.2369362921 }, dryRun: false)
            } else {
                self.message = "Possible impact detected. Preserving available video."
            }

        }

        camera.onCameraStatus = { [weak self] status, ready in

            DispatchQueue.main.async {

                self?.cameraStatus = status

                self?.cameraReady = ready

            }

        }

        camera.onRecorderStatus = { [weak self] status in

            DispatchQueue.main.async { self?.recorderStatus = status }

        }

        camera.onIncidents = { [weak self] events in

            DispatchQueue.main.async { self?.incidents = events }

        }

        camera.onTrackingReset = { [weak self] reason in

            DispatchQueue.main.async { self?.clearTarget(reason) }

        }

        camera.tracker.onReading = { [weak self] reading in

            DispatchQueue.main.async { self?.consume(reading) }

        }

        camera.tracker.onDetections = { [weak self] detections in

            DispatchQueue.main.async {

                guard self?.tripActive == true, self?.foreground == true else { return }

                self?.detections = detections

            }

        }

        camera.tracker.onStatus = { [weak self] status in

            DispatchQueue.main.async { self?.detectorStatus = status }

        }

        camera.tracker.onLane = { [weak self] lane in
            DispatchQueue.main.async {
                guard let self, self.tripActive, self.foreground, !self.demo else { return }
                self.lanePath = lane
            }
        }
        camera.tracker.onRoad = { [weak self] road in
            DispatchQueue.main.async {
                guard let self, self.tripActive, self.foreground, !self.demo else { return }
                self.roadRegion = road
            }
        }

        camera.tracker.onLost = { [weak self] id, reason in

            DispatchQueue.main.async {

                guard self?.targetID == id else { return }

                // The tracker already reset itself; don't enqueue a second clear after it selects a new ID.

                self?.targetID = nil; self?.trackingBox = nil
                self?.trackingCoasting = false

                self?.trackingStatus = reason; self?.invalidateRisk(reason)

            }

        }

        timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in

            // Timer is installed on the main run loop.

            Task { @MainActor in self?.tick() }

        }

        if let timer { RunLoop.main.add(timer, forMode: .common) }

        camera.refreshIncidents()

        camera.tracker.prepare()

    }

    func startTrip() {

        guard !tripActive, !stopping, !demo else { return }

        tripActive = true
        tripStarted = Date(); tripMinutes = 0; hardBrakes = 0; closeCalls = 0; maxSpeedMPH = 0
        wasCritical = false; wasHardBrake = false; wasCloseCall = false
        Task { await safety.prepareVoice() }

        clearFusionDisplay()
        speedService.start()

        UIApplication.shared.isIdleTimerDisabled = true

        requestCameraIfNeeded()

        updateImpactService()

    }

    private func requestCameraIfNeeded() {

        guard !permissionPending else { return }

        permissionPending = true

        CameraService.requestPermission { [weak self] granted in

            guard let self else { return }

            self.permissionPending = false

            guard self.tripActive, !self.demo, self.foreground else { return }

            if granted { self.camera.start() }

            else {

                self.cameraStatus = "Camera permission denied"

                self.message = "Allow Camera in iPhone Settings to record and track vehicles. GPS can still run."

            }

        }

    }

    func endTrip() {

        guard tripActive, !stopping else { return }

        tripActive = false
        safety.sendTrip(minutes: Date().timeIntervalSince(tripStarted) / 60,
                        hardBrakes: hardBrakes, closeCalls: closeCalls, maxSpeedMPH: maxSpeedMPH)

        stopping = true

        speedService.stop()
        clearFusionDisplay()

        impactService.stop()

        speedReading = nil

        speed = nil

        egoAcceleration = nil

        gpsAccuracy = nil; gpsSimulated = false

        clearTarget("Trip ended")

        UIApplication.shared.isIdleTimerDisabled = false

        beginFinalizationTask()

        camera.stop { [weak self] in

            DispatchQueue.main.async {

                self?.stopping = false

                self?.finishBackgroundTask()

            }

        }

    }

    func sceneChanged(_ phase: ScenePhase) {

        if phase == .background {

            foreground = false
            safety.pauseAudio()

            if demo { setDemo(false) }

            UIApplication.shared.isIdleTimerDisabled = false

            guard tripActive else { return }

            clearTarget("Background: tracking paused. Recalibrate when you return.")

            impactService.stop()

            beginFinalizationTask()

            camera.pause { [weak self] in

                DispatchQueue.main.async { self?.finishBackgroundTask() }

            }

        } else if phase == .active {

            let resumed = !foreground

            foreground = true

            if tripActive {

                UIApplication.shared.isIdleTimerDisabled = true

                if resumed { requestCameraIfNeeded() }

                updateImpactService()

            }

        }

    }

    private func finishBackgroundTask() {

        guard backgroundTask != .invalid else { return }

        UIApplication.shared.endBackgroundTask(backgroundTask)

        backgroundTask = .invalid

    }

    private func beginFinalizationTask() {

        finishBackgroundTask()

        backgroundTask = UIApplication.shared.beginBackgroundTask(withName: "Finalize video segment") { [weak self] in

            self?.finishBackgroundTask()

        }

    }

    func selectVehicle(box: CGRect, measuredDistance: Double) {

        guard tripActive, cameraReady, !demo, (2...80).contains(measuredDistance),

              box.width > 0.03, box.height > 0.03,

              box.minX >= 0, box.maxX <= 1, box.minY >= 0, box.maxY <= 1 else { return }

        let id = UUID()

        targetID = id

        estimator.reset()

        trackingBox = box

        trackingStatus = "Tracking calibrated target"

        lastTrackingUptime = ProcessInfo.processInfo.systemUptime

        camera.tracker.select(box: box, knownDistance: measuredDistance, id: id)

    }

    func clearTarget(_ reason: String = "Automatic lead candidate selection · range uses assumed vehicle width") {

        targetID = nil

        trackingBox = nil
        trackingCoasting = false
        lanePath = nil
        roadRegion = nil

        trackingStatus = reason

        detections = []

        camera.tracker.clear()

        invalidateRisk(reason)

    }

    func markIncident() {

        guard tripActive, !demo else { return }

        camera.markIncident(reason: "Manually marked incident")

        message = "Available footage is being preserved. Keep BrakeCheck visible to capture the next 15 minutes."

    }

    func deleteIncident(_ id: UUID) { camera.deleteIncident(id) }

    func setDemo(_ enabled: Bool) {

        guard !tripActive, !stopping else { return }

        demo = enabled

        estimator.reset()

        if enabled {

            demoStarted = ProcessInfo.processInfo.systemUptime

            trackingStatus = "SIMULATED · braking scenario repeats every 20 seconds"

            speed = 25

            egoAcceleration = 0

        } else {

            speed = nil

            egoAcceleration = nil

            trackingBox = nil

            risk = RiskSnapshot()

            trackingStatus = "Automatic lead candidate selection starts with the camera."

        }

    }

    private func consume(_ reading: TrackingReading) {

        guard tripActive, foreground, !demo else { return }

        if reading.targetID != targetID { targetID = reading.targetID; estimator.reset() }

        let now = ProcessInfo.processInfo.systemUptime

        guard now - reading.uptime < 0.6 else {

            invalidateRisk("Camera processing is behind. Estimates paused.")

            return

        }

        trackingBox = reading.box
        trackingCoasting = !reading.isFreshDetection || !reading.isPathSupported

        lastTrackingUptime = reading.uptime

        rangeSource = reading.rangeSource

        guard reading.isFreshDetection else {
            trackingStatus = "Keeping lead track · detection gap · estimates paused"
            invalidateRisk("Brief detection gap. Speed and collision estimates paused.")
            return
        }
        guard reading.isPathSupported else {
            trackingStatus = "Candidate · driving path/direction uncertain · estimates paused"
            invalidateRisk("Driving path or vehicle direction uncertain. Estimates paused.")
            return
        }

        trackingStatus = String(format: "Lead candidate · confidence %.0f%% · %@", reading.confidence * 100, reading.rangeSource)

        let alignedGPS = speedService.reading(at: reading.uptime)

        risk = estimator.update(time: reading.uptime, distance: reading.distance,

                                egoSpeed: alignedGPS?.metersPerSecond,

                                gpsAge: alignedGPS.map { reading.uptime - $0.measurementUptime } ?? .infinity,

                                confidence: reading.confidence)

        if risk.distance == nil { risk.distance = reading.distance }
        updateTripEvents()

    }

    private func tick() {

        let now = ProcessInfo.processInfo.systemUptime

        if demo {

            let t = (now - demoStarted).truncatingRemainder(dividingBy: 20)

            // Lead brakes at 7s; ego brakes at 9s. Both decelerate at 3 m/s² for 4s.

            let leadBrake = max(0, min(4, t - 7))

            let egoBrake = max(0, min(4, t - 9))

            let leadLostTravel = 1.5 * leadBrake * leadBrake + 12 * max(0, t - 11)

            let egoLostTravel = 1.5 * egoBrake * egoBrake + 12 * max(0, t - 13)

            let ego = 25 - 3 * egoBrake

            let lead = 25 - 3 * leadBrake

            speed = ego

            egoAcceleration = t >= 9 && t < 13 ? -3 : 0

            risk = RiskRules.evaluate(distance: 65 - leadLostTravel + egoLostTravel,

                                      egoSpeed: ego, rangeRate: lead - ego,

                                      leadAcceleration: t >= 7 && t < 11 ? -3 : 0)

            return

        }

        guard tripActive else { return }
        tripMinutes = Date().timeIntervalSince(tripStarted) / 60
        if let speed { maxSpeedMPH = max(maxSpeedMPH, speed * 2.2369362921) }

        if let reading = speedReading, now - reading.measurementUptime > 2 {

            speedReading = nil

            speed = nil

            egoAcceleration = nil

            gpsAccuracy = nil

            gpsSimulated = false

            gpsStatus = "GPS speed is stale"

            invalidateRisk("Waiting for a fresh GPS speed.")

        }

        // A fusion estimate is only a display diagnostic. Its own source
        // reports GPS age; clear it when the source stops issuing estimates.
        if let age = fusedGPSAge, age > 1.5 {
            clearFusionDisplay()
        }

        if targetID != nil, now - lastTrackingUptime > 0.8 {

            clearTarget("Tracking timed out. Automatic selection will resume.")

        }

    }

    private func clearFusionDisplay() {
        fusedSpeed = nil
        fusedSpeedUncertainty = nil
        fusedGPSAge = nil
        fusedSpeedSource = "GPS fusion unavailable"
    }

    private func invalidateRisk(_ reason: String) {

        estimator.reset()

        risk = RiskSnapshot(reason: reason)

    }

    private func updateImpactService() {

        if impactEnabled, tripActive, foreground, !demo { impactService.start() }

        else { impactService.stop() }

    }

    private func updateTripEvents() {
        let critical = risk.level == .critical
        if critical && !wasCritical { safety.brakeWarning() }
        wasCritical = critical
        let braking = (risk.leadAcceleration ?? 0) < -3
        if braking && !wasHardBrake { hardBrakes += 1 }
        wasHardBrake = braking
        let close = risk.timeToCollision.map { $0 < 2 } ?? false
        if close && !wasCloseCall { closeCalls += 1 }
        wasCloseCall = close
    }

    func simulateCheckin() {
        safety.beginCheckin(location: nil, speedMPH: nil, dryRun: true)
    }

}
