import Foundation

/// A color observation within an independently detected signal housing.
/// This is not a traffic-light detector or a determination of right of way.
public enum TrafficSignalPixels {
    public static func color(rgba: [UInt8], width: Int, height: Int) -> TrafficSignalColor {
        guard width >= 8, height >= 16, width <= 128, height <= 128,
              rgba.count == width * height * 4 else { return .unknown }
        var red = 0, green = 0
        for index in stride(from: 0, to: rgba.count, by: 4) {
            let r = Double(rgba[index]), g = Double(rgba[index + 1]), b = Double(rgba[index + 2])
            guard max(r, g, b) >= 100, max(r, g, b) - min(r, g, b) >= 55 else { continue }
            if r > 1.7 * g, r > 1.6 * b { red += 1 }
            if g > 1.4 * r, g > 1.25 * b { green += 1 }
        }
        let minimum = max(8, width * height / 70)
        if red >= minimum, red > 4 * green { return .red }
        if green >= minimum, green > 4 * red { return .green }
        return .unknown
    }
}
