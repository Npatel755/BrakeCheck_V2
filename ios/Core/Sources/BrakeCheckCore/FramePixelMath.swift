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
    public static func luma(red: UInt8, green: UInt8, blue: UInt8) -> UInt8 {
        UInt8((77 * Int(red) + 150 * Int(green) + 29 * Int(blue) + 128) >> 8)
    }
    public static func chroma(red: UInt8, green: UInt8, blue: UInt8) -> (UInt8, UInt8) {
        (clamped(128 + ((-43 * Int(red) - 85 * Int(green) + 128 * Int(blue) + 128) >> 8)),
         clamped(128 + ((128 * Int(red) - 107 * Int(green) - 21 * Int(blue) + 128) >> 8)))
    }
    private static func clamped(_ value: Int) -> UInt8 { UInt8(max(0, min(255, value))) }
}
