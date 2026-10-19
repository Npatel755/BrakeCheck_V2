import BrakeCheckCore

import SwiftUI

import Combine

import UIKit
import AVFoundation

@MainActor

final class DriveModel: ObservableObject {

    let camera = CameraService()
    let safety = SafetyService()
    let replay = ReplayService()
    private let speedLog = SpeedLogWriter()
    // Replay mode: a recorded drive runs through the live tracker and risk estimator.
    @Published var replaying = false
    @Published var replayFrame: UIImage?
    @Published var replayStatus = ""
    @Published var replaySpeedSource = ""
    private var replayTrack: SpeedTrack?
    private var replayAssumedSpeed: Double?
    private var replayOffset: TimeInterval = 0
    private var replayGeneration: Int?
    private var replayAnchor: (uptime: TimeInterval, recorded: Date?, elapsed: TimeInterval)?
    @Published var tripMinutes: Double = 0
    @Published var hardBrakes = 0
    @Published var closeCalls = 0
    @Published var maxSpeedMPH: Double = 0
    private var tripStarted = Date()
    private var brakeWarningLatch = BrakeWarningLatch()
    @Published private(set) var brakeWarningActive = false
    private var wasHardBrake = false
    private var wasCloseCall = false

    private let speedService = SpeedService()

    private let impactService = ImpactService()

    private var estimator = LeadRiskContinuity()
    private var cameraRangeEstimator = CameraRangeEstimator()
    let measurementRevision = "N11"
    private var detectorMeasurementGeneration: Int?

    private var speedReading: GPSSpeedSample?

    private var targetID: UUID?
    private var currentCameraRisk: RiskSnapshot?

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
    @Published var captureGeometry = CaptureFrameGeometry.standby
    @Published var changingCameraGeometry = false
    private var desiredCaptureOrientation: AVCaptureVideoOrientation = .portrait
    private var latestGeometryChangeUptime: TimeInterval = 0
    private var latestTrackingResetUptime: TimeInterval = 0
    private var lastDetectionsUptime: TimeInterval = 0

    @Published var speed: Double?

    @Published var gpsSimulated = false

    @Published var gpsAccuracy: Double?

    // Experimental speed estimate for UI only; never fed to RiskEstimator.
    @Published var fusedSpeed: Double?
    @Published var fusedSpeedUncertainty: Double?
    @Published var fusedSpeedSource = "GPS fusion unavailable"
    @Published var fusedGPSAge: TimeInterval?
    @Published var fusionStatus = "Fusion stopped"
    private var fusedReceivedUptime: TimeInterval = 0

    @Published var detections: [VehicleDetection] = []

    @Published var detectorStatus = "Vehicle models are loading…"
    @Published var detectorLightingStatus = "Automatic lighting · detectors loading"
    @Published var detectorLightingMode = DetectorLightingMode(rawValue:
        UserDefaults.standard.string(forKey: "BrakeCheck.detectorLightingMode") ?? "automatic") ?? .automatic {
        didSet {
            UserDefaults.standard.set(detectorLightingMode.rawValue, forKey: "BrakeCheck.detectorLightingMode")
            camera.tracker.setLightingMode(detectorLightingMode)
        }
    }
    @Published var leadSelectionStatus = "Looking for a car ahead"
    /// Fresh camera range can be displayed for a rear-confirmed candidate
    /// while driving-path evidence is insufficient for speed or risk.
    @Published var trackedDistance: Double?
    @Published var cameraRange = CameraRangeSnapshot()
    @Published var speedBlocker = "Needs target"
    @Published var collisionBlocker = "Needs target"
    @Published var frameAlignmentStatus = "Waiting for camera and GPS"
    @Published var collisionHazardID: UUID?
    @Published var leadTargetID: UUID?
    @Published var leadRearEvidenceStatus = "No confirmed lead identity"

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

            if let reading {
                // Logged at measurement time so replay can line speed up with the video.
                let measured = Date().addingTimeInterval(reading.measurementUptime - ProcessInfo.processInfo.systemUptime)
                self.speedLog.append(speed: reading.metersPerSecond, accuracy: reading.accuracy, at: measured)
            }

