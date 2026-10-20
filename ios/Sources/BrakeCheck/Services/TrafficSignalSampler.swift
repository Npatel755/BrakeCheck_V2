import BrakeCheckCore
import CoreImage
import CoreVideo
import Vision

/// Reuses the existing full-frame detector; never starts another model job.
/// Multiple plausible signals, tiny crops and horizontal heads are rejected.
/// Color alone cannot distinguish arrows or determine the applicable lane.
/// Notifications describe a change, not permission to drive.
final class TrafficSignalSampler {
    private let context = CIContext(options: [.cacheIntermediates: false])
    func observe(_ objects: [VNRecognizedObjectObservation], buffer: CVPixelBuffer,
                 capturedAt: TimeInterval) -> TrafficSignalObservation? {
        let width = CVPixelBufferGetWidth(buffer), height = CVPixelBufferGetHeight(buffer)
        let candidates = objects.filter { object in
            guard let label = object.labels.first, label.identifier.lowercased() == "traffic light",
                  label.confidence >= 0.75 else { return false }
            let box = object.boundingBox
            return LeadVehicle.validBox(box) && (0.32...0.68).contains(box.midX)
                && box.midY >= 0.45 && box.height * Double(height) >= 24
                && box.width * Double(width) >= 10
                && box.height * Double(height) >= 1.5 * box.width * Double(width)
        }
        guard candidates.count == 1, let box = candidates.first?.boundingBox else { return nil }
        let crop = CGRect(x: box.minX * Double(width), y: box.minY * Double(height),
                          width: box.width * Double(width), height: box.height * Double(height))
        let image = CIImage(cvPixelBuffer: buffer).cropped(to: crop)
            .transformed(by: CGAffineTransform(translationX: -crop.minX, y: -crop.minY))
            .transformed(by: CGAffineTransform(scaleX: 24 / crop.width, y: 48 / crop.height))
        var pixels = [UInt8](repeating: 0, count: 24 * 48 * 4)
        pixels.withUnsafeMutableBytes { bytes in
            if let address = bytes.baseAddress {
                context.render(image, toBitmap: address, rowBytes: 24 * 4,
                    bounds: CGRect(x: 0, y: 0, width: 24, height: 48), format: .RGBA8,
                    colorSpace: CGColorSpaceCreateDeviceRGB())
            }
        }
        return TrafficSignalObservation(box: box,
            color: TrafficSignalPixels.color(rgba: pixels, width: 24, height: 48), capturedAt: capturedAt)
    }
}
