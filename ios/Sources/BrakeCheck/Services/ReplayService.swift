import AVFoundation
import CoreImage
import UIKit
import CoreVideo

/// Plays a recorded drive through the same VehicleTracker the live camera uses, paced in real time,
/// so the danger meter, voice alert and overlays behave exactly as on the road.
/// Callbacks arrive on a background queue.
final class ReplayService {
    struct Clip {
        let url: URL
        /// Wall-clock time of the clip's first frame, when known (BrakeCheck recordings know it).
        let start: Date?
    }

    private let queue = DispatchQueue(label: "BrakeCheck.replay", qos: .userInitiated)
    private let lock = NSLock()
    private var generation = 0
    private let ciContext = CIContext(options: [.cacheIntermediates: false])

    /// Preview image for the dashboard (about 15 per second), with its playback generation.
    /// The generation prevents queued callbacks from an old replay replacing a newer one.
    var onFrame: ((UIImage, Int) -> Void)?
    /// Each submitted frame: (uptime it was submitted, wall-clock time it was recorded if known, seconds since replay start).
    var onTime: ((TimeInterval, Date?, TimeInterval, Int) -> Void)?
    var onFinished: ((String, Int) -> Void)?
    var onGeometry: ((TimeInterval, Int) -> Void)?

    @discardableResult
    func play(_ clips: [Clip], tracker: VehicleTracker) -> Int {
        let current = bump()
        queue.async { [weak self] in self?.run(clips, tracker: tracker, generation: current) }
        return current
    }

    func stop() { _ = bump() }

    private func bump() -> Int {
        lock.lock(); defer { lock.unlock() }
        generation += 1
        return generation
    }

