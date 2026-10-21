import BrakeCheckCore

import SwiftUI

import Combine

import UIKit
import AVFoundation

@MainActor

final class DriveModel: ObservableObject {

    let camera = CameraService()
    let safety = SafetyService()
    let navigation = NavigationService()
    let replay = ReplayService()
    private let speedLog = SpeedLogWriter()
    private let tripMetricsStore = TripMetricsStore(url: FileManager.default.urls(for: .documentDirectory,
        in: .userDomainMask)[0].appendingPathComponent("TripMetrics/trip-history.json"))
    @Published private(set) var savedTrips: [TripMetrics] = []
    var tripHistoryURL: URL { tripMetricsStore.url }
    private var tripHadGPSSpeed = false
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
    private var lastTripPublishedSecond = -1
    private var brakeWarningLatch = BrakeWarningLatch()
    @Published private(set) var brakeWarningActive = false
    private var wasHardBrake = false
    private var wasCloseCall = false

    private let speedService = SpeedService()

    private let impactService = ImpactService()

    private var estimator = LeadRiskContinuity()
    private var cameraRangeEstimator = CameraRangeEstimator()
    var measurementRevision: String {
        "N" + (Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "?")
    }
    private var detectorMeasurementGeneration: Int?

    private var speedReading: GPSSpeedSample?
    private var lastLoggedGPSUptime: TimeInterval?
    private var gpsEstimateLeadEligible = false
    private var currentLeadConfidence = 0.0

    private var targetID: UUID?
    private var currentCameraRisk: RiskSnapshot?
    private var dangerContinuity = LeadDangerContinuity()
    private var laneDisplayContinuity = LaneDisplayContinuity(rejectOutliers: true, smoothingSeconds: 0.4)
    private var drivingAssist = DrivingAssistEngine()
    private var assistFeatures: Set<DrivingAssistFeature> = []
    private var assistLane: LanePath?
    private var assistSubscriptions = Set<AnyCancellable>()
    @Published private(set) var drivingAssistAlert: DrivingAssistAlert?

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
    @Published private(set) var gpsSpeedEstimated = false
    @Published private(set) var stationaryGPSConfirmed = false
    func hasStationaryGPS(at time: TimeInterval) -> Bool {
        foreground && tripActive && !demo && !replaying && speedReading?.isFreshStationary(at: time) == true
    }
    func hasMeasuredMovement(at time: TimeInterval) -> Bool { speedReading?.isConfidentlyMoving(at: time) == true }

    private var navigationMountCalibration: CameraRouteCalibration {
        let defaults = UserDefaults.standard
        return CameraRouteCalibration(heightMeters: defaults.object(forKey: "navigation.mountHeight") as? Double ?? 1.4,
            horizonOffset: defaults.double(forKey: "navigation.horizonOffset"), yawDegrees: defaults.double(forKey: "navigation.mountYaw"))
    }
    private func mountSignature(_ calibration: CameraRouteCalibration) -> String {
        CameraMountVerification.signature(width: captureGeometry.width, height: captureGeometry.height,
            fov: captureGeometry.horizontalFieldOfView, orientation: captureGeometry.videoOrientation.rawValue, calibration: calibration)
    }
    private var cameraOrientationSignature: String {
        mountSignature(CameraRouteCalibration(heightMeters: navigationMountCalibration.heightMeters))
    }
    var cameraAlignmentVerified: Bool {
        let signature = mountSignature(navigationMountCalibration)
        return signature != "unavailable" && UserDefaults.standard.string(forKey: "navigation.verifiedMount") == signature
    }
    var cameraMountConfirmed: Bool {
        let signature = cameraOrientationSignature
        return signature != "unavailable" && (cameraAlignmentVerified
            || UserDefaults.standard.string(forKey: "navigation.confirmedMount") == signature)
    }
    /// Parked setup saves the mount/height without needing visible lane paint.
    /// Stable road geometry may finish alignment later, without driver input.
    func confirmCameraMount(parkedAcknowledged: Bool) -> Bool {
        let now = ProcessInfo.processInfo.systemUptime
        guard parkedAcknowledged, foreground, tripActive, !demo, !replaying,
              cameraReady, !changingCameraGeometry, !hasMeasuredMovement(at: now) else { return false }
        let calibration = navigationMountCalibration
        let signature = cameraOrientationSignature
        guard signature != "unavailable", calibration.heightMeters.isFinite, (0.5...3).contains(calibration.heightMeters) else { return false }
        UserDefaults.standard.set(signature, forKey: "navigation.confirmedMount")
        arPresentation.setMountConfirmed(true)
        return true
    }
    private func completeRoadAlignmentForConfirmedMount(at now: TimeInterval) -> Bool {
        // A saved pose is configuration; avoid rewriting defaults at camera frame cadence.
        if cameraAlignmentVerified { return true }
        guard cameraMountConfirmed, foreground, tripActive, !demo, !replaying, cameraReady, !changingCameraGeometry,
              let record = cameraOrientationLock.record, record.signature == cameraOrientationSignature,
              let scene = arScene, now >= scene.capturedAt, now - scene.capturedAt <= 0.35,
              now >= scene.laneObservedAt, now - scene.laneObservedAt <= 0.8,
              let lane = scene.lane, lane.isUsable(at: now),
              lane.source != .markings || lane.confidence >= 0.85,
              let fov = captureGeometry.horizontalFieldOfView,
              let current = CameraCalibrationGeometry.estimate(lane: lane, fov: fov, aspect: captureGeometry.aspect),
              CameraOrientationLock.agrees(current, record.estimate) else { return false }
        let signature = mountSignature(navigationMountCalibration)
        guard signature != "unavailable" else { return false }
        let defaults = UserDefaults.standard
        defaults.set(record.estimate.horizon, forKey: "navigation.verifiedHorizon")
        defaults.set(record.estimate.yawDegrees, forKey: "navigation.verifiedYawEstimate")
        defaults.set(signature, forKey: "navigation.verifiedMount")
        arPresentation.setAlignmentLocked(true)
        return true
    }
    @Published private(set) var estimatedLeadSpeed: Double?

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