            if reading == nil { self.invalidateRisk("GPS speed unavailable.") }

        }

        speedService.onFusedSpeed = { [weak self] estimate in
            guard let self, self.tripActive, !self.demo else { return }
            self.fusedSpeed = estimate?.metersPerSecond
            self.fusedSpeedUncertainty = estimate?.uncertaintyMetersPerSecond
            self.fusedSpeedSource = estimate?.source ?? "GPS fusion unavailable"
            self.fusedGPSAge = estimate?.gpsAgeSeconds
            self.fusedReceivedUptime = ProcessInfo.processInfo.systemUptime
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

        camera.onGeometry = { [weak self] geometry in
            DispatchQueue.main.async {
                guard let self, !self.replaying, geometry.videoOrientation == self.desiredCaptureOrientation else { return }
                if self.captureGeometry != geometry {
                    self.latestGeometryChangeUptime = ProcessInfo.processInfo.systemUptime
                    self.clearTrackingDisplay("Camera rotated · reacquiring the lead")
                }
                self.captureGeometry = geometry
                self.changingCameraGeometry = false
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

        camera.tracker.onDetections = { [weak self] detections, uptime in

            DispatchQueue.main.async {

                guard let self, self.foreground, self.tripActive || self.replaying, !self.demo else { return }

                guard !self.changingCameraGeometry,
                      uptime >= max(self.latestGeometryChangeUptime, self.latestTrackingResetUptime) else { return }
                guard TrackingFreshness.measurementIsFresh(capturedAt: uptime,
                    now: ProcessInfo.processInfo.systemUptime) else { self.detections = []; return }
                self.lastDetectionsUptime = uptime
                self.detections = detections

            }

        }

        camera.tracker.onStatus = { [weak self] status in

            DispatchQueue.main.async { self?.detectorStatus = status }

        }
        camera.tracker.onLightingStatus = { [weak self] status in
            DispatchQueue.main.async { self?.detectorLightingStatus = status }
        }
        camera.tracker.onLeadStatus = { [weak self] status in
            DispatchQueue.main.async {
                guard let self, self.foreground, self.tripActive || self.replaying, !self.demo,
                      !self.changingCameraGeometry else { return }
                self.leadSelectionStatus = status
            }
        }

        camera.tracker.onLane = { [weak self] lane in
            DispatchQueue.main.async {
                guard let self, self.foreground, self.tripActive || self.replaying, !self.demo else { return }
                guard !self.changingCameraGeometry,
                      lane.map({ $0.capturedAt >= self.latestGeometryChangeUptime }) ?? true else { return }
                self.lanePath = lane
            }
        }
        camera.tracker.onRoad = { [weak self] road in
            DispatchQueue.main.async {
                guard let self, self.foreground, self.tripActive || self.replaying, !self.demo else { return }
                guard !self.changingCameraGeometry,
                      road.map({ $0.capturedAt >= self.latestGeometryChangeUptime }) ?? true else { return }
                self.roadRegion = road
            }
        }

        camera.tracker.onLost = { [weak self] id, reason in

            DispatchQueue.main.async {

                guard self?.targetID == id else { return }

                // The tracker already reset itself; don't enqueue a second clear after it selects a new ID.

                self?.targetID = nil; self?.trackingBox = nil
                self?.trackingCoasting = false
                self?.trackedDistance = nil
                self?.cameraRangeEstimator.reset()
                self?.cameraRange = CameraRangeSnapshot(reason: reason)
                self?.collisionHazardID = nil
                self?.leadTargetID = nil
                self?.leadRearEvidenceStatus = "Lead released · \(reason)"
                self?.currentCameraRisk = nil
                self?.speedBlocker = "Needs target"
                self?.collisionBlocker = "Needs target"

                self?.trackingStatus = reason; self?.invalidateRisk(reason)

            }

        }

        timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in

            // Timer is installed on the main run loop.

            Task { @MainActor in self?.tick() }

        }

        if let timer { RunLoop.main.add(timer, forMode: .common) }

        replay.onFrame = { [weak self] image, generation in
            DispatchQueue.main.async {
                guard let self, self.replaying, self.replayGeneration == generation else { return }
                self.replayFrame = image
            }
        }
        replay.onTime = { [weak self] uptime, recorded, elapsed, generation in
            DispatchQueue.main.async {
                guard let self, self.replaying, self.replayGeneration == generation else { return }
                self.replayAnchor = (uptime: uptime, recorded: recorded, elapsed: elapsed)
            }
        }
        replay.onGeometry = { [weak self] uptime, generation in
            DispatchQueue.main.async {
                guard let self, self.replaying, self.replayGeneration == generation else { return }
                self.latestGeometryChangeUptime = uptime
                self.clearTrackingDisplay("Replay geometry changed · reacquiring the lead")
            }
        }
        replay.onFinished = { [weak self] status, generation in
            DispatchQueue.main.async {
                guard let self, self.replaying, self.replayGeneration == generation else { return }
                self.stopReplay(status)
            }
        }

        camera.refreshIncidents()

        camera.tracker.setLightingMode(detectorLightingMode)
        camera.tracker.prepare()

    }

    func interfaceOrientationChanged(_ orientation: UIInterfaceOrientation) {
        guard let video = CaptureFrameGeometry.videoOrientation(for: orientation),
              video != desiredCaptureOrientation else { return }
        desiredCaptureOrientation = video
        // Replay follows the recorded frame, not the orientation of the HUD.
        guard !replaying else { return }
        changingCameraGeometry = true
        latestGeometryChangeUptime = ProcessInfo.processInfo.systemUptime
        clearTrackingDisplay("Camera rotating · estimates paused")
        camera.setVideoOrientation(video)
    }

    func startTrip() {

        guard !tripActive, !stopping, !demo, !replaying else { return }

        camera.setVideoOrientation(desiredCaptureOrientation)
        clearTarget("Starting trip · acquiring the lead")
        tripActive = true
        tripStarted = Date(); tripMinutes = 0; hardBrakes = 0; closeCalls = 0; maxSpeedMPH = 0
        speedLog.start(at: tripStarted)
        wasHardBrake = false; wasCloseCall = false
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

        speedLog.stop()
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
            if replaying { stopReplay("Replay stopped in the background") }

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

        guard tripActive, cameraReady, !demo, !changingCameraGeometry, (2...80).contains(measuredDistance),

              box.width > 0.03, box.height > 0.03,

              box.minX >= 0, box.maxX <= 1, box.minY >= 0, box.maxY <= 1 else { return }

        let id = UUID()
        latestTrackingResetUptime = ProcessInfo.processInfo.systemUptime

        targetID = id

        estimator.reset()

        trackingBox = box

        trackingStatus = "Tracking calibrated target"

        lastTrackingUptime = ProcessInfo.processInfo.systemUptime

        camera.tracker.select(box: box, knownDistance: measuredDistance, id: id)

    }

    func clearTarget(_ reason: String = "Automatic lead candidate selection · range uses assumed vehicle width") {
        clearTrackingDisplay(reason)
        camera.tracker.clear()
    }

    private func clearTrackingDisplay(_ reason: String) {
        latestTrackingResetUptime = ProcessInfo.processInfo.systemUptime

        targetID = nil
        detectorMeasurementGeneration = nil
        leadTargetID = nil
        leadRearEvidenceStatus = "No confirmed lead identity"

        trackingBox = nil
        trackingCoasting = false
        trackedDistance = nil
        cameraRangeEstimator.reset()
        cameraRange = CameraRangeSnapshot(reason: reason)
        collisionHazardID = nil
        currentCameraRisk = nil
        speedBlocker = "Needs target"
        collisionBlocker = "Needs target"
        frameAlignmentStatus = "Waiting for camera and GPS"
        lanePath = nil
        roadRegion = nil

        trackingStatus = reason

        leadSelectionStatus = "Looking for a car ahead"

        detections = []

        invalidateRisk(reason)

    }

    func markIncident() {

        guard tripActive, !demo else { return }

        camera.markIncident(reason: "Manually marked incident")

        message = "Available footage is being preserved. Keep BrakeCheck visible to capture the next 15 minutes."

    }

    func deleteIncident(_ id: UUID) { camera.deleteIncident(id) }

    func setDemo(_ enabled: Bool) {

        guard !tripActive, !stopping, !replaying else { return }

        demo = enabled
        leadTargetID = nil
        leadRearEvidenceStatus = "No confirmed lead identity"
        trackedDistance = nil
        cameraRangeEstimator.reset()
        cameraRange = CameraRangeSnapshot()
        collisionHazardID = nil
        currentCameraRisk = nil
        wasHardBrake = false; wasCloseCall = false

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

        guard foreground, tripActive || replaying, !demo,
              !changingCameraGeometry,
              reading.uptime >= max(latestGeometryChangeUptime, latestTrackingResetUptime) else { return }

        let now = ProcessInfo.processInfo.systemUptime
        guard TrackingFreshness.identityIsFresh(capturedAt: reading.uptime, now: now),
              reading.uptime > lastTrackingUptime || targetID == nil else { return }
        let fresh = TrackingFreshness.measurementIsFresh(capturedAt: reading.uptime, now: now)
        // Delayed work may retain an already selected car, but cannot select a
        // new target or display its measurements as current.
        guard fresh || reading.targetID == targetID else { return }

        if reading.targetID != targetID {
            targetID = reading.targetID; estimator.reset(); cameraRangeEstimator.reset()
        }
        if detectorMeasurementGeneration != reading.detectorGeneration {
            detectorMeasurementGeneration = reading.detectorGeneration
            estimator.reset(); cameraRangeEstimator.reset()
        }

        lastTrackingUptime = reading.uptime
        trackingBox = reading.box

        guard fresh else {

            trackedDistance = nil
            cameraRangeEstimator.reset()
            cameraRange = CameraRangeSnapshot(reason: "Camera delayed · numbers paused")
            collisionHazardID = nil
            leadRearEvidenceStatus = "Lead identity held · waiting for a fresh camera frame"
            speedBlocker = "Camera delayed"
            collisionBlocker = "Camera delayed"
            leadSelectionStatus = "Lead \(reading.targetID.uuidString.prefix(4)) held · camera delayed"
            trackingCoasting = true
            frameAlignmentStatus = String(format: "N11 · camera frame age %.0f ms · outside 0–600 ms freshness limit", (now - reading.uptime) * 1000)

            invalidateRisk("Camera processing is behind. Estimates paused.")

            return

        }

        trackingCoasting = reading.isDetectorSettling || !reading.isFreshDetection
            || (!reading.isPathSupported && !reading.hasConfirmedRearIdentity)

        rangeSource = reading.rangeSource
        frameAlignmentStatus = String(format: "N11 · camera frame age %.0f ms", (now - reading.uptime) * 1000)
        leadTargetID = reading.hasConfirmedRearIdentity ? reading.targetID : nil
        leadRearEvidenceStatus = reading.isRearConfirmationRetained
            ? "Rear confirmation retained on the same vehicle ID · waiting for a fresh rear check"
            : reading.hasConfirmedRearIdentity ? "Rear confirmation refreshed" : "No confirmed rear identity"
        currentCameraRisk = nil
        collisionHazardID = reading.isCollisionHazard && reading.isFreshDetection && reading.isPathSupported
            ? reading.targetID : nil
        guard !reading.isDetectorSettling else {
            trackedDistance = nil
            cameraRangeEstimator.reset()
            cameraRange = CameraRangeSnapshot(reason: "Detector changed · collecting fresh measurements")
            collisionHazardID = nil
            speedBlocker = "Sampling"; collisionBlocker = "Sampling"
            leadSelectionStatus = "Lead \(reading.targetID.uuidString.prefix(4)) held · detector changed"
            trackingStatus = "Keeping vehicle identity · resampling after lighting change"
            invalidateRisk("Detector changed. Collecting fresh measurements on the same vehicle.")
            return
        }
        guard let distance = reading.distance else {
            // Keep the lead's identity and fresh visible box when clipped.
            // A partial silhouette must not supply numerical range or speed.
            trackedDistance = nil
            cameraRangeEstimator.reset()
            cameraRange = CameraRangeSnapshot(reason: "Vehicle clipped · gap numbers paused")
            collisionHazardID = nil
            trackingCoasting = true
            speedBlocker = "Vehicle clipped"; collisionBlocker = "Vehicle clipped"
            leadSelectionStatus = "Lead \(reading.targetID.uuidString.prefix(4)) held · vehicle clipped"
            trackingStatus = reading.rangeSource
            invalidateRisk("Vehicle clipped. Gap and collision estimates paused; identity retained.")
            return
        }
        // Range is a camera measurement, independent of GPS and lane support.
        // A visual-only coast must not leave an old numeric gap on screen.
        cameraRange = cameraRangeEstimator.update(targetID: reading.targetID,
            time: reading.uptime, distance: distance,
            confidence: reading.confidence, fresh: reading.isFreshDetection)
        trackedDistance = cameraRange.distance

        guard reading.isFreshDetection else {
            speedBlocker = "Detection gap"
            collisionBlocker = "Detection gap"
            trackingStatus = "Keeping lead track · detection gap · estimates paused"
            leadSelectionStatus = "Lead \(reading.targetID.uuidString.prefix(4)) held · detection gap"
            invalidateRisk("Brief detection gap. Speed and collision estimates paused.", preserveSamples: true)
            return
        }
        guard cameraRange.distance != nil else {
            speedBlocker = "Range unsettled"; collisionBlocker = "Range unsettled"
            trackingCoasting = true
            trackingStatus = cameraRange.reason
            leadSelectionStatus = "Lead \(reading.targetID.uuidString.prefix(4)) held · checking range"
            invalidateRisk("Checking an abrupt range change against the next capture.", preserveSamples: true)
            return
        }
        if !reading.isPathSupported, reading.hasConfirmedRearIdentity {
            // Lane uncertainty does not invalidate relative range/motion on
            // the same confirmed lead. Absolute lead speed stays unavailable.
            estimator.reset()
            risk = CollisionApproachRules.forConfirmedLead(lead: RiskSnapshot(), camera: cameraRange,
                confidence: reading.confidence, fresh: reading.isFreshDetection,
                pathSupported: false, rearConfirmed: true)
            currentCameraRisk = risk.score == nil ? nil : risk
            if risk.distance == nil { risk.distance = distance }
            collisionHazardID = risk.timeToCollision != nil ? reading.targetID : nil
            speedBlocker = "Direction uncertain"
            collisionBlocker = risk.score == nil ? "Sampling" : ""
            leadSelectionStatus = risk.timeToCollision != nil ? "Possible collision · gap closing"
                : "Lead \(reading.targetID.uuidString.prefix(4)) · \(cameraRange.reason.lowercased())"
            trackingStatus = "Confirmed lead · \(cameraRange.reason) · camera motion"
            updateTripEvents()
            return
        }
        guard reading.isPathSupported else {
            speedBlocker = "Needs path"
            collisionBlocker = "Needs path"
            leadSelectionStatus = (cameraRange.closingSpeed ?? 0) > 0.5
                ? "Lead \(reading.targetID.uuidString.prefix(4)) · gap closing · path uncertain"
                : "Lead \(reading.targetID.uuidString.prefix(4)) · gap available · path uncertain"
            trackingStatus = "Rear candidate · \(cameraRange.reason) · driving path unconfirmed"
            invalidateRisk("Lead speed and collision risk need a confirmed driving path.", preserveSamples: true)
            return
        }
        trackingStatus = String(format: "Lead candidate · confidence %.0f%% · %@", reading.confidence * 100, reading.rangeSource)

        let egoSpeed: Double?
        let gpsAge: TimeInterval
        if replaying {
            egoSpeed = replayEgoSpeed(at: reading.uptime)
            gpsAge = egoSpeed == nil ? .infinity : 0
        } else {
            let alignedGPS = speedService.reading(at: reading.uptime)
            egoSpeed = alignedGPS?.metersPerSecond
            gpsAge = alignedGPS.map { reading.uptime - $0.measurementUptime } ?? .infinity
            frameAlignmentStatus += alignedGPS.map {
                String(format: " · matched GPS age %.0f ms · uncertainty ±%.1f m/s", gpsAge * 1000, $0.accuracy)
            } ?? " · no fresh GPS at capture time"
        }

        if reading.isCollisionHazard {
            // A stopped or front-facing car can be a collision hazard even
            // when it cannot be a same-direction lead. Camera closing time
            // does not require absolute GPS speed or target deceleration.
            let approach = CollisionApproachRules.evaluateCamera(cameraRange,
                confidence: reading.confidence, fallbackClosingSpeed: reading.approachClosingSpeed, egoSpeed: egoSpeed,
                canEstimateLeadSpeed: reading.canEstimateLeadSpeed,
                isCameraProjectedPath: reading.isCameraProjectedPath)
            currentCameraRisk = approach.score == nil ? nil : approach
            if reading.canEstimateLeadSpeed {
                // Keep same-direction acceleration/headway calculations when
                // their inputs are available. Approach risk fills a missing
                // absolute-speed estimate and can raise the TTC indication.
                let lead = estimator.update(time: reading.uptime, distance: distance,
                    egoSpeed: egoSpeed, gpsAge: gpsAge, confidence: reading.confidence)
                risk = CollisionApproachRules.combine(lead: lead, approach: approach)
            } else { estimator.reset(); risk = approach }
            speedBlocker = risk.leadSpeed != nil ? "" : !reading.canEstimateLeadSpeed ? "Hazard target"
                : egoSpeed == nil ? "Needs GPS" : "Range uncertain"
            collisionBlocker = risk.score == nil ? "Sampling" : ""
            if reading.isCameraProjectedPath {
                leadSelectionStatus = risk.timeToCollision == nil
                    ? "Possible obstacle · sampling" : "Possible obstacle · gap closing"
            } else {
                leadSelectionStatus = risk.timeToCollision == nil ? "Closing vehicle · sampling" : "Closing vehicle ahead"
            }
            trackingStatus = "Collision hazard · \(reading.rangeSource) · braking not required"
            updateTripEvents()
            return
        }

        risk = estimator.update(time: reading.uptime, distance: distance,

                                egoSpeed: egoSpeed,

                                gpsAge: gpsAge,

                                confidence: reading.confidence)

        let cameraRisk = CollisionApproachRules.forConfirmedLead(lead: RiskSnapshot(), camera: cameraRange,
            confidence: reading.confidence, fresh: reading.isFreshDetection,
            pathSupported: reading.isPathSupported, rearConfirmed: reading.hasConfirmedRearIdentity)
        currentCameraRisk = cameraRisk.score == nil ? nil : cameraRisk
        risk = CollisionApproachRules.combine(lead: risk, approach: cameraRisk)
        if risk.distance == nil { risk.distance = distance }
        speedBlocker = ""
        collisionBlocker = ""
        if risk.leadSpeed == nil {
            if egoSpeed == nil {
                speedBlocker = "Needs GPS"
                leadSelectionStatus = "Lead confirmed · waiting for GPS"
            } else if reading.confidence < 0.65 {
                speedBlocker = "Low confidence"
                leadSelectionStatus = "Lead confirmed · low confidence"
            } else if risk.reason == "Collecting distance samples…" {
                speedBlocker = "Sampling"
                leadSelectionStatus = "Lead confirmed · sampling speed"
            } else {
                speedBlocker = "Range unsettled"
                leadSelectionStatus = "Lead confirmed · range unsettled"
            }
            collisionBlocker = risk.score == nil ? speedBlocker : ""
            if risk.timeToCollision != nil { leadSelectionStatus = "Lead \(reading.targetID.uuidString.prefix(4)) · gap closing" }
        }
        updateTripEvents()

    }

    private func tick() {

        let now = ProcessInfo.processInfo.systemUptime
        brakeWarningActive = brakeWarningLatch.isActive(at: now)

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
            updateTripEvents()

            return

        }

        if replaying {
            speed = replayEgoSpeed(at: now)
            expireCameraMeasurements(at: now)
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
        if let age = fusedGPSAge, age + max(0, now - fusedReceivedUptime) > 1.5 {
            clearFusionDisplay()
        }
        expireCameraMeasurements(at: now)

    }

    private func expireCameraMeasurements(at now: TimeInterval) {
        if !TrackingFreshness.measurementIsFresh(capturedAt: lastDetectionsUptime, now: now) {
            detections = []
        }
        if lanePath?.isUsable(at: now) == false { lanePath = nil }
        if roadRegion?.isUsable(at: now) == false { roadRegion = nil }
        guard targetID != nil else { return }
        if !TrackingFreshness.identityIsFresh(capturedAt: lastTrackingUptime, now: now) {
            clearTarget("Vehicle track expired · reacquiring the lead")
            return
        }
        if !TrackingFreshness.measurementIsFresh(capturedAt: lastTrackingUptime, now: now),
           trackedDistance != nil || !trackingCoasting || risk.score != nil || collisionHazardID != nil {
            trackedDistance = nil
            cameraRangeEstimator.reset()
            cameraRange = CameraRangeSnapshot(reason: "Camera measurement expired")
            trackingCoasting = true
            leadRearEvidenceStatus = "Lead identity held · camera measurements expired"
            collisionHazardID = nil
            speedBlocker = "Camera delayed"
            collisionBlocker = "Camera delayed"
            leadSelectionStatus = "Vehicle track held · waiting for a fresh camera measurement"
            invalidateRisk("Camera measurement expired. Estimates paused.")
        }
    }

    private func clearFusionDisplay() {
        fusedSpeed = nil
        fusedSpeedUncertainty = nil
        fusedGPSAge = nil
        fusedSpeedSource = "GPS fusion unavailable"
    }

    private func invalidateRisk(_ reason: String, preserveSamples: Bool = false) {

        if reason.contains("GPS"), let cameraRisk = currentCameraRisk,
           (0...0.6).contains(ProcessInfo.processInfo.systemUptime - lastTrackingUptime) {
            // Retain only independently supported camera risk. A higher GPS
            // headway/braking score must not survive after its inputs expire.
            estimator.reset()
            risk = cameraRisk
            risk.leadSpeed = nil; risk.leadAcceleration = nil; risk.headway = nil
            speedBlocker = "Needs GPS"
            collisionBlocker = ""
            return
        }
        currentCameraRisk = nil

        if preserveSamples {
            risk = estimator.pause(at: lastTrackingUptime, reason: reason)
        } else {
            estimator.reset()
            risk = RiskSnapshot(reason: reason)
        }
        collisionHazardID = nil

        if reason.contains("GPS") { speedBlocker = "Needs GPS"; collisionBlocker = "Needs GPS" }

    }

    private func updateImpactService() {

        if impactEnabled, tripActive, foreground, !demo { impactService.start() }

        else { impactService.stop() }

    }

    private func updateTripEvents() {
        let critical = risk.level == .critical
        let now = ProcessInfo.processInfo.systemUptime
        let alreadyVisible = brakeWarningLatch.isActive(at: now)
        if critical { brakeWarningLatch.trigger(at: now) }
        brakeWarningActive = brakeWarningLatch.isActive(at: now)
        if critical && !alreadyVisible {
            safety.brakeWarning()
        }
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

    // MARK: - Replay mode

    /// Runs a recorded drive through the live tracker and risk estimator. `track` supplies ego speed;
    /// without one, `assumedSpeed` (m/s) is used and labelled as an assumption on screen.
    func startReplay(_ clips: [ReplayService.Clip], track: SpeedTrack?, assumedSpeed: Double?, offset: TimeInterval) {
        guard !tripActive, !stopping, !demo, !replaying, !clips.isEmpty else { return }
        replayTrack = track
        replayAssumedSpeed = assumedSpeed
        replayOffset = offset
        replayAnchor = nil
        replayFrame = nil
        replaySpeedSource = track.map { "LOGGED SPEED · \($0.name)" }
            ?? (assumedSpeed != nil ? "ASSUMED SPEED · no speed log" : "NO SPEED LOG · estimates unavailable")
        if let fov = ReplayService.recordingFieldOfView() { camera.tracker.setHorizontalFieldOfView(fov) }
        camera.tracker.prepare()
        clearTarget("Replay · automatic lead candidate selection")
        replaying = true
        changingCameraGeometry = false
        latestGeometryChangeUptime = ProcessInfo.processInfo.systemUptime
        replayStatus = "Replaying recorded drive"
        gpsStatus = "Replay speed · recorded or assumed, not live GPS"
        gpsSimulated = true
        clearFusionDisplay()
        UIApplication.shared.isIdleTimerDisabled = true
        Task { await safety.prepareVoice() }
        replayGeneration = replay.play(clips, tracker: camera.tracker)
    }

    func stopReplay(_ status: String = "Replay stopped") {
        guard replaying else { return }
        replay.stop()
        replayGeneration = nil
        replaying = false
        replayStatus = status
        replayFrame = nil
        speed = nil
        replayTrack = nil
        replayAssumedSpeed = nil
        replayAnchor = nil
        gpsStatus = "GPS stopped"
        gpsSimulated = false
        egoAcceleration = nil
        clearTarget()
        changingCameraGeometry = captureGeometry.videoOrientation != desiredCaptureOrientation
        camera.setVideoOrientation(desiredCaptureOrientation)
        UIApplication.shared.isIdleTimerDisabled = false
    }

    private func replayEgoSpeed(at uptime: TimeInterval) -> Double? {
        guard let track = replayTrack else { return replayAssumedSpeed }
        guard let anchor = replayAnchor,
              let base = anchor.recorded ?? track.start?.addingTimeInterval(anchor.elapsed) else { return nil }
        return track.speed(at: base.addingTimeInterval(uptime - anchor.uptime + replayOffset))
    }

}