    private func isCurrent(_ value: Int) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return value == generation
    }

    private func run(_ clips: [Clip], tracker: VehicleTracker, generation current: Int) {
        let replayStart = ProcessInfo.processInfo.systemUptime
        var elapsedBeforeClip: TimeInterval = 0
        var lastPreview: TimeInterval = 0
        var lastSubmit: TimeInterval = 0
        var framesShown = 0
        for clip in clips {
            guard isCurrent(current) else { return }
            let asset = AVURLAsset(url: clip.url)
            guard let track = asset.tracks(withMediaType: .video).first else {
                onFinished?("No video track in \(clip.url.lastPathComponent). Choose a video file with readable picture frames.", current)
                return
            }
            let reader: AVAssetReader
            do { reader = try AVAssetReader(asset: asset) } catch {
                onFinished?("Could not open \(clip.url.lastPathComponent): \(error.localizedDescription)", current)
                return
            }
            let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
            ])
            output.alwaysCopiesSampleData = false
            guard reader.canAdd(output) else {
                onFinished?("Could not decode \(clip.url.lastPathComponent). This video format is not supported by the iPhone.", current)
                return
            }
            reader.add(output)
            guard reader.startReading() else {
                let detail = reader.error?.localizedDescription ?? "The file could not be read."
                onFinished?("Could not open \(clip.url.lastPathComponent): \(detail)", current)
                return
            }
            let clipDate = clip.start ?? asset.creationDate?.dateValue
            let clipWallStart = ProcessInfo.processInfo.systemUptime
            var firstPTS: CMTime?
            var clipLength: TimeInterval = 0
            var geometryConfigured = false
            var decodedFrames = 0
            let displayTransform = track.preferredTransform
            while isCurrent(current), let sample = output.copyNextSampleBuffer() {
                let pts = CMSampleBufferGetPresentationTimeStamp(sample)
                guard pts.isValid else { continue }
                if firstPTS == nil { firstPTS = pts }
                let offset = max(0, CMTimeGetSeconds(CMTimeSubtract(pts, firstPTS ?? pts)))
                clipLength = offset
                let wait = clipWallStart + offset - ProcessInfo.processInfo.systemUptime
                if wait < -0.25 { continue }
                // A stop or new replay can cancel even a clip with a timestamp gap.
                while isCurrent(current) {
                    let remaining = clipWallStart + offset - ProcessInfo.processInfo.systemUptime
                    if remaining <= 0 { break }
                    Thread.sleep(forTimeInterval: min(0.02, remaining))
                }
                guard isCurrent(current), let buffer = CMSampleBufferGetImageBuffer(sample) else { continue }
                let uptime = ProcessInfo.processInfo.systemUptime
                onTime?(uptime, clipDate?.addingTimeInterval(offset), elapsedBeforeClip + offset, current)
                let previewDue = decodedFrames == 0 || uptime - lastPreview >= 0.066
                let detectionDue = decodedFrames == 0 || uptime - lastSubmit >= 0.1
                guard previewDue || detectionDue,
                      let upright = uprightBuffer(buffer, transform: displayTransform) else { continue }
                decodedFrames += 1
                if !geometryConfigured {
                    let width = CVPixelBufferGetWidth(upright), height = CVPixelBufferGetHeight(upright)
                    let fov = CaptureFrameGeometry.horizontalFOV(landscapeDegrees: Self.recordingFieldOfView(),
                                                               width: width, height: height)
                    tracker.configureFrameGeometry(horizontalFOV: fov)
                    onGeometry?(uptime, current)
                    geometryConfigured = true
                }
                if previewDue {
                    lastPreview = uptime
                    if let image = makeImage(upright) { onFrame?(image, current); framesShown += 1 }
                }
                // Same 10 Hz detector cadence as CameraService.captureOutput.
                if detectionDue {
                    lastSubmit = uptime
                    tracker.submit(upright, uptime: uptime)
                }
            }
            guard isCurrent(current) else { reader.cancelReading(); return }
            let readError = reader.status == .failed ? reader.error?.localizedDescription ?? "The video reader failed." : nil
            reader.cancelReading()
            if let readError {
                onFinished?("Could not finish \(clip.url.lastPathComponent): \(readError)", current)
                return
            }
            guard decodedFrames > 0 else {
                onFinished?("No readable picture frames in \(clip.url.lastPathComponent). Try an H.264 or HEVC video.", current)
                return
            }
            elapsedBeforeClip += clipLength
        }
        guard isCurrent(current) else { return }
        let seconds = ProcessInfo.processInfo.systemUptime - replayStart
        onFinished?(framesShown == 0 ? "Replay found no readable video frames." : String(format: "Replay finished · %.0f s", seconds), current)
    }

    /// Honor a movie's display transform before both rendering and inference.
    /// BrakeCheck's physically upright recordings use the identity fast path.
    private func uprightBuffer(_ buffer: CVPixelBuffer, transform: CGAffineTransform) -> CVPixelBuffer? {
        guard !transform.isIdentity else { return buffer }
        let image = CIImage(cvPixelBuffer: buffer).transformed(by: transform)
        let extent = image.extent
        guard extent.width.isFinite, extent.height.isFinite,
              extent.width > 0, extent.height > 0, extent.width <= 8192, extent.height <= 8192 else { return nil }
        let width = Int(extent.width.rounded()), height = Int(extent.height.rounded())
        var result: CVPixelBuffer?
        let attributes: [String: Any] = [kCVPixelBufferIOSurfacePropertiesKey as String: [:]]
        guard CVPixelBufferCreate(kCFAllocatorDefault, width, height, kCVPixelFormatType_32BGRA,
                                  attributes as CFDictionary, &result) == kCVReturnSuccess,
              let result else { return nil }
        let normalized = image.transformed(by: CGAffineTransform(translationX: -extent.minX, y: -extent.minY))
        ciContext.render(normalized, to: result)
        return result
    }

    private func makeImage(_ buffer: CVPixelBuffer) -> UIImage? {
        let image = CIImage(cvPixelBuffer: buffer)
        let scale = min(1, 960 / max(1, image.extent.width))
        let scaled = image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        guard let cgImage = ciContext.createCGImage(scaled, from: scaled.extent) else { return nil }
        return UIImage(cgImage: cgImage)
    }

    /// Horizontal field of view of the rear wide camera at the 720p format BrakeCheck records with.
    /// Range estimates need it; the live camera sets it when it starts, which replay may never do.
    static func recordingFieldOfView() -> Double? {
        guard let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) else { return nil }
        let match = device.formats.first { format in
            let size = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
            return size.width == 1280 && size.height == 720
        }
        let fov = Double((match ?? device.activeFormat).videoFieldOfView)
        return fov > 0 ? fov : nil
    }
}
