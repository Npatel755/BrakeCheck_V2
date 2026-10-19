import BrakeCheckCore
import AVFoundation
import Foundation

final class CameraService: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let session = AVCaptureSession()
    let tracker = VehicleTracker()
    private let queue = DispatchQueue(label: "BrakeCheck.capture", qos: .userInitiated)
    private var output = AVCaptureVideoDataOutput()
    private var configured = false
    private var wantsRunning = false
    private var lastVisionTime: TimeInterval = 0
    private var observers: [NSObjectProtocol] = []
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

    func refreshIncidents() { queue.async { self.recorder.refresh() } }

    func start() {
        queue.async {
            self.wantsRunning = true
            do {
                try self.configureIfNeeded()
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
                if connection.isVideoOrientationSupported { connection.videoOrientation = .landscapeRight }
                if connection.isVideoStabilizationSupported { connection.preferredVideoStabilizationMode = .off }
            }
            try device.lockForConfiguration()
            device.videoZoomFactor = 1
            if device.isFocusModeSupported(.continuousAutoFocus) { device.focusMode = .continuousAutoFocus }
            if device.isExposureModeSupported(.continuousAutoExposure) { device.exposureMode = .continuousAutoExposure }
            if device.activeFormat.videoSupportedFrameRateRanges.contains(where: { $0.minFrameRate <= 30 && $0.maxFrameRate >= 30 }) {
                device.activeVideoMinFrameDuration = CMTime(value: 1, timescale: 30)
                device.activeVideoMaxFrameDuration = CMTime(value: 1, timescale: 30)
            }
            device.unlockForConfiguration()
            tracker.setHorizontalFieldOfView(Double(device.activeFormat.videoFieldOfView))
            configured = true
        } catch {
            session.inputs.forEach(session.removeInput)
            session.outputs.forEach(session.removeOutput)
            throw error
        }
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        guard wantsRunning else { return }
        recorder.append(sampleBuffer)
        let uptime = ProcessInfo.processInfo.systemUptime
        guard uptime - lastVisionTime >= 0.1,
              let buffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        lastVisionTime = uptime
        tracker.submit(buffer, uptime: uptime)
    }

    enum CameraError: LocalizedError {
        case unavailable
        var errorDescription: String? { "A physical iPhone rear camera is required. Use Demo in Simulator." }
    }
}