    @Published var risk = RiskSnapshot() {
        didSet { refreshDangerMeter(at: ProcessInfo.processInfo.systemUptime) }
    }
    @Published private(set) var dangerMeter = DangerMeterSnapshot()

    @Published var trackingBox: CGRect?
    @Published var lanePath: LanePath?
    let arPresentation = CameraARPresentation()
    var arScene: CameraARScene? { arPresentation.scene }
    var cameraCalibrationEstimate: CameraCalibrationEstimate? { arPresentation.calibrationEstimate }
    private var arCameraVisible = false
    private var arCalibrationVisible = false
    func setARCameraVisible(_ visible: Bool) { arCameraVisible = visible; applyARDemand() }
    func setARCalibrationVisible(_ visible: Bool) { arCalibrationVisible = visible; applyARDemand() }
    func requestAutomaticCameraCalibration() {
        cameraCalibrationAssistant = CameraCalibrationAssistant()
        cameraOrientationLock.reset(); lastAppliedOrientation = nil
        UserDefaults.standard.removeObject(forKey: "navigation.orientationRecord")
        UserDefaults.standard.removeObject(forKey: "navigation.confirmedMount")
        UserDefaults.standard.set("", forKey: "navigation.verifiedMount")
        arPresentation.clearCalibration()
    }
    private func applyARDemand() {
        camera.tracker.setARProcessingEnabled(arCameraVisible || arCalibrationVisible)
        if !arCameraVisible && !arCalibrationVisible { arPresentation.reset() }
    }
    private var cameraCalibrationAssistant = CameraCalibrationAssistant()
    private var calibrationGeometry: CaptureFrameGeometry?
    private var cameraOrientationLock: CameraOrientationLock = {
        let data = UserDefaults.standard.data(forKey: "navigation.orientationRecord")
        let restored = data.flatMap { try? JSONDecoder().decode(CameraOrientationRecord.self, from: $0) }
        return CameraOrientationLock(restored: restored)
    }()
    private var lastAppliedOrientation: CameraOrientationRecord?
    @Published var trackingCoasting = false

    @Published var trackingStatus = "Automatic lead candidate selection starts with the camera."
    @Published private(set) var detectorReady = false

    @Published var cameraStatus = "Camera stopped"

    @Published var gpsStatus = "GPS stopped"

    @Published var recorderStatus = "Recorder stopped"
    @Published var recordingStorageBytes: Int64?

    @Published var motionStatus = "Impact sensing off"

    @Published var incidents: [IncidentManifest] = []
    /// Local GPS trail and flagged moments for the trip detail page.
    let tripRoute = TripRouteRecorder()

    @Published var message: String?

    @Published var impactEnabled = false {

        didSet { updateImpactService() }

    }

