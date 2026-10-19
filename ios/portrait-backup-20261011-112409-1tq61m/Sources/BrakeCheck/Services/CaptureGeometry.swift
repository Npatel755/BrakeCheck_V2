import AVFoundation
import UIKit

/// Geometry of the actual upright buffer, independent of the shape of the HUD.
struct CaptureFrameGeometry: Equatable {
    let videoOrientation: AVCaptureVideoOrientation
    let width: Int
    let height: Int
    let horizontalFieldOfView: Double?

    var aspect: Double { Double(width) / Double(max(1, height)) }
    var isPortrait: Bool { width < height }

    static let standby = CaptureFrameGeometry(videoOrientation: .portrait,
        width: 720, height: 1280, horizontalFieldOfView: nil)

    static func videoOrientation(for orientation: UIInterfaceOrientation) -> AVCaptureVideoOrientation? {
        // These are interface orientations, not UIDeviceOrientation. The
        // device's left/right naming is reversed relative to the interface.
        switch orientation {
        case .portrait: return .portrait
        case .portraitUpsideDown: return .portraitUpsideDown
        case .landscapeLeft: return .landscapeLeft
        case .landscapeRight: return .landscapeRight
        default: return nil
        }
    }

    static func isPortrait(_ orientation: AVCaptureVideoOrientation) -> Bool {
        orientation == .portrait || orientation == .portraitUpsideDown
    }

    /// videoFieldOfView describes the native landscape horizontal axis.
    /// In portrait the buffer's horizontal axis is the sensor's short axis.
    /// Use the actual buffer ratio rather than assuming the screen is 16:9.
    static func horizontalFOV(landscapeDegrees: Double?, width: Int, height: Int) -> Double? {
        guard let degrees = landscapeDegrees, degrees.isFinite,
              degrees > 0, degrees < 180, width > 0, height > 0 else { return nil }
        if width >= height { return degrees }
        let ratio = Double(width) / Double(height)
        let result = 2 * atan(tan(degrees * .pi / 360) * ratio) * 180 / .pi
        return result.isFinite && result > 0 ? result : nil
    }

    static func preview(orientation: AVCaptureVideoOrientation, landscapeFOV: Double?) -> CaptureFrameGeometry {
        let portrait = isPortrait(orientation)
        let width = portrait ? 720 : 1280
        let height = portrait ? 1280 : 720
        return CaptureFrameGeometry(videoOrientation: orientation, width: width, height: height,
            horizontalFieldOfView: horizontalFOV(landscapeDegrees: landscapeFOV, width: width, height: height))
    }
}
