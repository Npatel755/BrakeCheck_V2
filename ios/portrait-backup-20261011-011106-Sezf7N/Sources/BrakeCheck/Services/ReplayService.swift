import AVFoundation
import CoreImage
import UIKit

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
            guard let track = asset.tracks(withMediaType: .video).first else { continue }
            let reader: AVAssetReader
            do { reader = try AVAssetReader(asset: asset) } catch {
                onFinished?("Could not open \(clip.url.lastPathComponent): \(error.localizedDescription)", current)
                return
            }
            let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
            ])
            output.alwaysCopiesSampleData = false
            guard reader.canAdd(output) else { continue }
            reader.add(output)
            guard reader.startReading() else { continue }
            let clipDate = clip.start ?? asset.creationDate?.dateValue
            let clipWallStart = ProcessInfo.processInfo.systemUptime
            var firstPTS: CMTime?
            var clipLength: TimeInterval = 0
            while isCurrent(current), let sample = output.copyNextSampleBuffer() {
                let pts = CMSampleBufferGetPresentationTimeStamp(sample)
                guard pts.isValid else { continue }
                if firstPTS == nil { firstPTS = pts }
                let offset = max(0, CMTimeGetSeconds(CMTimeSubtract(pts, firstPTS ?? pts)))
                clipLength = offset
                let wait = clipWallStart + offset - ProcessInfo.processInfo.systemUptime
                if wait > 0 { Thread.sleep(forTimeInterval: wait) } else if wait < -0.25 { continue }
                guard isCurrent(current), let buffer = CMSampleBufferGetImageBuffer(sample) else { continue }
                let uptime = ProcessInfo.processInfo.systemUptime
                onTime?(uptime, clipDate?.addingTimeInterval(offset), elapsedBeforeClip + offset, current)
                if uptime - lastPreview >= 0.066 {
                    lastPreview = uptime
                    if let image = makeImage(buffer) { onFrame?(image, current); framesShown += 1 }
                }
                // Same 10 Hz detector cadence as CameraService.captureOutput.
                if uptime - lastSubmit >= 0.1 {
                    lastSubmit = uptime
                    tracker.submit(buffer, uptime: uptime)
                }
            }
            reader.cancelReading()
            elapsedBeforeClip += clipLength
        }
        guard isCurrent(current) else { return }
        let seconds = ProcessInfo.processInfo.systemUptime - replayStart
        onFinished?(framesShown == 0 ? "Replay found no readable video frames." : String(format: "Replay finished · %.0f s", seconds), current)
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
