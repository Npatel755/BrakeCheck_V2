import BrakeCheckCore
import AVFoundation
import Foundation
import UIKit

final class CameraService: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let session = AVCaptureSession()
    let tracker = VehicleTracker()
    private let queue = DispatchQueue(label: "BrakeCheck.capture", qos: .userInitiated)
    private var output = AVCaptureVideoDataOutput()
    private var configured = false
    private var wantsRunning = false
    private var lastVisionTime: TimeInterval = 0
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
    private lazy var recorder: SegmentRecorder = {
        let result = SegmentRecorder(queue: queue)
        result.onStatus = { [weak self] message in self?.onRecorderStatus?(message) }
        result.onIncidents = { [weak self] events in self?.onIncidents?(events) }
        return result
    }()
    var onCameraStatus: ((String, Bool) -> Void)?
    var onRecorderStatus: ((String) -> Void)?
    var onIncidents: (([IncidentManifest]) -> Void)?
    var onTrackingReset: ((String) -> Void)?
    var onGeometry: ((CaptureFrameGeometry) -> Void)?

    override init() {
        super.init()
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: AVCaptureSession.wasInterruptedNotification,
                                             object: session, queue: nil) { [weak self] _ in
            guard let self else { return }
            self.queue.async {
                self.tracker.clear()
                self.recorder.pause(reason: "Camera interrupted; video has a gap.")
                self.onTrackingReset?("Camera interrupted. Recalibrate when it returns.")
                self.onCameraStatus?("Camera interrupted", false)
            }
        })
        observers.append(center.addObserver(forName: AVCaptureSession.interruptionEndedNotification,
                                             object: session, queue: nil) { [weak self] _ in
            guard let self else { return }
            self.queue.async {
                guard self.wantsRunning else { return }
                if !self.session.isRunning { self.session.startRunning() }
                self.recorder.start()
                self.onCameraStatus?("Camera active · reselect target", self.session.isRunning)
            }
        })
        observers.append(center.addObserver(forName: AVCaptureSession.runtimeErrorNotification,
                                             object: session, queue: nil) { [weak self] note in
            guard let self else { return }
            let description = (note.userInfo?[AVCaptureSessionErrorKey] as? NSError)?.localizedDescription ?? "Unknown camera error"
            self.queue.async {
                self.tracker.clear()
                self.recorder.pause(reason: "Camera error; recording interrupted.")
                self.onTrackingReset?("Camera error. End and restart the trip.")
                self.onCameraStatus?("Camera: \(description)", false)
            }
        })
    }

    deinit { observers.forEach(NotificationCenter.default.removeObserver) }

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
            wantsRunning = false
            recorder.pause(reason: "Camera rotation unavailable; capture paused.")
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
        geometry = .preview(orientation: desiredOrientation, landscapeFOV: nativeHorizontalFOV)
        lastVisionTime = 0
        framesEligibleAfter = ProcessInfo.processInfo.systemUptime + 0.15
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

    func start() {
        queue.async {
            self.wantsRunning = true
            do {
                try self.configureIfNeeded()
                guard self.wantsRunning else { return }
                self.recorder.start()
                if !self.session.isRunning { self.session.startRunning() }
                self.onCameraStatus?(self.session.isRunning ? "Rear camera · 720p" : "Camera could not start", self.session.isRunning)
            } catch {
                self.onCameraStatus?("Camera: \(error.localizedDescription)", false)
            }
        }
    }

    func pause(completion: @escaping () -> Void) {
        queue.async {
            self.wantsRunning = false
            self.tracker.clear()
            if self.session.isRunning { self.session.stopRunning() }
            self.recorder.pause(reason: "Background: GPS continues; camera and recording are paused.", completion: completion)
            self.onCameraStatus?("Camera paused in background", false)
        }
    }

    func stop(completion: @escaping () -> Void) {
        queue.async {
            self.wantsRunning = false
            self.tracker.clear()
            if self.session.isRunning { self.session.stopRunning() }
            self.recorder.stop(completion: completion)
            self.onCameraStatus?("Camera stopped", false)
        }
    }

    func markIncident(reason: String) { queue.async { self.recorder.markIncident(reason: reason) } }
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
            captureDevice = nil
            session.inputs.forEach(session.removeInput)
            session.outputs.forEach(session.removeOutput)
            throw error
        }
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        let uptime = ProcessInfo.processInfo.systemUptime
        guard wantsRunning, !geometryChanging, uptime >= framesEligibleAfter,
              let buffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
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
        recorder.append(sampleBuffer)
        reportCaptureHealth(sampleBuffer, buffer: buffer, at: uptime)
        guard uptime - lastVisionTime >= 0.1 else { return }
        lastVisionTime = uptime
        tracker.submit(buffer, uptime: uptime, captureISO: captureDevice.map { Double($0.iso) },
            exposureSeconds: captureDevice.map { CMTimeGetSeconds($0.exposureDuration) })
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
        onCameraStatus?(String(format: "N5 · rear 720p · %@ · exposure %.1f ms · ISO %.0f · %@%@",
            fps, CMTimeGetSeconds(device.exposureDuration) * 1000, Double(device.iso), boost, luma), true)
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
