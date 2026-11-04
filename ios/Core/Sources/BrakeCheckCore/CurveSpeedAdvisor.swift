import Foundation

public struct CurveSpeedWarning: Equatable {
    public enum Severity: Equatable { case caution, sharp }
    public let severity: Severity
    /// Metres from the vehicle to the tightest point of the curve.
    public let distance: Double
    public let radius: Double
    /// Speed (m/s) at which the curve stays within the comfortable lateral acceleration.
    public let advisedSpeed: Double
    public let currentSpeed: Double
    public let text: String
    public let spoken: String
    /// True exactly once per curve, when the warning first becomes stable.
    public let shouldSpeak: Bool
    public let capturedAt: TimeInterval
    public var symbol: String { severity == .sharp ? "exclamationmark.triangle.fill" : "arrow.triangle.turn.up.right.diamond" }
}

/// Curve-speed warning from the active route's geometry and GPS speed. The route
/// is resampled once; each GPS fix scans only the road the vehicle reaches in the
/// next few seconds. Intersection turns (very small radii) belong to the turn
/// prompt and are ignored here. This is an advisory, never vehicle control.
public struct CurveSpeedAdvisor {
    public static let sampleSpacing = 10.0
    public static let comfortableLateralAcceleration = 2.5
    public static let warningLateralAcceleration = 3.5
    public static let sharpLateralAcceleration = 5.0
    public static let minimumSpeed = 8.0
    public static let radiusRange = 25.0...400.0
    public static let requiredObservations = 2

    private struct Sample { let distance: Double; let headingDelta: Double; let radius: Double }
    private var samples: [Sample] = []
    private(set) public var routeLength = 0.0
    private var proposalKey: Int?
    private var proposalCount = 0
    private var spoken: Set<Int> = []
    private var lastUpdate: TimeInterval?
    public init() {}

    public mutating func setRoute(_ route: [NavigationCoordinate]) {
        samples = []; routeLength = 0; proposalKey = nil; proposalCount = 0; spoken = []; lastUpdate = nil
        guard route.count >= 3, route.allSatisfy(\.isValid), let origin = route.first else { return }
        let scale = cos(origin.latitude * .pi / 180)
        let points = route.map { (east: ($0.longitude - origin.longitude) * 111_320 * scale,
                                  north: ($0.latitude - origin.latitude) * 111_320) }
        // Resample at a fixed spacing so curvature is independent of vertex density.
        var resampled: [(east: Double, north: Double)] = [points[0]]
        var carried = 0.0
        for (a, b) in zip(points, points.dropFirst()) {
            let length = hypot(b.east - a.east, b.north - a.north)
            guard length > 0 else { continue }
            var position = Self.sampleSpacing - carried
            while position <= length {
                let t = position / length
                resampled.append((a.east + (b.east - a.east) * t, a.north + (b.north - a.north) * t))
                position += Self.sampleSpacing
            }
            carried = length - (position - Self.sampleSpacing)
            routeLength += length
        }
        guard resampled.count >= 4 else { samples = []; return }
        // Heading at each sample from its neighbours; the heading change per
        // sample spacing is the local curvature.
        let headings = (1..<(resampled.count - 1)).map { index in
            atan2(resampled[index + 1].east - resampled[index - 1].east,
                  resampled[index + 1].north - resampled[index - 1].north)
        }
        for index in 1..<headings.count {
            let delta = Self.wrapped(headings[index] - headings[index - 1])
            let radius = abs(delta) > 1e-9 ? Self.sampleSpacing / abs(delta) : Double.infinity
            samples.append(Sample(distance: Double(index + 1) * Self.sampleSpacing, headingDelta: delta, radius: radius))
        }
    }

    static func wrapped(_ angle: Double) -> Double {
        var value = angle.truncatingRemainder(dividingBy: 2 * .pi)
        if value > .pi { value -= 2 * .pi }
        if value < -.pi { value += 2 * .pi }
        return value
    }

    /// The contiguous curved stretch containing `index`, summarised as one bend:
    /// its start, length and effective radius (length / total heading change).
    /// An intersection corner turns 90° within a few samples and therefore has
    /// a small effective radius even where its edge samples look gentle.
    private func bend(containing index: Int) -> (startIndex: Int, startDistance: Double, length: Double, radius: Double)? {
        var first = index, last = index
        while first > 0, samples[first - 1].radius <= Self.radiusRange.upperBound,
              samples[first - 1].headingDelta.sign == samples[index].headingDelta.sign { first -= 1 }
        while last < samples.count - 1, samples[last + 1].radius <= Self.radiusRange.upperBound,
              samples[last + 1].headingDelta.sign == samples[index].headingDelta.sign { last += 1 }
        let turn = abs(samples[first...last].reduce(0) { $0 + $1.headingDelta })
        // Neighbour-based headings smear a bend over one extra sample at each end.
        let length = max(Self.sampleSpacing, Double(last - first - 1) * Self.sampleSpacing)
        guard turn > 0.05 else { return nil }
        return (first, samples[first].distance, length, length / turn)
    }

    public mutating func update(progressFraction: Double?, speed: Double?, navigating: Bool,
                                now: TimeInterval) -> CurveSpeedWarning? {
        guard navigating, now.isFinite, !samples.isEmpty, routeLength > 0,
              let progressFraction, progressFraction.isFinite, (0...1).contains(progressFraction),
              let speed, speed.isFinite, speed >= Self.minimumSpeed else {
            proposalKey = nil; proposalCount = 0; return nil
        }
        if let last = lastUpdate, now <= last { return nil }
        lastUpdate = now
        let progress = progressFraction * routeLength
        // Look as far ahead as roughly eight seconds of travel, but not into the
        // curve the vehicle is already in.
        let start = progress + 15, end = progress + max(80, speed * 8)
        guard let tightestIndex = samples.indices.filter({
                  let sample = samples[$0]
                  return sample.distance >= start && sample.distance <= end && sample.radius <= Self.radiusRange.upperBound
              }).min(by: { samples[$0].radius < samples[$1].radius }),
              let bend = bend(containing: tightestIndex), Self.radiusRange.contains(bend.radius) else {
            proposalKey = nil; proposalCount = 0; return nil
        }
        let lateral = speed * speed / bend.radius
        guard lateral >= Self.warningLateralAcceleration else { proposalKey = nil; proposalCount = 0; return nil }
        let key = bend.startIndex
        if proposalKey != key { proposalKey = key; proposalCount = 0 }
        proposalCount += 1
        guard proposalCount >= Self.requiredObservations else { return nil }
        let tightest = (distance: max(bend.startDistance, samples[tightestIndex].distance), radius: bend.radius)
        let advised = sqrt(Self.comfortableLateralAcceleration * tightest.radius)
        let severity: CurveSpeedWarning.Severity = lateral >= Self.sharpLateralAcceleration ? .sharp : .caution
        let text = severity == .sharp ? "Sharp curve ahead · slow down" : "Curve ahead · ease off"
        let spokenText = severity == .sharp ? "Sharp curve ahead. Slow down." : "Curve ahead. Ease off the speed."
        let shouldSpeak = !spoken.contains(key)
        if shouldSpeak { spoken.insert(key) }
        return CurveSpeedWarning(severity: severity, distance: tightest.distance - progress, radius: tightest.radius,
            advisedSpeed: advised, currentSpeed: speed, text: text, spoken: spokenText,
            shouldSpeak: shouldSpeak, capturedAt: now)
    }
}
