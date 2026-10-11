import BrakeCheckCore
import CoreGraphics
import CoreImage
import CoreVideo
import Foundation

/// Queue-confined renderer. One reusable 640-pixel buffer pool; the crop is
/// letterboxed with the detector's training padding, then mapped back in Core.
final class VehicleRecoveryRenderer {
    private let context = CIContext(options: [.cacheIntermediates: false])
    private var pool: CVPixelBufferPool?
    func render(_ buffer: CVPixelBuffer, crop: VehicleSearchCrop) throws -> CVPixelBuffer {
        if pool == nil {
            let attributes: [String: Any] = [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                kCVPixelBufferWidthKey as String: 640, kCVPixelBufferHeightKey as String: 640,
                kCVPixelBufferIOSurfacePropertiesKey as String: [:]]
            guard CVPixelBufferPoolCreate(kCFAllocatorDefault, nil, attributes as CFDictionary, &pool) == kCVReturnSuccess
            else { throw RenderError.buffer }
        }
        guard let pool else { throw RenderError.buffer }
        var output: CVPixelBuffer?
        guard CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, pool, &output) == kCVReturnSuccess,
              let output else { throw RenderError.buffer }
        let image = CIImage(cvPixelBuffer: buffer), r = crop.region
        let source = CGRect(x: image.extent.minX + r.minX * image.extent.width,
                            y: image.extent.minY + r.minY * image.extent.height,
                            width: r.width * image.extent.width, height: r.height * image.extent.height)
        let c = crop.content
        let pixels = image.cropped(to: source)
            .transformed(by: CGAffineTransform(translationX: -source.minX, y: -source.minY))
            .transformed(by: CGAffineTransform(scaleX: c.width * 640 / source.width, y: c.height * 640 / source.height))
            .transformed(by: CGAffineTransform(translationX: c.minX * 640, y: c.minY * 640))
        let bounds = CGRect(x: 0, y: 0, width: 640, height: 640)
        let background = CIImage(color: CIColor(red: 114.0 / 255, green: 114.0 / 255, blue: 114.0 / 255)).cropped(to: bounds)
        context.render(pixels.composited(over: background).cropped(to: bounds), to: output,
                       bounds: bounds, colorSpace: CGColorSpaceCreateDeviceRGB())
        return output
    }
    private enum RenderError: Error { case buffer }
}