    init() {
        PremiumStore.shared.$isSubscribed.removeDuplicates().receive(on: RunLoop.main)
            .sink { [weak self] _ in self?.syncDrivingAssists() }.store(in: &assistSubscriptions)
        NotificationCenter.default.publisher(for: UserDefaults.didChangeNotification)
            .receive(on: RunLoop.main).sink { [weak self] _ in self?.syncDrivingAssists() }
            .store(in: &assistSubscriptions)
        SpeechVoicePreferences.restoreOriginalDefaultsIfNeeded()
        do {
            savedTrips = try tripMetricsStore.load()
            if let last = savedTrips.first {
                tripMinutes = last.durationSeconds / 60; maxSpeedMPH = last.maxSpeedMPH ?? 0
                hardBrakes = last.estimatedLeadHardBrakes; closeCalls = last.estimatedCloseCalls
            }
        } catch { message = "Could not read saved trip metrics. Your existing history has been kept." }

        navigation.useMPH = UserDefaults.standard.object(forKey: "display.mph") as? Bool ?? true
        navigation.canSpeak = { [weak self] in
            guard let self else { return false }
            return self.foreground && self.tripActive && !self.demo && !self.replaying
                && !self.brakeWarningActive && !self.safety.checkinPresented && !self.safety.audio.isBusy
        }
        safety.beforePriorityAudio = { [weak self] in
            self?.navigation.stopSpeaking(deactivateSession: false)
        }

        speedService.onSpeed = { [weak self] reading in

            guard let self, self.foreground, self.tripActive, !self.demo else { return }

            self.speedReading = reading
            self.camera.tracker.updateEgoSpeed(reading)

            self.gpsSimulated = reading?.simulated ?? false

            if let reading, self.lastLoggedGPSUptime.map({ reading.measurementUptime > $0 }) ?? true {
                self.tripHadGPSSpeed = true
                self.maxSpeedMPH = max(self.maxSpeedMPH, reading.metersPerSecond * 2.2369362921)
                // Logged at measurement time so replay can line speed up with the video.
                let measured = Date().addingTimeInterval(reading.measurementUptime - ProcessInfo.processInfo.systemUptime)
                self.speedLog.append(speed: reading.metersPerSecond, accuracy: reading.accuracy, at: measured)
                self.lastLoggedGPSUptime = reading.measurementUptime
                if let location = self.speedService.latestLocation { self.tripRoute.add(location) }
            }

            if reading == nil { self.invalidateRisk("GPS speed unavailable.") }
            self.refreshGPSDisplay(at: ProcessInfo.processInfo.systemUptime)

        }

        speedService.onFusedSpeed = { [weak self] estimate in
            guard let self, self.foreground, self.tripActive, !self.demo else { return }
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

        camera.onRecordingStorage = { [weak self] bytes in
            DispatchQueue.main.async { self?.recordingStorageBytes = bytes }
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
                    now: ProcessInfo.processInfo.systemUptime) else {
                    if !self.detections.isEmpty { self.detections = [] }
                    return
                }
                self.lastDetectionsUptime = uptime
                self.processAssistVehicles(detections, capturedAt: uptime)
                if !detections.isEmpty || !self.detections.isEmpty { self.detections = detections }

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
            // Perception guides are optional diagnostics. Safety and AR use
            // the tracker's lane directly, so hidden guides need no UI work.
            guard UserDefaults.standard.bool(forKey: "display.laneGuides")
                || DrivingAssistFeature.offered.contains(where: { UserDefaults.standard.bool(forKey: $0.preferenceKey) }) else { return }
            DispatchQueue.main.async {
                guard let self, self.foreground, self.tripActive || self.replaying, !self.demo else { return }
                guard !self.changingCameraGeometry,
                      lane.map({ $0.capturedAt >= self.latestGeometryChangeUptime }) ?? true else { return }
                self.processAssistLane(lane)
                guard UserDefaults.standard.bool(forKey: "display.laneGuides") else { return }
                let path = self.laneDisplayContinuity.update(lane,
                    at: ProcessInfo.processInfo.systemUptime)
                if self.lanePath?.capturedAt != path?.capturedAt { self.lanePath = path }
            }
        }
        camera.tracker.onTrafficSignal = { [weak self] observation, capturedAt in
            DispatchQueue.main.async {
                guard let self, capturedAt >= max(self.latestGeometryChangeUptime, self.latestTrackingResetUptime) else { return }
                let now = ProcessInfo.processInfo.systemUptime
                let stationary = self.hasStationaryGPS(at: now)
                    && self.speedService.reading(at: capturedAt)?.isFreshStationary(at: capturedAt) == true
                let alert = self.drivingAssist.updateSignal(observation, stationary: stationary,
                    enabled: self.assistAllowed(.greenLight), now: now)
                self.deliverAssist(alert)
            }
        }
        camera.tracker.onDetectorReady = { [weak self] ready in
            DispatchQueue.main.async {
                guard let self, self.detectorReady != ready else { return }
                self.detectorReady = ready
            }
        }
        camera.tracker.onARScene = { [weak self] scene in
            DispatchQueue.main.async {
                guard let self, self.foreground, self.tripActive, !self.replaying, !self.demo,
                      self.arCameraVisible || self.arCalibrationVisible,
                      !self.changingCameraGeometry,
                      scene.map({ $0.capturedAt >= self.latestGeometryChangeUptime }) ?? true else { return }
                if self.calibrationGeometry != self.captureGeometry {
                    self.cameraCalibrationAssistant = CameraCalibrationAssistant()
                    self.calibrationGeometry = self.captureGeometry
                }
                let now = ProcessInfo.processInfo.systemUptime
                let estimate = scene.flatMap { scene -> CameraCalibrationEstimate? in
                    guard let lane = scene.lane, lane.source != .markings || lane.confidence >= 0.85,
                          now >= scene.laneObservedAt, now - scene.laneObservedAt <= 0.8,
                          let fov = self.captureGeometry.horizontalFieldOfView else { return nil }
                    return CameraCalibrationGeometry.estimate(lane: lane, fov: fov, aspect: self.captureGeometry.aspect)
                }
                let suggestion = self.cameraCalibrationAssistant.update(estimate, observedAt: estimate == nil ? now : (scene?.laneObservedAt ?? now))
                let defaults = UserDefaults.standard
                let locked = self.cameraOrientationLock.update(suggestion,
                    signature: self.cameraOrientationSignature,
                    observedAt: scene?.laneObservedAt ?? now)
                let record = self.cameraOrientationLock.record
                if record != self.lastAppliedOrientation {
                    if let record, let data = try? JSONEncoder().encode(record) {
                        defaults.set(data, forKey: "navigation.orientationRecord")
                    } else { defaults.removeObject(forKey: "navigation.orientationRecord") }
                    if let locked, defaults.object(forKey: "navigation.automaticCalibration") == nil || defaults.bool(forKey: "navigation.automaticCalibration") {
                        let verified = self.cameraAlignmentVerified
                            && abs(locked.horizon - defaults.double(forKey: "navigation.verifiedHorizon")) <= 0.05
                            && abs(locked.yawDegrees - defaults.double(forKey: "navigation.verifiedYawEstimate")) <= 3
                        if !verified {
                            defaults.set(min(30, max(-30, locked.yawDegrees)), forKey: "navigation.mountYaw")
                            defaults.set(0.0, forKey: "navigation.horizonOffset")
                            defaults.set("", forKey: "navigation.verifiedMount")
                        }
                    }
                    self.lastAppliedOrientation = record
                }
                self.arPresentation.update(scene: scene, estimate: locked,
                    calibrationProgress: self.cameraCalibrationAssistant.observationCount,
                    alignmentLocked: self.cameraAlignmentVerified, mountConfirmed: self.cameraMountConfirmed)
                _ = self.completeRoadAlignmentForConfirmedMount(at: now)

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
                let blocker = reason.localizedCaseInsensitiveContains("clipped") ? "Vehicle clipped"
                    : reason.localizedCaseInsensitiveContains("detection gap") ? "Detection gap" : "Needs target"
                self?.speedBlocker = blocker
                self?.collisionBlocker = blocker

                self?.trackingStatus = reason; self?.invalidateRisk(reason)

            }

        }

        reconcileDisplayTimer()

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
                if status.hasPrefix("Could not") || status.hasPrefix("No video") || status.hasPrefix("No readable") {
                    self.message = status
                }
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
        lastTripPublishedSecond = -1
        tripHadGPSSpeed = false
        lastLoggedGPSUptime = nil
        speedLog.start(at: tripStarted)
        wasHardBrake = false; wasCloseCall = false
        tripRoute.start(destination: navigation.isNavigating ? navigation.destinationTitle : nil)
        Task { await safety.prepareVoice() }

        clearFusionDisplay()
        speedService.start()

        UIApplication.shared.isIdleTimerDisabled = true

        requestCameraIfNeeded()

        updateImpactService()
        reconcileDisplayTimer()

    }

