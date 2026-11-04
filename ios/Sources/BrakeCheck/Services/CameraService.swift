import BrakeCheckCore
import AVFoundation
import Foundation
import UIKit

final class CameraService: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let session = AVCaptureSession()
    let tracker = VehicleTracker()
    private let queue = DispatchQueue(label: "BrakeCheck.capture", qos: .userInitiated,
                                     autoreleaseFrequency: .workItem)
    private var output = AVCaptureVideoDataOutput()
    private var configured = false
    private var geometryAvailable = false
    private var captureTimestamp = CaptureTimestamp()
    private var recovery = CameraSessionRecovery()
    private var wantsRunning: Bool { recovery.wantsRunning }
    private var recoveryWork: DispatchWorkItem?
    private var watchdog: DispatchSourceTimer?
    private var visionCadence = FrameCadence()
    private var runRevision = 0
    private var desiredOrientation: AVCaptureVideoOrientation = .portrait
    private var nativeHorizontalFOV: Double?
    private var geometry = CaptureFrameGeometry.standby
    private var geometryRevision = 0
    private var geometryChanging = false
    private var framesEligibleAfter: TimeInterval = 0
    private var observers: [NSObjectProtocol] = []
    private var captureDevice: AVCaptureDevice?
    private var lastDiagnosticsTime: TimeInterval = 0
    private var lastFramePTS: Double?
    private var measuredFPS: Double?
    private lazy var recorder = QueuedSegmentRecorder(
        onStatus: { [weak self] message in self?.onRecorderStatus?(message) },
        onIncidents: { [weak self] events in self?.onIncidents?(events) },
        onStorage: { [weak self] bytes in self?.onRecordingStorage?(bytes) },
        onRecovery: { [weak self] clips, warnings in self?.onRecordingRecovery?(clips, warnings) },
        onEnabled: { [weak self] enabled in self?.onRecordingEnabled?(enabled) })
    var onCameraStatus: ((String, Bool) -> Void)?
    var onRecorderStatus: ((String) -> Void)?
    var onRecordingEnabled: ((Bool) -> Void)?
    var onIncidents: (([IncidentManifest]) -> Void)?
    var onRecordingStorage: ((Int64?) -> Void)?
    var onRecordingRecovery: (([UnindexedRecording], [String]) -> Void)?
    var onTrackingReset: ((String) -> Void)?
    var onGeometry: ((CaptureFrameGeometry) -> Void)?

    override init() {
        super.init()
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: AVCaptureSession.wasInterruptedNotification,
                                             object: session, queue: nil) { [weak self] _ in
            guard let self else { return }
            self.queue.async {
                guard self.wantsRunning else { return }
                self.watchdog?.cancel(); self.watchdog = nil
                self.recoveryWork?.cancel(); self.recoveryWork = nil
                self.recovery.beginInterruption()
                self.resetCaptureMeasurements("Camera interrupted. Tracking will reacquire when it returns.")
                self.recorder.pause(reason: "Camera interrupted; video has a gap.")
                self.onCameraStatus?("Camera interrupted", false)
            }
        })
        observers.append(center.addObserver(forName: AVCaptureSession.interruptionEndedNotification,
                                             object: session, queue: nil) { [weak self] _ in
            guard let self else { return }
            self.queue.async {
                guard self.recovery.endInterruption() else { return }
                self.restartCapture()
            }
        })
        observers.append(center.addObserver(forName: AVCaptureSession.runtimeErrorNotification,
                                             object: session, queue: nil) { [weak self] note in
            guard let self else { return }
            let error = note.userInfo?[AVCaptureSessionErrorKey] as? NSError
            let description = error?.localizedDescription ?? "Unknown camera error"
            let recoverable = error?.domain == AVFoundationErrorDomain && error?.code == AVError.mediaServicesWereReset.rawValue
            self.queue.async {
                guard self.wantsRunning else { return }
                self.watchdog?.cancel(); self.watchdog = nil
                self.resetCaptureMeasurements("Camera reset. Tracking measurements were cleared.")
                self.recorder.pause(reason: "Camera error; recording interrupted.")
                if recoverable { self.scheduleRecovery() }
                else {
                    self.recoveryWork?.cancel(); self.recoveryWork = nil
                    self.recovery.stop()
                    if self.session.isRunning { self.session.stopRunning() }
                    self.onCameraStatus?("Camera: \(description). Use Retry camera when ready.", false)
                }
            }
        })
    }

    deinit { watchdog?.cancel(); recoveryWork?.cancel(); observers.forEach(NotificationCenter.default.removeObserver) }

    private func monitorFrames() {
        watchdog?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 1, repeating: 1, leeway: .milliseconds(100))
        timer.setEventHandler { [weak self] in
            guard let self, self.recovery.needsRestart(at: ProcessInfo.processInfo.systemUptime) else { return }
            self.watchdog?.cancel(); self.watchdog = nil
            self.resetCaptureMeasurements("Camera frames stopped. Tracking measurements were cleared.")
            self.recorder.pause(reason: "Camera frames stopped; recording has a gap.")
            if self.session.isRunning { self.session.stopRunning() }
            self.scheduleRecovery()
        }
        watchdog = timer
        timer.resume()
    }

    private func resetCaptureMeasurements(_ reason: String) {
        runRevision += 1
        geometryRevision += 1
        geometryChanging = false
        framesEligibleAfter = ProcessInfo.processInfo.systemUptime + 0.15
        visionCadence.reset(); lastFramePTS = nil; measuredFPS = nil
        lastDiagnosticsTime = 0
        resetFrameTiming(notBefore: framesEligibleAfter)
        tracker.clear()
        onTrackingReset?(reason)
    }

    private func scheduleRecovery() {
        let previous = recovery.pendingRetry
        guard let retry = recovery.failed(at: ProcessInfo.processInfo.systemUptime) else {
            if recovery.interrupted { onCameraStatus?("Camera interrupted · waiting to resume", false) }
            else if wantsRunning { onCameraStatus?("Camera could not recover. Use Retry camera when ready.", false) }
            return
        }
        guard retry != previous else { return }
        recoveryWork?.cancel()
        onCameraStatus?("Camera reset · reconnecting…", false)
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.recovery.take(retry, at: ProcessInfo.processInfo.systemUptime) else { return }
            self.recoveryWork = nil
            self.restartCapture()
        }
        recoveryWork = work
        queue.asyncAfter(deadline: .now() + max(0, retry.at - ProcessInfo.processInfo.systemUptime), execute: work)
    }

    private func restartCapture() {
        guard wantsRunning else { return }
        watchdog?.cancel(); watchdog = nil
        if session.isInterrupted {
            recovery.beginInterruption()
            onCameraStatus?("Camera interrupted · waiting to resume", false)
            return
        }
        do {
            tracker.prepare()
            let wasConfigured = configured
            try configureIfNeeded()
            if wasConfigured { applyGeometry() }
            guard wantsRunning, geometryAvailable else { return }
            runRevision += 1
            let revision = runRevision
            recorder.start { [weak self] in
                guard let self else { return }
                self.queue.async {
                    guard self.wantsRunning, self.geometryAvailable, self.runRevision == revision else { return }
                    if !self.session.isRunning { self.session.startRunning() }
                    guard self.session.isRunning, !self.session.isInterrupted else {
                        self.scheduleRecovery()
                        return
                    }
                    self.recovery.sessionStarted(at: ProcessInfo.processInfo.systemUptime)
                    self.monitorFrames()
                    self.onCameraStatus?("Camera starting - waiting for frames", false)
                }
            }
        } catch {
            recovery.stop()
            if session.isRunning { session.stopRunning() }
            onCameraStatus?("Camera: \(error.localizedDescription). Use Retry camera when ready.", false)
        }
    }

    static func requestPermission(_ completion: @escaping (Bool) -> Void) {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized: completion(true)
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { granted in
                DispatchQueue.main.async { completion(granted) }
            }
        default: completion(false)
        }
    }

    /// Called with the window scene's orientation, never the device's reversed left/right enum.
    func setVideoOrientation(_ orientation: AVCaptureVideoOrientation) {
        queue.async {
            guard self.desiredOrientation != orientation else { return }
            self.desiredOrientation = orientation
            if self.configured { self.applyGeometry() }
            else {
                self.geometry = .preview(orientation: orientation, landscapeFOV: nil)
                self.onGeometry?(self.geometry)
            }
        }
    }

    private func applyGeometry() {
        guard let connection = output.connection(with: .video), connection.isVideoOrientationSupported else {
            geometryAvailable = false
            recovery.stop()
            watchdog?.cancel(); watchdog = nil
            recoveryWork?.cancel(); recoveryWork = nil
            recorder.pause(reason: "Camera rotation unavailable; capture paused.")
            tracker.clear()
            onTrackingReset?("Camera rotation unavailable. Tracking paused.")
            onCameraStatus?("Camera rotation unavailable", false)
            return
        }
        geometryRevision += 1
        let revision = geometryRevision
        geometryChanging = true
        // Finish the old encoder before any frame with new dimensions reaches it.
        recorder.beginOrientationSegment()
        connection.videoOrientation = desiredOrientation
        if connection.isVideoMirroringSupported { connection.isVideoMirrored = false }
        geometryAvailable = true
        geometry = .preview(orientation: desiredOrientation, landscapeFOV: nativeHorizontalFOV)
        framesEligibleAfter = ProcessInfo.processInfo.systemUptime + 0.15
        resetFrameTiming(notBefore: framesEligibleAfter)
        let applied = geometry
        tracker.configureFrameGeometry(horizontalFOV: applied.horizontalFieldOfView) { [weak self] in
            guard let self else { return }
            self.queue.async {
                guard self.geometryRevision == revision else { return }
                self.geometryChanging = false
                self.onGeometry?(applied)
            }
        }
    }

    func refreshIncidents() { queue.async { self.recorder.refresh() } }
    func applyRecordingPreferences() { queue.async { self.recorder.applyStoragePreferences() } }
    func resumeRecording() {
        queue.async {
            guard self.recovery.acceptsFrames, self.session.isRunning, !self.session.isInterrupted,
                  self.geometryAvailable, !self.geometryChanging else { return }
            self.recorder.resumeIfPaused()
        }
    }

    private func resetFrameTiming(notBefore time: TimeInterval) {
        captureTimestamp.reset(notBefore: time)
        visionCadence.reset()
        lastFramePTS = nil
        measuredFPS = nil
    }

    func start() {
        queue.async {
            self.recoveryWork?.cancel(); self.recoveryWork = nil
            self.recovery.start()
            self.resetCaptureMeasurements("Camera starting · reacquiring the lead")
            self.restartCapture()
        }
    }

    func pause(completion: @escaping () -> Void) {
        queue.async {
            self.runRevision += 1
            self.recovery.stop()
            self.watchdog?.cancel(); self.watchdog = nil
            self.recoveryWork?.cancel(); self.recoveryWork = nil
            self.tracker.clear()
            if self.session.isRunning { self.session.stopRunning() }
            self.recorder.pause(reason: "Background: camera tracking and recording are paused. Return to BrakeSense to resume.", completion: completion)
            self.onCameraStatus?("Camera paused in background", false)
        }
    }

    func stop(completion: @escaping () -> Void) {
        queue.async {
            self.runRevision += 1
            self.recovery.stop()
            self.watchdog?.cancel(); self.watchdog = nil
            self.recoveryWork?.cancel(); self.recoveryWork = nil
            self.tracker.clear()
            if self.session.isRunning { self.session.stopRunning() }
            self.recorder.stop(completion: completion)
            self.onCameraStatus?("Camera stopped", false)
        }
    }

    func markIncident(reason: String, tripID: UUID) {
        queue.async { self.recorder.markIncident(reason: reason, tripID: tripID) }
    }
    func deleteIncident(_ id: UUID) { queue.async { self.recorder.deleteIncident(id) } }

    private func configureIfNeeded() throws {
        guard !configured else { return }
        session.beginConfiguration()
        defer { session.commitConfiguration() }
        do {
            guard session.canSetSessionPreset(.hd1280x720),
                  let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) else {
                throw CameraError.unavailable
            }
            session.sessionPreset = .hd1280x720
            let input = try AVCaptureDeviceInput(device: device)
            guard session.canAddInput(input) else { throw CameraError.unavailable }
            session.addInput(input)
            output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarFullRange]
            output.alwaysDiscardsLateVideoFrames = true
            output.setSampleBufferDelegate(self, queue: queue)
            guard session.canAddOutput(output) else { throw CameraError.unavailable }
            session.addOutput(output)
            if let connection = output.connection(with: .video) {
                if connection.isVideoOrientationSupported { connection.videoOrientation = desiredOrientation }
                if connection.isVideoStabilizationSupported { connection.preferredVideoStabilizationMode = .off }
            }
            try device.lockForConfiguration()
            device.videoZoomFactor = 1
            if device.isFocusModeSupported(.continuousAutoFocus) { device.focusMode = .continuousAutoFocus }
            if device.isExposureModeSupported(.continuousAutoExposure) { device.exposureMode = .continuousAutoExposure }
            if device.isWhiteBalanceModeSupported(.continuousAutoWhiteBalance) {
                device.whiteBalanceMode = .continuousAutoWhiteBalance
            }
            if device.isLowLightBoostSupported {
                device.automaticallyEnablesLowLightBoostWhenAvailable = true
            }
            if device.activeFormat.videoSupportedFrameRateRanges.contains(where: { $0.minFrameRate <= 30 && $0.maxFrameRate >= 30 }) {
                device.activeVideoMinFrameDuration = CMTime(value: 1, timescale: 30)
                // Let auto exposure use up to 1/24 s where the format permits
                // it, rather than fixing every scene to a 1/30 s ceiling.
                let allows24 = device.activeFormat.videoSupportedFrameRateRanges.contains {
                    $0.minFrameRate <= 24 && $0.maxFrameRate >= 30
                }
                device.activeVideoMaxFrameDuration = CMTime(value: 1, timescale: allows24 ? 24 : 30)
            }
            device.unlockForConfiguration()
            captureDevice = device
            nativeHorizontalFOV = Double(device.activeFormat.videoFieldOfView)
            configured = true
            applyGeometry()
        } catch {
            geometryAvailable = false
            captureDevice = nil
            session.inputs.forEach(session.removeInput)
            session.outputs.forEach(session.removeOutput)
            throw error
        }
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        let trace = PerformanceTrace.begin(.cameraCallback)
        defer { PerformanceTrace.end(trace) }
        let uptime = ProcessInfo.processInfo.systemUptime
        PerformanceTrace.captureFrame(at: uptime)
        guard recovery.acceptsFrames, CMSampleBufferDataIsReady(sampleBuffer), !geometryChanging, uptime >= framesEligibleAfter,
              let buffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let capturedAt = captureUptime(sampleBuffer, maximumAge: 3)
        if let capturedAt { recovery.frameReceived(at: capturedAt) }
        guard !geometryChanging, uptime >= framesEligibleAfter else { return }
        let width = CVPixelBufferGetWidth(buffer), height = CVPixelBufferGetHeight(buffer)
        // Discard a queued frame from the previous orientation.
        guard (width < height) == CaptureFrameGeometry.isPortrait(desiredOrientation) else { return }
        if width != geometry.width || height != geometry.height {
            geometry = CaptureFrameGeometry(videoOrientation: desiredOrientation, width: width, height: height,
                horizontalFieldOfView: CaptureFrameGeometry.horizontalFOV(
                    landscapeDegrees: nativeHorizontalFOV, width: width, height: height))
            geometryRevision += 1
            let revision = geometryRevision
            let actual = geometry
            geometryChanging = true
            resetFrameTiming(notBefore: uptime)
            recorder.beginOrientationSegment()
            tracker.configureFrameGeometry(horizontalFOV: actual.horizontalFieldOfView) { [weak self] in
                guard let self else { return }
                self.queue.async {
                    guard self.geometryRevision == revision else { return }
                    self.geometryChanging = false
                    self.onGeometry?(actual)
                }
            }
            return
        }
        if let capturedAt, let date = CaptureFrameTiming.captureDate(capturedAt: capturedAt,
            uptimeNow: ProcessInfo.processInfo.systemUptime, wallNow: Date()) {
            recorder.append(sampleBuffer, capturedAt: date)
        }
        reportCaptureHealth(sampleBuffer, buffer: buffer, at: uptime)
        guard let capturedAt, capturedAt >= framesEligibleAfter else { return }
        var cadence = visionCadence
        guard cadence.shouldRun(at: capturedAt, interval: visionInterval) else { return }
        // A busy worker did not consume this time slot. Offer the next fresh
        // capture instead of adding another full interval after it becomes free.
        if tracker.submit(buffer, uptime: capturedAt, captureISO: captureDevice.map { Double($0.iso) },
            exposureSeconds: captureDevice.map { CMTimeGetSeconds($0.exposureDuration) }) {
            visionCadence = cadence
        }
    }

    private func captureUptime(_ sample: CMSampleBuffer, maximumAge: TimeInterval) -> TimeInterval? {
        let pts = CMSampleBufferGetPresentationTimeStamp(sample)
        guard pts.isNumeric, let clock = session.synchronizationClock else { return nil }
        let hostClock = CMClockGetHostTimeClock()
        let captured = CMSyncConvertTime(pts, from: clock, to: hostClock)
        let receivedAt = ProcessInfo.processInfo.systemUptime
        let hostNow = CMTimeGetSeconds(CMClockGetTime(hostClock))
        guard captured.isNumeric,
              let capturedAt = captureTimestamp.uptime(hostTimestamp: CMTimeGetSeconds(captured),
                  hostNow: hostNow, receivedAt: receivedAt),
              receivedAt - capturedAt <= maximumAge else { return nil }
        return capturedAt
    }

    private var visionInterval: TimeInterval {
        switch ProcessInfo.processInfo.thermalState {
        case .nominal: return 0.10
        case .fair: return 0.125
        case .serious: return 0.20
        case .critical: return 0.25
        @unknown default: return 0.20
        }
    }

    private func reportCaptureHealth(_ sample: CMSampleBuffer, buffer: CVPixelBuffer, at time: TimeInterval) {
        let pts = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sample))
        if pts.isFinite {
            if let previous = lastFramePTS, (0.01...0.2).contains(pts - previous) {
                let fps = 1 / (pts - previous)
                measuredFPS = measuredFPS.map { 0.8 * $0 + 0.2 * fps } ?? fps
            } else { measuredFPS = nil }
            lastFramePTS = pts
        }
        guard time - lastDiagnosticsTime >= 2, let device = captureDevice else { return }
        lastDiagnosticsTime = time
        let fps = measuredFPS.map { String(format: "%.0f fps", $0) } ?? "fps sampling"
        let boost = device.isLowLightBoostSupported
            ? device.isLowLightBoostEnabled ? "boost active" : "auto boost ready"
            : "boost unsupported"
        let luma = sceneLuma(buffer).map { String(format: " · scene luma %.0f%%", $0 * 100) } ?? ""
        let pacing = String(format: " · vision ≤%.0f fps · heat %@", 1 / visionInterval,
            String(describing: ProcessInfo.processInfo.thermalState))
        onCameraStatus?(String(format: "N7 · rear 720p · %@ · exposure %.1f ms · ISO %.0f · %@%@%@",
            fps, CMTimeGetSeconds(device.exposureDuration) * 1000, Double(device.iso), boost, luma, pacing), true)
    }

    /// Sparse luma samples describe the scene; they do not classify vehicles,
    /// lower detection thresholds, or count as direction evidence.
    private func sceneLuma(_ buffer: CVPixelBuffer) -> Double? {
        guard CVPixelBufferGetPlaneCount(buffer) == 2,
              CVPixelBufferLockBaseAddress(buffer, .readOnly) == kCVReturnSuccess else { return nil }
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddressOfPlane(buffer, 0) else { return nil }
        let width = CVPixelBufferGetWidthOfPlane(buffer, 0)
        let height = CVPixelBufferGetHeightOfPlane(buffer, 0)
        let stride = CVPixelBufferGetBytesPerRowOfPlane(buffer, 0)
        let bytes = base.assumingMemoryBound(to: UInt8.self)
        var sum = 0, count = 0
        for y in Swift.stride(from: height / 5, to: height * 4 / 5, by: max(1, height / 24)) {
            for x in Swift.stride(from: width / 8, to: width * 7 / 8, by: max(1, width / 32)) {
                sum += Int(bytes[y * stride + x]); count += 1
            }
        }
        return count > 0 ? Double(sum) / Double(count * 255) : nil
    }

    enum CameraError: LocalizedError {
        case unavailable
        var errorDescription: String? { "A physical iPhone rear camera is required. Use Demo in Simulator." }
    }
}
