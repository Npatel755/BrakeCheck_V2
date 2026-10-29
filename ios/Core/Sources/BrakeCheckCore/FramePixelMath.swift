import Foundation

/// The lane observer consumes full-range luminance/chroma, whether capture
/// supplies YUV or replay supplies RGB. Integer conversion avoids per-pixel
/// allocations and applies the same 601 coefficients to both RGB layouts.
public enum FramePixelMath {
    public static func fullRangeLuma(_ video: UInt8) -> UInt8 {
        clamped((Int(video) - 16) * 255 / 219)
    }
    public static func fullRangeChroma(_ video: UInt8) -> UInt8 {
        clamped(128 + (Int(video) - 128) * 255 / 224)
    }
    // Typed intermediates: Xcode's type checker times out on the one-line integer sums.
    public static func luma(red: UInt8, green: UInt8, blue: UInt8) -> UInt8 {
        let r: Int = 77 * Int(red), g: Int = 150 * Int(green), b: Int = 29 * Int(blue)
        return UInt8((r + g + b + 128) >> 8)
    }
    public static func chroma(red: UInt8, green: UInt8, blue: UInt8) -> (UInt8, UInt8) {
        let r = Int(red), g = Int(green), b = Int(blue)
        let cb: Int = (-43 * r - 85 * g + 128 * b + 128) >> 8
        let cr: Int = (128 * r - 107 * g - 21 * b + 128) >> 8
        return (clamped(128 + cb), clamped(128 + cr))
    }
    private static func clamped(_ value: Int) -> UInt8 { UInt8(max(0, min(255, value))) }
}