    func startNavigationDrive() {
        guard !stopping, !demo, !replaying, navigation.canStartGuidance else { return }
        navigation.startGuidance()
        if navigation.isNavigating && !tripActive { startTrip() }
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

        navigation.stopGuidance()

        tripActive = false
        let endedAt = Date()
        tripMinutes = max(0, endedAt.timeIntervalSince(tripStarted) / 60)
        do {
            let trip = TripMetrics(startedAt: tripStarted, endedAt: endedAt,
                maxSpeedMPH: tripHadGPSSpeed ? maxSpeedMPH : nil,
                estimatedLeadHardBrakes: hardBrakes, estimatedCloseCalls: closeCalls)
            savedTrips = try tripMetricsStore.save(trip)
            tripRoute.finish(tripID: trip.id)
        } catch {
            tripRoute.cancel()
            message = "Could not save this trip's metrics. Existing trip history has been kept."
        }
        safety.sendTrip(minutes: tripMinutes,
                        hardBrakes: hardBrakes, closeCalls: closeCalls, maxSpeedMPH: maxSpeedMPH)

        stopping = true

        speedLog.stop()
        speedService.stop()
        clearFusionDisplay()

        impactService.stop()

        speedReading = nil

        speed = nil

        egoAcceleration = nil

        gpsAccuracy = nil; gpsSimulated = false; gpsSpeedEstimated = false

        clearTarget("Trip ended")
        reconcileDisplayTimer()

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

        navigation.setForeground(phase == .active)

        if phase != .active {
            guard foreground else { return }

            foreground = false
            reconcileDisplayTimer()
            speedService.stop()
            clearFusionDisplay()
            speedReading = nil
            speed = nil
            gpsSpeedEstimated = false
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
            brakeWarningActive = brakeWarningLatch.isActive(at: ProcessInfo.processInfo.systemUptime)
            safety.resumeForegroundPolling()

            if tripActive {
                speedService.start()

                UIApplication.shared.isIdleTimerDisabled = true

                if resumed { requestCameraIfNeeded() }

                updateImpactService()

            }
            reconcileDisplayTimer()

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
        drivingAssist.reset(); drivingAssistAlert = nil; assistLane = nil
        latestTrackingResetUptime = ProcessInfo.processInfo.systemUptime

        targetID = nil
        gpsEstimateLeadEligible = false
        estimatedLeadSpeed = nil
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
        let blocker = reason.localizedCaseInsensitiveContains("clipped") ? "Vehicle clipped"
            : reason.localizedCaseInsensitiveContains("detection gap") ? "Detection gap" : "Needs target"
        speedBlocker = blocker
        collisionBlocker = blocker
        frameAlignmentStatus = "Waiting for camera and GPS"
        laneDisplayContinuity.reset()
        lanePath = nil
        arPresentation.reset(); cameraCalibrationAssistant = CameraCalibrationAssistant()

        trackingStatus = reason

        leadSelectionStatus = "Looking for a car ahead"

        detections = []

        invalidateRisk(reason)

    }

    func markIncident() {

        guard tripActive, !demo else { return }

        camera.markIncident(reason: "Manually marked incident")

        message = "Available footage is being preserved. Keep BrakeSense visible to capture the next 15 minutes."

    }

    func deleteIncident(_ id: UUID) { camera.deleteIncident(id) }
    func applyRecordingPreferences() { camera.applyRecordingPreferences() }

    func setDemo(_ enabled: Bool) {

        guard !tripActive, !stopping, !replaying else { return }

        navigation.setLiveModeAllowed(!enabled)

        demo = enabled
        gpsSpeedEstimated = false
        gpsEstimateLeadEligible = false
        estimatedLeadSpeed = nil
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
        reconcileDisplayTimer()

    }

    private func consume(_ reading: TrackingReading) {
        defer { processAssistLead(reading) }

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
        gpsEstimateLeadEligible = false
        estimatedLeadSpeed = nil
        currentLeadConfidence = reading.confidence
        trackingBox = reading.displayBox ?? reading.box

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
            frameAlignmentStatus = String(format: "\(measurementRevision) · camera frame age %.0f ms · outside 0–600 ms freshness limit", (now - reading.uptime) * 1000)

            invalidateRisk("Camera processing is behind. Estimates paused.")

            return

        }

        trackingCoasting = reading.isDetectorSettling || !reading.isFreshDetection
            || (!reading.isPathSupported && !reading.hasConfirmedRearIdentity)

        rangeSource = reading.rangeSource
        frameAlignmentStatus = String(format: "\(measurementRevision) · camera frame age %.0f ms", (now - reading.uptime) * 1000)
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
        if reading.isFreshDetection, let captured = reading.cameraRangeCapture,
           captured.isCurrent(for: reading.targetID, at: reading.uptime) {
            // Confirmation does not restart motion already measured on this
            // same ID. An outlier's nil snapshot must still pause the output.
            cameraRange = captured.snapshot
            cameraRangeEstimator.reset()
        } else {
            cameraRange = cameraRangeEstimator.update(targetID: reading.targetID,
                time: reading.uptime, distance: distance,
                confidence: reading.confidence, fresh: reading.isFreshDetection)
        }
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
        gpsEstimateLeadEligible = reading.canEstimateLeadSpeed && reading.hasConfirmedRearIdentity
        if !reading.isPathSupported, reading.hasConfirmedRearIdentity {
            // Lane uncertainty does not invalidate relative range/motion on
            // the same confirmed lead. A separate approximate speed may be
            // displayed, without supplying absolute acceleration or headway.
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
            refreshEstimatedLeadSpeed(at: now)
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
                    egoSpeed: egoSpeed, gpsAge: gpsAge, confidence: reading.confidence, camera: cameraRange)
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
            refreshEstimatedLeadSpeed(at: now)
            updateTripEvents()
            return
        }

        risk = estimator.update(time: reading.uptime, distance: distance,

                                egoSpeed: egoSpeed,

                                gpsAge: gpsAge,

                                confidence: reading.confidence, camera: cameraRange)

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
            collisionBlocker = risk.score == nil
                ? reading.confidence < 0.65 ? "Low confidence"
                    : !reading.hasConfirmedRearIdentity ? "Checking lead" : "Sampling motion"
                : ""
            if risk.timeToCollision != nil { leadSelectionStatus = "Lead \(reading.targetID.uuidString.prefix(4)) · gap closing" }
        }
        refreshEstimatedLeadSpeed(at: now)
        updateTripEvents()

    }

    private func reconcileDisplayTimer() {
        let wanted = foreground && (tripActive || demo || replaying || brakeWarningActive)
        guard wanted else { timer?.invalidate(); timer = nil; return }
        guard timer == nil else { return }
        let timer = Timer(timeInterval: 0.2, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in self?.tick() }
        }
        timer.tolerance = 0.04
        self.timer = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    private func tick() {
        syncDrivingAssists()
        guard foreground else { reconcileDisplayTimer(); return }

        let now = ProcessInfo.processInfo.systemUptime
        let warningActive = brakeWarningLatch.isActive(at: now)
        if brakeWarningActive != warningActive { brakeWarningActive = warningActive }
        reconcileDisplayTimer()
        refreshDangerMeter(at: now)

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
        let elapsed = Date().timeIntervalSince(tripStarted)
        if Int(elapsed) != lastTripPublishedSecond {
            lastTripPublishedSecond = Int(elapsed)
            tripMinutes = elapsed / 60
        }

        if let reading = speedReading, now - reading.measurementUptime > 1.5 {

            speedReading = nil
            camera.tracker.updateEgoSpeed(nil)
            invalidateRisk("Waiting for a fresh GPS speed.")

        }
        refreshGPSDisplay(at: now)

        // A fusion estimate is only a display diagnostic. Its own source
        // reports GPS age; clear it when the source stops issuing estimates.
        if let age = fusedGPSAge, age + max(0, now - fusedReceivedUptime) > 1.5 {
            clearFusionDisplay()
        }
        expireCameraMeasurements(at: now)

    }

    private func expireCameraMeasurements(at now: TimeInterval) {
        if !detections.isEmpty, !TrackingFreshness.measurementIsFresh(capturedAt: lastDetectionsUptime, now: now) {
            detections = []
        }
        if UserDefaults.standard.bool(forKey: "display.laneGuides") {
            let displayedLane = laneDisplayContinuity.current(at: now)
            if lanePath?.capturedAt != displayedLane?.capturedAt { lanePath = displayedLane }
        }
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

    private func refreshGPSDisplay(at now: TimeInterval) {
        let stationary = hasStationaryGPS(at: now)
        if stationaryGPSConfirmed != stationary { stationaryGPSConfirmed = stationary }
        guard tripActive, !demo, !replaying else {
            gpsSpeedEstimated = false; estimatedLeadSpeed = nil
            return
        }
        let hadSpeed = speed != nil
        let estimate = speedService.estimate(at: now)
        if speed != estimate?.metersPerSecond { speed = estimate?.metersPerSecond }
        if gpsAccuracy != estimate?.accuracy { gpsAccuracy = estimate?.accuracy }
        let estimated = estimate?.isEstimated ?? false
        if gpsSpeedEstimated != estimated { gpsSpeedEstimated = estimated }
        let acceleration = estimated ? nil : speedService.reading(at: now)?.acceleration
        if egoAcceleration != acceleration { egoAcceleration = acceleration }
        if let estimate, estimate.isEstimated {
            gpsStatus = String(format: "GPS gap · estimated speed · last fix %.1f s old · uncertainty ±%.1f m/s",
                now - estimate.lastMeasurementUptime, estimate.accuracy)
        } else if estimate == nil, hadSpeed {
            gpsStatus = "GPS speed unavailable · short estimate expired"
            gpsSimulated = false
            invalidateRisk("GPS speed unavailable after the short estimate expired.")
        }
        refreshEstimatedLeadSpeed(at: now)
    }

    private func refreshEstimatedLeadSpeed(at now: TimeInterval) {
        guard !demo, !replaying, risk.leadSpeed == nil,
              gpsEstimateLeadEligible, let id = targetID, leadTargetID == id,
              speedService.estimate(at: now) != nil else {
            if estimatedLeadSpeed != nil { estimatedLeadSpeed = nil }
            return
        }
        // Use the speed at the camera capture, rather than today's newest
        // callback. If the next fix has arrived, the display can interpolate.
        let next = GPSSpeedDisplayRules.estimatedLeadSpeed(ego: speedService.estimate(at: lastTrackingUptime),
            camera: cameraRange, confidence: currentLeadConfidence, rearConfirmed: true,
            capturedAt: lastTrackingUptime, now: now)
        if estimatedLeadSpeed != next { estimatedLeadSpeed = next }
    }

    private func assistAllowed(_ feature: DrivingAssistFeature) -> Bool {
        foreground && tripActive && cameraReady && !replaying && !demo && !changingCameraGeometry
            && PremiumStore.shared.isSubscribed && assistFeatures.contains(feature)
            && UserDefaults.standard.bool(forKey: feature.preferenceKey)
            && !brakeWarningActive && risk.level != .critical && !safety.checkinPresented
    }

    private func syncDrivingAssists() {
        let next = DrivingAssistFeature.enabled(in: .standard, premium: PremiumStore.shared.isSubscribed)
        if next != assistFeatures {
            assistFeatures = next; drivingAssist.reset(); drivingAssistAlert = nil; assistLane = nil
        }
        let now = ProcessInfo.processInfo.systemUptime
        camera.tracker.setTrafficSignalEnabled(assistAllowed(.greenLight) && hasStationaryGPS(at: now))
        if let alert = drivingAssistAlert,
           !assistAllowed(alert.feature) || now - alert.capturedAt > 3 {
            drivingAssistAlert = nil
        }
    }

    private func processAssistLead(_ reading: TrackingReading) {
        let now = ProcessInfo.processInfo.systemUptime
        let current = reading.isFreshDetection && !reading.isDetectorSettling && reading.isPathSupported
            && targetID == reading.targetID && lastTrackingUptime == reading.uptime
            && TrackingFreshness.measurementIsFresh(capturedAt: reading.uptime, now: now)
        let stationary = hasStationaryGPS(at: now)
            && speedService.reading(at: reading.uptime)?.isFreshStationary(at: reading.uptime) == true
        let alert = drivingAssist.updateLead(id: reading.targetID,
            distance: current ? cameraRange.distance : nil, rangeRate: current ? cameraRange.rangeRate : nil,
            confidence: reading.confidence, rearConfirmed: reading.hasConfirmedRearIdentity,
            stationary: stationary, enabled: assistAllowed(.trafficStart), capturedAt: reading.uptime, now: now)
        deliverAssist(alert)
    }

    private func processAssistLane(_ lane: LanePath?) {
        guard !assistFeatures.isEmpty else { return }
        assistLane = lane
        let now = ProcessInfo.processInfo.systemUptime
        let fix = lane.flatMap { speedService.reading(at: $0.capturedAt) }
        let speed = fix.flatMap { $0.isConfidentlyMoving(at: now) && $0.accuracy <= 2 ? $0.metersPerSecond : nil }
        // Typed steps: as one closure of untyped literals, Xcode's type checker timed out here.
        var center = Double.nan
        if let fov = captureGeometry.horizontalFieldOfView {
            let yawRadians: Double = navigationMountCalibration.yawDegrees * Double.pi / 180
            let halfFieldRadians: Double = fov * Double.pi / 360
            center = 0.5 + tan(yawRadians) / (2 * tan(halfFieldRadians))
        }
        // Planned turns and route rejoining can intentionally cross lane paint.
        let maneuver = navigation.isNavigating && ((navigation.maneuverDistance ?? .infinity) < 100
            || navigation.isRejoiningRoute)
        let alert = drivingAssist.updateLane(lane, cameraCenter: center, speed: speed,
            aligned: cameraAlignmentVerified && !maneuver, enabled: assistAllowed(.laneDeparture), now: now)
        deliverAssist(alert)
    }

    private func processAssistVehicles(_ detections: [VehicleDetection], capturedAt: TimeInterval) {
        guard !assistFeatures.isEmpty else { return }
        let now = ProcessInfo.processInfo.systemUptime
        let fix = speedService.reading(at: capturedAt)
        let speed = fix.flatMap { $0.isConfidentlyMoving(at: now) && $0.accuracy <= 2 ? $0.metersPerSecond : nil }
        let vehicles = detections.filter { $0.isFreshDetection && capturedAt - $0.capturedAt <= 0.1 }.map {
            LeadVehicle(box: $0.box, label: $0.label, confidence: $0.confidence,
                        viewpoint: $0.viewpoint, trackID: $0.id)
        }
        let alert = drivingAssist.updateVehicles(vehicles, lane: assistLane, speed: speed,
            enabled: assistAllowed(.cutIn) && cameraAlignmentVerified,
            capturedAt: capturedAt, now: now)
        deliverAssist(alert)
    }

    private func deliverAssist(_ alert: DrivingAssistAlert?) {
        guard let alert, assistAllowed(alert.feature), risk.level != .high,
              ProcessInfo.processInfo.systemUptime - alert.capturedAt <= 0.5 else { return }
        drivingAssistAlert = alert
        UINotificationFeedbackGenerator().notificationOccurred(.warning)
        if safety.settings.voiceEnabled, !safety.audio.isBusy, !navigation.isSpeakingDirections {
            safety.audio.speakDrivingAssist(alert.feature, language: safety.settings.language)
        }
    }

    private func invalidateRisk(_ reason: String, preserveSamples: Bool = false) {

        estimatedLeadSpeed = nil
        // Numeric estimates expire here; the presentation-only danger meter
        // may retain its last level for up to three seconds while a car remains.

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

    private func refreshDangerMeter(at now: TimeInterval) {
        if demo {
            dangerContinuity.reset()
            dangerMeter = DangerMeterSnapshot(level: risk.level, score: risk.score)
            return
        }
        let next = dangerContinuity.update(targetID: targetID,
            capturedAt: lastTrackingUptime, now: now, risk: risk,
            hasVehicle: targetID != nil || detections.contains { ["car", "truck", "bus", "motorcycle"].contains($0.label) })
        if dangerMeter != next { dangerMeter = next }
    }

    private func updateImpactService() {

        if impactEnabled, tripActive, foreground, !demo { impactService.start() }

        else { impactService.stop() }

    }

    private func updateTripEvents() {
        let critical = risk.level == .critical
        let now = ProcessInfo.processInfo.systemUptime
        let alreadyVisible = brakeWarningLatch.isActive(at: now)
        if critical {
            navigation.stopSpeaking()
            brakeWarningLatch.trigger(at: now)
        }
        let active = brakeWarningLatch.isActive(at: now)
        if brakeWarningActive != active { brakeWarningActive = active }
        if critical && !alreadyVisible {
            safety.brakeWarning()
            tripRoute.add(.brakeAlert, at: speedService.latestLocation,
                speedMPH: speed.map { $0 * 2.2369362921 }, closingSeconds: risk.timeToCollision)
        }
        let braking = (risk.leadAcceleration ?? 0) < -3
        if braking && !wasHardBrake {
            hardBrakes += 1
            tripRoute.add(.hardBrake, at: speedService.latestLocation,
                speedMPH: speed.map { $0 * 2.2369362921 }, closingSeconds: nil)
        }
        wasHardBrake = braking
        let close = risk.timeToCollision.map { $0 < 2 } ?? false
        if close && !wasCloseCall {
            closeCalls += 1
            tripRoute.add(.closeCall, at: speedService.latestLocation,
                speedMPH: speed.map { $0 * 2.2369362921 }, closingSeconds: risk.timeToCollision)
        }
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
        navigation.setLiveModeAllowed(false)
        gpsSpeedEstimated = false
        estimatedLeadSpeed = nil
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
        reconcileDisplayTimer()
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
        reconcileDisplayTimer()
        navigation.setLiveModeAllowed(true)
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
